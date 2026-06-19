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
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from psycopg import OperationalError
from psycopg_pool import AsyncConnectionPool
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import BearerAuthMiddleware
from .config import Settings, get_settings
from .observability import AccessLogMiddleware, setup_logging
from .server import build_mcp
from .storage.postgres import PostgresBackend

log = structlog.get_logger(__name__)


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging(settings.log_level)
    log.info("startup", **settings.as_log_safe())

    if not settings.auth_token:
        raise RuntimeError("MCP_AUTH_TOKEN is required; refusing to start")

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


# /healthz is registered before the catch-all mount that serves the MCP tools at
# /mcp. Bearer auth wraps the whole stack and lets GET /healthz and / through.
app = FastAPI(title="assist-memory", lifespan=lifespan)
app.add_api_route("/healthz", _healthz, methods=["GET"])
app.mount("/", _mcp_app)
# Outermost first: the access log sees every request, including 401s.
app.add_middleware(BearerAuthMiddleware, token=settings.auth_token)
app.add_middleware(AccessLogMiddleware)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port, access_log=False)
