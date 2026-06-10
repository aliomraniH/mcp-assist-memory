"""Bearer-token ASGI middleware: 401 before any MCP routing is reachable.

The only anonymous route is GET/HEAD / (health probe).
"""

from __future__ import annotations

import secrets

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/" and scope["method"] in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        auth = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                auth = value
                break
        if not secrets.compare_digest(auth, self._expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
