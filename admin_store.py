"""PostgreSQL store for the dashboard-managed MCP auth token.

Lives in the same database as the memory store but a separate table
(``admin_auth_tokens``) — it holds only token-management data, never agent
memory. The table is self-created by ``init()`` at boot (it is intentionally not
part of the numbered memory migrations, which stay focused on the data schema).

The active token is cached in-process (short TTL) so the auth middleware does
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
    token: str
    created_at: datetime


class AdminStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._lock = threading.Lock()
        self._cache: str | None = None
        self._cache_at: float = 0.0
        self._initialized = False

    def _connect(self) -> psycopg.Connection:
        # prepare_threshold=None disables psycopg3 server-side prepared statements,
        # required for PgBouncer transaction pooling (Neon's pooled endpoint);
        # harmless on a direct endpoint.
        return psycopg.connect(self._dsn, prepare_threshold=None)

    def init(self) -> None:
        """Create the token table. Deferred from __init__ so the store can be
        built at import time without a live DB; called from the app lifespan."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_auth_tokens (
                    id BIGSERIAL PRIMARY KEY,
                    token TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    active BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
            # At most one active token row, enforced at the DB level.
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_admin_auth_token_active "
                "ON admin_auth_tokens (active) WHERE active"
            )
            conn.commit()
        self._initialized = True

    def get_active_token(self) -> str | None:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now - self._cache_at < CACHE_TTL_SECONDS:
                return self._cache
        info = self.info()
        token = info.token if info else None
        with self._lock:
            self._cache = token
            self._cache_at = time.monotonic()
        return token

    def info(self) -> TokenInfo | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT token, created_at FROM admin_auth_tokens "
                "WHERE active ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return None
        return TokenInfo(token=row[0], created_at=row[1])

    def set_token(self, token: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE admin_auth_tokens SET active = FALSE WHERE active")
            cur.execute(
                "INSERT INTO admin_auth_tokens (token, active) VALUES (%s, TRUE)",
                (token,),
            )
            conn.commit()
        with self._lock:
            self._cache = token

    def rotate(self) -> str:
        token = secrets.token_urlsafe(32)
        self.set_token(token)
        return token

    def ensure_token(self, seed: str | None = None) -> str:
        """Return the active token, creating one on first boot. Prefers an
        explicit ``seed`` (e.g. MCP_AUTH_TOKEN) so existing client registrations
        keep working; otherwise generates a strong token."""
        existing = self.get_active_token()
        if existing:
            return existing
        token = seed.strip() if seed and seed.strip() else secrets.token_urlsafe(32)
        self.set_token(token)
        return token
