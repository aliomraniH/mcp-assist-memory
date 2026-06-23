"""Transparent reconnect: a backend op whose connection was dropped server-side
(e.g. "terminating connection due to administrator command", SQLSTATE 57P01) is
retried on a fresh connection instead of surfacing to the caller — but only when
a replay is safe (reads, or idempotent writes). These are pure unit tests of the
decorators; no DB required, so they run even when DATABASE_URL is unset."""
from __future__ import annotations

import psycopg
import pytest

from storage.postgres import (
    _CONN_RETRIES,
    _is_disconnect,
    _retry_if_idempotent,
    _retry_on_disconnect,
)


class _OpErr(psycopg.OperationalError):
    """OperationalError with a controllable SQLSTATE for testing the predicate."""

    def __init__(self, sqlstate: str | None = None) -> None:
        self._ss = sqlstate  # set before super().__init__ reads self.sqlstate
        super().__init__("simulated")

    @property
    def sqlstate(self) -> str | None:  # overrides psycopg's diag-backed property
        return self._ss


# --------------------------------------------------------------- predicate
def test_predicate_flags_real_disconnects():
    assert _is_disconnect(_OpErr("57P01"))          # admin shutdown
    assert _is_disconnect(_OpErr("57P02"))          # crash shutdown
    assert _is_disconnect(_OpErr("08006"))          # connection failure (08xxx)
    assert _is_disconnect(_OpErr(None))             # client-side "connection closed"
    assert _is_disconnect(psycopg.InterfaceError("connection already closed"))


def test_predicate_ignores_non_connection_errors():
    assert not _is_disconnect(_OpErr("53300"))      # too_many_connections
    assert not _is_disconnect(_OpErr("55P03"))      # lock_not_available
    assert not _is_disconnect(ValueError("nope"))


# --------------------------------------------------- _retry_on_disconnect
async def test_recovers_after_one_disconnect():
    calls = {"n": 0}

    @_retry_on_disconnect
    async def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _OpErr("57P01")
        return "ok"

    assert await op() == "ok"
    assert calls["n"] == 2  # failed once, retried, succeeded


async def test_exhausts_retries_then_reraises():
    calls = {"n": 0}

    @_retry_on_disconnect
    async def op():
        calls["n"] += 1
        raise _OpErr("57P01")

    with pytest.raises(psycopg.OperationalError):
        await op()
    assert calls["n"] == _CONN_RETRIES


async def test_does_not_retry_non_disconnect_operational_error():
    calls = {"n": 0}

    @_retry_on_disconnect
    async def op():
        calls["n"] += 1
        raise _OpErr("53300")  # too_many_connections — not a disconnect

    with pytest.raises(psycopg.OperationalError):
        await op()
    assert calls["n"] == 1  # surfaced immediately, no retry


async def test_does_not_retry_unrelated_exceptions():
    calls = {"n": 0}

    @_retry_on_disconnect
    async def op():
        calls["n"] += 1
        raise ValueError("not a connection error")

    with pytest.raises(ValueError):
        await op()
    assert calls["n"] == 1


# --------------------------------------------------- _retry_if_idempotent
async def test_idempotent_write_retries_only_with_event_id():
    calls = {"n": 0}

    @_retry_if_idempotent
    async def save(self, value, *, event_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _OpErr("57P01")
        return "ok"

    # With an event_id a replay is exactly-once, so it retries and recovers.
    assert await save(object(), "v", event_id="evt-1") == "ok"
    assert calls["n"] == 2


async def test_non_idempotent_write_surfaces_without_retry():
    calls = {"n": 0}

    @_retry_if_idempotent
    async def save(self, value, *, event_id=None):
        calls["n"] += 1
        raise _OpErr("57P01")

    # No event_id -> not safe to replay -> runs once and surfaces.
    with pytest.raises(psycopg.OperationalError):
        await save(object(), "v")
    assert calls["n"] == 1
