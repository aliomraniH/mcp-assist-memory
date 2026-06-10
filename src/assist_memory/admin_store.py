"""Separate PostgreSQL store for the dashboard-managed auth token.

This database is deliberately kept separate from the MCP memory store (which
lives in SQLite under DATA_DIR). It holds only token-management data — never
any agent memory, sessions, handoffs, or artifacts.

The active token is cached in-process so the auth middleware does not hit the
database on every request. The deployment runs a single instance (Reserved
VM), so the cache is authoritative; rotations update both the database and the
cache atomically.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime

import psycopg


@dataclass(frozen=True)
class TokenInfo:
    token: str
    created_at: datetime


class AdminStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._lock = threading.Lock()
        self._cache: str | None = None
        self._init_db()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

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
            conn.commit()

    def get_active_token(self) -> str | None:
        with self._lock:
            if self._cache is not None:
                return self._cache
        info = self.info()
        token = info.token if info else None
        with self._lock:
            self._cache = token
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
