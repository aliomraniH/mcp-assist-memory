"""FastAPI entrypoint for the Postgres-backed deployment (Replit Reserved VM).

Exposes the 18 MCP tools at /mcp (bearer-authed) and an unauthenticated
/healthz liveness probe. One AsyncConnectionPool is opened in the lifespan and
shared by the PostgresBackend; no other module opens a connection.

The pool is created unopened at import (no network), the backend + FastMCP app
are built at module scope, and the pool is opened — with a bounded readiness
probe — inside the lifespan so a Neon cold start or partition fails fast and
lets Replit restart the VM rather than hanging.
"""

from __future__ import annotations

import asyncio
import secrets as _secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from psycopg import OperationalError
from psycopg_pool import AsyncConnectionPool
from starlette.requests import Request
from starlette.responses import JSONResponse

from .admin_store import AdminStore
from .auth import BearerAuthMiddleware
from .config import Settings, get_settings
from .dashboard import build_routes
from .observability import AccessLogMiddleware, setup_logging
from .server import build_mcp
from .storage.postgres import PostgresBackend

log = structlog.get_logger(__name__)

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "0001_init.sql"


def create_pool(dsn: str) -> AsyncConnectionPool:
    """Build the shared pool (unopened). Tuned for Neon's pooled (PgBouncer) endpoint.

    min_size=0 so a cold DB is never warmed through the pool; prepare_threshold
    is disabled because PgBouncer transaction pooling is incompatible with
    server-side prepared statements; statement/idle timeouts bound every query.
    """
    return AsyncConnectionPool(
        dsn,
        open=False,
        min_size=0,
        max_size=10,
        timeout=10.0,
        reconnect_timeout=30.0,
        max_idle=60.0,
        kwargs={
            "connect_timeout": 10,
            "prepare_threshold": None,
            "options": (
                "-c statement_timeout=15000 "
                "-c idle_in_transaction_session_timeout=15000"
            ),
        },
    )


async def _probe(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")


settings: Settings = get_settings()

# Built at import; the pool is attached in the lifespan.
backend = PostgresBackend(
    None, settings.max_total_storage_bytes, settings.max_artifact_bytes
)
mcp = build_mcp(settings, backend)
_mcp_app = mcp.streamable_http_app()

# The live MCP auth token is managed in Postgres (admin_auth_tokens) and
# rotatable via the password-protected /admin dashboard. The store is built
# lazily here (no DB I/O) and initialized in the lifespan. With no DATABASE_URL
# (tests/dev import), there is no admin store and auth falls back to the static
# settings token.
_admin: AdminStore | None = (
    AdminStore(settings.database_url.get_secret_value())
    if settings.database_url is not None
    else None
)
_admin_password = (
    settings.admin_password.get_secret_value() if settings.admin_password else ""
)
_session_secret = (
    (settings.session_secret.get_secret_value() if settings.session_secret else "")
    or _admin_password
    or _secrets.token_urlsafe(32)
)


def _token_provider() -> str | None:
    if _admin is not None:
        return _admin.get_active_token()
    return settings.auth_token or None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging(settings.log_level)
    log.info("startup", **settings.as_log_safe())

    pool = create_pool(settings.database_url_str())
    await pool.open()  # returns immediately (min_size=0)
    try:
        # Bound the readiness wait — never a bare pool.wait(): a Neon cold start
        # or silent partition must crash terminally, not hang the lifespan.
        async with asyncio.timeout(15):
            await _probe(pool)
    except (TimeoutError, OperationalError) as exc:
        log.error("db_unready", error=str(exc))
        await pool.close()
        raise

    backend.pool = pool
    app.state.pool = pool

    # Apply the frozen schema (idempotent; everything is IF NOT EXISTS). The
    # managed Replit/Neon database uses a single role, so the app self-migrates
    # on startup — mirroring the admin store's table self-creation. `make
    # migrate` remains available for an explicit owner-role workflow.
    if _MIGRATION.exists():
        async with pool.connection() as conn:
            await conn.execute(_MIGRATION.read_text())
            await conn.commit()
        log.info("migrations_applied")

    # Create the admin token table and ensure a live token exists, seeding from
    # MCP_AUTH_TOKEN on first boot so any pre-existing client registration keeps
    # working. After this the token is owned by the /admin dashboard.
    if _admin is not None:
        _admin.init()
        _admin.ensure_token(seed=settings.auth_token or None)
        if not _admin_password:
            log.warning("admin_password_unset")  # /admin login disabled until set
    elif not settings.auth_token:
        raise RuntimeError(
            "No DATABASE_URL admin store and no MCP_AUTH_TOKEN fallback; "
            "refusing to start without an auth token."
        )
    log.info("ready")

    # Run the FastMCP streamable-HTTP transport's own (untouched) lifespan so its
    # session manager is initialized; then hand control to the server.
    try:
        async with _mcp_app.router.lifespan_context(app):
            yield
    finally:
        log.info("shutdown")
        await pool.close()
        backend.pool = None  # type: ignore[assignment]


async def _healthz(request: Request) -> JSONResponse:
    """Bounded SELECT 1 liveness probe. No auth, returns no data."""
    pool: AsyncConnectionPool | None = getattr(request.app.state, "pool", None)
    db_ok = False
    if pool is not None:
        try:
            async with asyncio.timeout(5):
                await _probe(pool)
            db_ok = True
        except Exception:
            db_ok = False
    status = 200 if db_ok else 503
    return JSONResponse(
        {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"},
        status_code=status,
    )


# /healthz and the /admin dashboard are registered before the catch-all mount
# that serves the MCP tools at /mcp. Bearer auth wraps the whole stack and lets
# GET /healthz, GET /, and the (self-authenticating) /admin routes through.
app = FastAPI(title="assist-memory", lifespan=lifespan)
app.add_api_route("/healthz", _healthz, methods=["GET"])
if _admin is not None:
    for route in build_routes(_admin, _session_secret, _admin_password):
        app.router.routes.append(route)
app.mount("/", _mcp_app)
# Outermost first: the access log sees every request, including 401s. The token
# provider reads the live (rotatable) token from the admin store on each request.
app.add_middleware(BearerAuthMiddleware, token_provider=_token_provider)
app.add_middleware(AccessLogMiddleware)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port, access_log=False)
