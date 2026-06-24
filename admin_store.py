"""PostgreSQL store for the dashboard-managed MCP auth tokens.

Lives in the same database as the memory store but a separate table
(``admin_auth_tokens``) — it holds only token-management data, never agent
memory. The table is self-created by ``init()`` at boot (it is intentionally not
part of the numbered memory migrations, which stay focused on the data schema).

Multiple labelled tokens are supported: one active token per ``label`` (e.g.
``web`` for the claude.ai connector, ``desktop-cli`` for Claude Desktop + the
Claude Code CLI). The auth middleware accepts ANY active token, so each surface
can be rotated or revoked independently without disturbing the others.

The active tokens are cached in-process (short TTL) so the auth middleware does
not hit the DB on every request. The deployment is a single Reserved VM, so the
cache is effectively authoritative and rotations refresh it immediately; the TTL
bounds staleness to a few seconds if ever scaled horizontally.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import psycopg

CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class TokenInfo:
    label: str
    token: str
    created_at: datetime


class AdminStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._lock = threading.Lock()
        self._cache: set[str] | None = None
        self._cache_at: float = 0.0
        self._initialized = False

    def _connect(self) -> psycopg.Connection:
        # prepare_threshold=None disables psycopg3 server-side prepared statements,
        # required for PgBouncer transaction pooling (Neon's pooled endpoint);
        # harmless on a direct endpoint.
        return psycopg.connect(self._dsn, prepare_threshold=None)

    def init(self) -> None:
        """Create/upgrade the token table. Deferred from __init__ so the store can
        be built at import time without a live DB; called from the app lifespan.

        Upgrade path: a pre-existing single-token table (unique active row, no
        ``label``) gains a ``label`` column defaulting to ``web`` — so the live
        token is preserved under the ``web`` surface — and the global "one active
        row" index is replaced by a per-label one."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_auth_tokens (
                    id BIGSERIAL PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT 'web',
                    token TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    active BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
            # Upgrade older tables created before labels existed.
            cur.execute(
                "ALTER TABLE admin_auth_tokens "
                "ADD COLUMN IF NOT EXISTS label TEXT NOT NULL DEFAULT 'web'"
            )
            # Replace the old global single-active index with a per-label one so
            # each surface keeps exactly one active token.
            cur.execute("DROP INDEX IF EXISTS uniq_admin_auth_token_active")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_admin_auth_token_active_label "
                "ON admin_auth_tokens (label) WHERE active"
            )
            conn.commit()
        self._initialized = True

    # ------------------------------------------------------------------ reads
    def _fetch_active(self) -> list[TokenInfo]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT label, token, created_at FROM admin_auth_tokens "
                "WHERE active ORDER BY label"
            )
            rows = cur.fetchall()
        return [TokenInfo(label=r[0], token=r[1], created_at=r[2]) for r in rows]

    def list_tokens(self) -> list[TokenInfo]:
        """All active tokens (one per label), ordered by label."""
        return self._fetch_active()

    def info(self, label: str) -> TokenInfo | None:
        for t in self._fetch_active():
            if t.label == label:
                return t
        return None

    def get_active_tokens(self) -> set[str]:
        """The set of every active token value (cached). Used by the auth gate."""
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now - self._cache_at < CACHE_TTL_SECONDS:
                return self._cache
        tokens = {t.token for t in self._fetch_active()}
        with self._lock:
            self._cache = tokens
            self._cache_at = time.monotonic()
        return tokens

    # ----------------------------------------------------------------- writes
    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_at = 0.0

    def set_token(self, label: str, token: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_auth_tokens SET active = FALSE WHERE active AND label = %s",
                (label,),
            )
            cur.execute(
                "INSERT INTO admin_auth_tokens (label, token, active) VALUES (%s, %s, TRUE)",
                (label, token),
            )
            conn.commit()
        self._invalidate()

    def rotate(self, label: str) -> str:
        token = secrets.token_urlsafe(32)
        self.set_token(label, token)
        return token

    def ensure_tokens(self, labels: list[str], seed: dict[str, str | None] | None = None) -> None:
        """Make sure each label has an active token, creating any that are missing.
        A label's ``seed`` (e.g. MCP_AUTH_TOKEN for ``web``) is preferred on first
        creation so existing client registrations keep working; otherwise a strong
        token is generated. Existing tokens are never overwritten."""
        seed = seed or {}
        existing = {t.label for t in self._fetch_active()}
        for label in labels:
            if label in existing:
                continue
            s = seed.get(label)
            token = s.strip() if s and s.strip() else secrets.token_urlsafe(32)
            self.set_token(label, token)
