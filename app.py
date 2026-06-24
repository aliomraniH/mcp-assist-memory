"""FastAPI application: owns the ONE connection pool, mounts the MCP app, exposes
/healthz, a streamed /artifact/{sha256}, and the password-gated /admin token
dashboard.

Boot discipline: the lifespan never waits unbounded. The pool is built with
explicit timeouts, opened immediately, and a single SELECT 1 readiness probe is
wrapped in asyncio.timeout(). On failure we raise — a terminal crash lets the
Reserved VM restart instead of hanging forever.

Auth: the live MCP tokens are managed in Postgres (admin_auth_tokens) and
rotatable via /admin without a redeploy. One active token per surface label
(``web`` for the claude.ai connector, ``desktop-cli`` for Claude Desktop + the
Claude Code CLI); the gate accepts ANY active token, so surfaces rotate/revoke
independently. The middleware reads them on every request (5s cache). /healthz,
/artifact, and /admin are not behind the bearer gate (/admin enforces its own
password session).

Transport: the MCP app runs in stateless HTTP mode — every request is
self-contained, so client sessions survive VM restarts/redeploys and there is no
in-memory session affinity to lose across the three surfaces.
"""
from __future__ import annotations

import asyncio
import hmac
import secrets as _secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg import errors as pg_errors
from psycopg_pool import AsyncConnectionPool
from starlette.middleware.base import BaseHTTPMiddleware

from admin_store import AdminStore
from config import settings
from dashboard import SURFACE_LABELS, build_routes
from server.mcp_server import deps, mcp
from storage.postgres import PostgresBackend

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("assist-memory")

# Token store + dashboard. Built lazily (no DB I/O) so the module imports without
# a live DB; init() runs in the lifespan.
admin = AdminStore(settings.database_url)
_session_secret = settings.session_secret or settings.admin_password or _secrets.token_urlsafe(32)
_admin_password = settings.admin_password or ""


def _build_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(
        settings.database_url,
        open=False,
        min_size=0,                                    # don't warm a pool through a cold Neon
        max_size=settings.pool_max_size,
        timeout=settings.pool_timeout,                 # caller checkout cap
        reconnect_timeout=settings.pool_reconnect_timeout,
        max_idle=settings.pool_max_idle,
        # Validate each connection on checkout so one terminated server-side while
        # idle (Neon scale-down / "terminating connection due to administrator
        # command") is discarded and replaced instead of handed to a caller.
        check=AsyncConnectionPool.check_connection,
        num_workers=1,
        kwargs={
            "connect_timeout": settings.db_connect_timeout,
            "options": (
                f"-c statement_timeout={settings.db_statement_timeout_ms} "
                f"-c idle_in_transaction_session_timeout={settings.db_statement_timeout_ms}"
            ),
            # Neon's pooled (PgBouncer, transaction mode) endpoint needs prepared
            # statements off.
            "prepare_threshold": None,
        },
    )


# The MCP ASGI app (Streamable HTTP), stateless: each request is self-contained,
# so the three surfaces share no in-memory session state and survive restarts.
# Its lifespan still runs the session manager and must be entered while serving.
mcp_app = mcp.http_app(path="/", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = _build_pool()
    await pool.open()  # returns immediately; does not block on min_size
    try:
        async with asyncio.timeout(settings.readiness_timeout_s):
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
    except (TimeoutError, pg_errors.OperationalError) as exc:
        log.error("db_not_ready_at_boot", error=str(exc))
        await pool.close()
        raise  # terminal crash -> supervised restart, never a hung lifespan

    # Token table + ensure one active token per surface. The web token is seeded
    # from MCP_AUTH_TOKEN on first boot (so existing connector registrations keep
    # working); the desktop-cli token is generated and managed via /admin.
    admin.init()
    admin.ensure_tokens(SURFACE_LABELS, seed={"web": settings.mcp_auth_token or None})
    if not _admin_password:
        log.warning("admin_password_unset")  # /admin login disabled until set

    app.state.pool = pool
    deps.backend = PostgresBackend(pool)
    log.info("startup_ok", max_size=settings.pool_max_size)
    try:
        async with mcp_app.lifespan(app):  # run the MCP session manager
            yield
    finally:
        deps.backend = None
        await pool.close()
        log.info("shutdown_ok")


app = FastAPI(title="mcp-assist-memory", lifespan=lifespan)


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Bearer-token gate for the /mcp surface. The expected token is read live
    from the admin store (rotation takes effect immediately). /healthz,
    /artifact, and /admin are not gated here."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            active = admin.get_active_tokens()
            if not active or not _request_has_token(request, active):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def _matches_any(presented: str, active: set[str]) -> bool:
    # Constant-time compare against each active token; any match authorizes.
    return any(hmac.compare_digest(presented.encode(), t.encode()) for t in active)


def _request_has_token(request: Request, active: set[str]) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and _matches_any(auth[len("Bearer ") :], active):
        return True
    # Fallback for headerless clients (e.g. the claude.ai web connector): ?token=
    for candidate in parse_qs(request.url.query).get("token", []):
        if _matches_any(candidate, active):
            return True
    return False


app.add_middleware(MCPAuthMiddleware)

# Mount the password-gated /admin dashboard routes.
for _route in build_routes(admin, _session_secret, _admin_password):
    app.router.routes.append(_route)

app.mount("/mcp", mcp_app)


@app.get("/healthz")
async def healthz() -> Response:
    pool: AsyncConnectionPool = app.state.pool
    try:
        async with asyncio.timeout(5):
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
    except Exception as exc:  # liveness probe: any failure is "degraded"
        log.warning("healthz_degraded", error=str(exc))
        return JSONResponse({"status": "degraded", "db": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})


@app.get("/artifact/{sha256}")
async def get_artifact(sha256: str) -> Response:
    """Stream a blob from bytea in bounded windows (peak memory = one chunk)."""
    backend: PostgresBackend = deps.backend  # type: ignore[assignment]
    meta = await backend.artifact_get(sha256)
    if meta is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    chunk = settings.artifact_stream_chunk
    size = meta["size"]

    async def body():
        offset = 0
        while offset < size:
            window = await backend.artifact_read_range(sha256, offset, chunk)
            if not window:
                break
            yield window
            offset += len(window)

    headers = {"Content-Length": str(size)}
    return StreamingResponse(
        body(),
        media_type=meta.get("content_type") or "application/octet-stream",
        headers=headers,
    )
