"""Bearer-token ASGI middleware: 401 before any MCP routing is reachable.

Two ways to present the same MCP_AUTH_TOKEN:
- Authorization: Bearer <token>   (preferred — Claude Code CLI/Desktop, Cursor,
  and most MCP clients support custom headers)
- ?token=<token> query parameter  (fallback for clients that cannot send
  custom headers, e.g. the claude.ai web connector UI)

The only anonymous routes are GET/HEAD / and GET/HEAD /healthz (liveness/health
probes). Token values are never logged; the access log omits query strings
entirely.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("assist_memory.auth")

QUERY_TOKEN_PARAM = "token"


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self._expected_header = f"Bearer {token}".encode()
        self._expected_token = token.encode()

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                if secrets.compare_digest(value, self._expected_header):
                    return True
                break
        params = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        for candidate in params.get(QUERY_TOKEN_PARAM, []):
            if secrets.compare_digest(candidate.encode("latin-1"), self._expected_token):
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in ("/", "/healthz") and scope["method"] in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        if not self._authorized(scope):
            user_agent = next(
                (
                    v.decode("latin-1")
                    for n, v in scope.get("headers", [])
                    if n.lower() == b"user-agent"
                ),
                "",
            )
            logger.warning(
                'unauthorized method=%s path=%s user_agent="%s"',
                scope["method"],
                scope["path"],
                user_agent,
            )
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
