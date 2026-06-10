"""Structured logging: access log middleware and per-tool-call logging.

Hygiene rules: never log tokens, query strings, or stored values — only
names, codes, sizes, and durations.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .models import ToolFault

access_logger = logging.getLogger("assist_memory.access")
tool_logger = logging.getLogger("assist_memory.tools")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class AccessLogMiddleware:
    """Logs method, path, status, duration, and client user-agent per request.

    The query string is deliberately never logged (it may carry the auth token
    for clients that cannot send headers).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_holder = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            user_agent = ""
            for name, value in scope.get("headers", []):
                if name.lower() == b"user-agent":
                    user_agent = value.decode("latin-1")
                    break
            path = scope["path"]
            log = access_logger.debug if path == "/" else access_logger.info
            log(
                'method=%s path=%s status=%s duration_ms=%.1f user_agent="%s"',
                scope["method"],
                path,
                status_holder["status"],
                (time.perf_counter() - start) * 1000,
                user_agent,
            )


def logged(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wraps an MCP tool: logs name, outcome, and duration; never argument values."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except ToolFault as fault:
            tool_logger.warning(
                "tool=%s outcome=error code=%s duration_ms=%.1f",
                fn.__name__,
                fault.code,
                (time.perf_counter() - start) * 1000,
            )
            raise
        except Exception:
            tool_logger.exception(
                "tool=%s outcome=crash duration_ms=%.1f",
                fn.__name__,
                (time.perf_counter() - start) * 1000,
            )
            raise
        tool_logger.info(
            "tool=%s outcome=ok duration_ms=%.1f",
            fn.__name__,
            (time.perf_counter() - start) * 1000,
        )
        return result

    return wrapper
