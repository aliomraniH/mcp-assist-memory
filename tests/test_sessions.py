import re

import pytest

from .conftest import ToolFailure


async def test_session_lifecycle_with_ordered_events(call):
    started = await call("session_start", surface="cli", label="Fix Charts!")
    sid = started["session_id"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z_fix-charts$", sid)
    assert started["status"] == "open"

    for i, (etype, msg) in enumerate(
        [("note", "reproduced bug"), ("milestone", "found root cause"), ("note", "fix drafted")],
        start=1,
    ):
        logged = await call("session_log", session_id=sid, type=etype, message=msg)
        assert logged["seq"] == i

    ended = await call("session_end", session_id=sid, summary="fixed percentile bands")
    assert ended["status"] == "closed"
    assert ended["event_count"] == 3
    assert ended["ended_at"] is not None

    full = await call("session_get", session_id=sid)
    assert full["summary"] == "fixed percentile bands"
    assert [e["seq"] for e in full["events"]] == [1, 2, 3]
    assert [e["message"] for e in full["events"]] == [
        "reproduced bug",
        "found root cause",
        "fix drafted",
    ]


async def test_log_and_end_on_closed_session_rejected(call):
    sid = (await call("session_start", surface="web"))["session_id"]
    await call("session_end", session_id=sid, summary="done")

    with pytest.raises(ToolFailure) as exc:
        await call("session_log", session_id=sid, type="note", message="late")
    assert exc.value.code == "SESSION_CLOSED"

    with pytest.raises(ToolFailure) as exc:
        await call("session_end", session_id=sid, summary="again")
    assert exc.value.code == "SESSION_CLOSED"


async def test_caller_supplied_session_id_and_conflict(call):
    sid = "2026-06-10T08-00-00Z_manual"
    started = await call("session_start", surface="desktop", session_id=sid)
    assert started["session_id"] == sid

    with pytest.raises(ToolFailure) as exc:
        await call("session_start", surface="desktop", session_id=sid)
    assert exc.value.code == "SESSION_EXISTS"


async def test_session_list_filters(call):
    a = (await call("session_start", surface="cli", label="a"))["session_id"]
    await call("session_start", surface="web", label="b", namespace="proj-a")
    await call("session_end", session_id=a, summary="done")

    default_ns = await call("session_list")
    assert [s["session_id"] for s in default_ns["sessions"]] == [a]

    open_only = await call("session_list", status="open")
    assert open_only["count"] == 0

    proj = await call("session_list", namespace="proj-a", status="open")
    assert proj["count"] == 1


async def test_unknown_session(call):
    with pytest.raises(ToolFailure) as exc:
        await call("session_get", session_id="nope")
    assert exc.value.code == "NOT_FOUND"

    with pytest.raises(ToolFailure) as exc:
        await call("session_log", session_id="nope", type="note", message="m")
    assert exc.value.code == "NOT_FOUND"
