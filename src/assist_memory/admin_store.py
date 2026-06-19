"""Separate PostgreSQL store for the dashboard-managed auth token.

This database is deliberately kept separate from the MCP memory store (which
lives in SQLite under DATA_DIR). It holds only token-management data — never
any agent memory, sessions, handoffs, or artifacts.

The active token is cached in-process (with a short TTL) so the auth
middleware does not hit the database on every request. The deployment runs a
single instance (Reserved VM), so the local cache is effectively authoritative
and rotations refresh it immediately. The TTL additionally bounds staleness to
a few seconds if the process is ever scaled horizontally, so a rotation on one
process is picked up by every other process within ``CACHE_TTL_SECONDS``.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import psycopg

# How long a cached token read is trusted before it is re-validated against the
# database. Keeps the auth hot path off the DB while bounding cross-process
# staleness after a rotation.
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
        # prepare_threshold=None disables psycopg3's server-side prepared
        # statements. This is required for PgBouncer in transaction-pooling
        # mode (e.g. Neon's pooled endpoint), where prepared statements span
        # connections and break. It is harmless on a direct Postgres endpoint.
        return psycopg.connect(self._dsn, prepare_threshold=None)

    def init(self) -> None:
        """Create the token table. Deferred from __init__ so the store can be
        built at import time without a live DB; called from the app lifespan."""
        self._init_db()
        self._initialized = True

    def _init_db(self) -> None:
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
            # Enforce at most one active token row at the database level so a
            # race between set_token/ensure_token cannot leave two active rows.
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_admin_auth_token_active "
                "ON admin_auth_tokens (active) WHERE active"
            )
            conn.commit()

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
        """Return the active token, creating one on first boot.

        Prefers an explicit ``seed`` (e.g. a pre-existing MCP_AUTH_TOKEN) so
        existing client registrations keep working; otherwise generates a new
        strong token.
        """
        existing = self.get_active_token()
        if existing:
            return existing
        token = seed.strip() if seed and seed.strip() else secrets.token_urlsafe(32)
        self.set_token(token)
        return token
