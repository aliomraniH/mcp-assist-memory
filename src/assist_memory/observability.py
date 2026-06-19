"""Structured logging: access log middleware and per-tool-call logging.

Hygiene rules: never log tokens, query strings, or stored values — only
names, codes, sizes, and durations.
"""

from __future__ import annotations

import functools
import inspect
import logging
import sys
import time
from typing import Any, Callable

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .models import ToolFault

access_logger = logging.getLogger("assist_memory.access")
tool_logger = logging.getLogger("assist_memory.tools")


def setup_logging(level: str = "INFO") -> None:
    """Render all logs as structlog JSON to stdout.

    Both structlog calls (app.py lifespan/healthz) and the stdlib loggers used
    here (access/tools/auth) flow through one JSON ProcessorFormatter, so the
    Replit/Reserved-VM stdout stream is uniformly structured.
    """
    lvl = getattr(logging, level.upper(), logging.INFO)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(lvl)


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


def _log_outcome(name: str, start: float, fault: ToolFault | None, crashed: bool) -> None:
    elapsed = (time.perf_counter() - start) * 1000
    if fault is not None:
        tool_logger.warning(
            "tool=%s outcome=error code=%s duration_ms=%.1f", name, fault.code, elapsed
        )
    elif crashed:
        tool_logger.exception("tool=%s outcome=crash duration_ms=%.1f", name, elapsed)
    else:
        tool_logger.info("tool=%s outcome=ok duration_ms=%.1f", name, elapsed)


def logged(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wraps an MCP tool: logs name, outcome, and duration; never argument values.

    Supports both sync and async tool functions (the Postgres-backed tools are
    async; the sentinel preserves the original signature for FastMCP).
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
            except ToolFault as fault:
                _log_outcome(fn.__name__, start, fault, False)
                raise
            except Exception:
                _log_outcome(fn.__name__, start, None, True)
                raise
            _log_outcome(fn.__name__, start, None, False)
            return result

        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except ToolFault as fault:
            _log_outcome(fn.__name__, start, fault, False)
            raise
        except Exception:
            _log_outcome(fn.__name__, start, None, True)
            raise
        _log_outcome(fn.__name__, start, None, False)
        return result

    return wrapper
