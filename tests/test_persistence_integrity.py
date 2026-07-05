"""Phase 2 (T2.6): deliberate-violation tests — prove the guards by attacking them.

Covers: actor-scoped dedup (both directions), exactly-once visibility, phantom-
success prevention on persist failure, read-back mismatch injection, artifact
read-back, session-event idempotency, and the fail-closed grep audit (T2.4).
"""
from __future__ import annotations

import base64
import json
import pathlib
import re
import uuid

import pytest

from errors import AppError
from server import mcp_server


# ------------------------------------------------------------- T2.1 actor scope
async def test_distinct_actors_same_event_id_both_persist(backend, ns):
    eid = str(uuid.uuid4())
    a = await backend.memory_save(ns, "obs/subject", {"claim": "X"}, event_id=eid, actor="subject")
    b = await backend.memory_save(ns, "obs/instrument", {"measured": "X"}, event_id=eid, actor="instrument")
    assert a["deduplicated"] is False and b["deduplicated"] is False
    assert (await backend.memory_get(ns, "obs/subject")) is not None
    assert (await backend.memory_get(ns, "obs/instrument")) is not None


async def test_same_actor_retry_is_exactly_once_and_visible(backend, ns):
    eid = str(uuid.uuid4())
    first = await backend.memory_save(ns, "k", {"v": 1}, event_id=eid, actor="agent-a")
    replay = await backend.memory_save(ns, "k", {"v": "DIFFERENT"}, event_id=eid, actor="agent-a")
    assert replay["deduplicated"] is True
    assert replay["revision"] == first["revision"]
    hist = await backend.memory_history(ns, "k")
    assert len(hist) == 1  # exactly once


async def test_same_event_id_different_namespace_both_persist(backend, ns):
    eid = str(uuid.uuid4())
    await backend.memory_save(ns, "k", {"v": 1}, event_id=eid)
    other = await backend.memory_save(f"{ns}-b", "k", {"v": 2}, event_id=eid)
    assert other["deduplicated"] is False and other["namespace"] == f"{ns}-b"


async def test_legacy_callers_still_dedup_in_unattributed_bucket(backend, ns):
    eid = str(uuid.uuid4())
    await backend.memory_save(ns, "k", {"v": 1}, event_id=eid)          # no actor
    replay = await backend.memory_save(ns, "k", {"v": 2}, event_id=eid)  # no actor
    assert replay["deduplicated"] is True and replay["actor"] == "unattributed"


# --------------------------------------------------- T2.3 read-back verification
async def test_simulated_persist_failure_no_phantom_success(backend, ns):
    """A failure on the ack path (invalid kind → CHECK violation inside the
    INSERT) must produce an error result and ZERO persisted rows."""
    with pytest.raises(AppError):
        await backend.memory_save(ns, "phantom", {"v": 1}, kind="bogus")
    assert await backend.memory_get(ns, "phantom") is None
    assert await backend.memory_history(ns, "phantom") == []


async def test_readback_mismatch_injection(backend, ns, monkeypatch):
    """Corrupt the public read path: the write must refuse to ack success."""
    real_history = backend.memory_history

    async def lying_history(namespace, key, *, limit=50):
        rows = await real_history(namespace, key, limit=limit)
        for r in rows:
            r["content_hash"] = "0" * 64  # storage returns something else than written
        return rows

    monkeypatch.setattr(backend, "memory_history", lying_history)
    with pytest.raises(AppError) as exc:
        await backend.memory_save(ns, "k", {"v": 1})
    assert exc.value.code == "write_verification_failed"
    assert exc.value.retryable is True


async def test_readback_missing_row_injection(backend, ns, monkeypatch):
    async def empty_history(namespace, key, *, limit=50):
        return []

    monkeypatch.setattr(backend, "memory_history", empty_history)
    with pytest.raises(AppError) as exc:
        await backend.memory_save(ns, "k", {"v": 1})
    assert exc.value.code == "write_verification_failed"


async def test_tombstone_writes_are_verified_too(backend, ns):
    await backend.memory_save(ns, "gone", {"v": 1})
    out = await backend.memory_delete(ns, "gone")
    assert out["verified_persisted"] is True and out["tombstone"] is True


async def test_artifact_put_readback_verified(backend):
    data = b"phase-2 artifact readback"
    out = await backend.artifact_put(data, content_type="text/plain")
    assert out["verified_persisted"] is True
    assert out["readback_latency_ms"] >= 0
    stored = await backend.artifact_read_range(out["sha256"], 0, out["size"])
    assert stored == data


async def test_artifact_readback_mismatch_injection(backend, monkeypatch):
    async def lying_range(sha256, offset, length):
        return b"not what was written"

    monkeypatch.setattr(backend, "artifact_read_range", lying_range)
    with pytest.raises(AppError) as exc:
        await backend.artifact_put(b"real bytes")
    assert exc.value.code == "write_verification_failed"


# ------------------------------------------------- session_event idempotency
async def test_session_event_exactly_once_with_event_id(backend, ns):
    sess = await backend.session_create(ns, surface="test")
    sid = sess["session_id"]
    eid = str(uuid.uuid4())
    first = await backend.session_append_event(ns, sid, "step", {"n": 1},
                                               actor="agent-a", event_id=eid)
    assert first["deduplicated"] is False
    replay = await backend.session_append_event(ns, sid, "step", {"n": 2},
                                                actor="agent-a", event_id=eid)
    assert replay["deduplicated"] is True and replay["seq"] == first["seq"]
    events = await backend.session_events(ns, sid)
    assert len(events) == 1
    # a different actor's same event_id is an independent event
    other = await backend.session_append_event(ns, sid, "step", {"n": 3},
                                               actor="agent-b", event_id=eid)
    assert other["deduplicated"] is False
    assert len(await backend.session_events(ns, sid)) == 2


# ---------------------------------------------------- T2.5 standardized errors
async def test_tool_layer_serializes_apperror_payload(backend, ns):
    from fastmcp.exceptions import ToolError

    mcp_server.deps.backend = backend
    try:
        with pytest.raises(ToolError) as exc:
            await mcp_server.artifact_put(content_base64="!!!not-base64!!!")
        payload = json.loads(str(exc.value))
        assert payload["error"]["code"] == "invalid_base64"
        assert payload["error"]["remedy"]
    finally:
        mcp_server.deps.backend = None


def test_catalog_covers_paid_for_failures():
    from errors.catalog import CATALOG

    for code in ("mcp_session_stale", "unauthorized", "unknown_arg",
                 "artifact_not_found", "session_not_found",
                 "write_verification_failed"):
        remedy, _ = CATALOG[code]
        assert remedy


# ------------------------------------------------------- T2.4 fail-closed grep
def test_no_swallow_and_continue_in_write_paths():
    """The grep audit, automated: no bare `except:`, no except-pass, and no
    except-continue-without-raise outside the documented best-effort embed
    helpers (see docs/FAIL-CLOSED-WORKSHEET.md)."""
    src = (pathlib.Path(__file__).parent.parent / "storage" / "postgres.py").read_text()
    assert not re.search(r"except\s*:", src), "bare except in storage/postgres.py"
    # every swallow (except-pass or broad except Exception) must sit in a
    # documented best-effort helper — anything else is an audit failure
    swallows = list(re.finditer(r"except [^\n]+:\s*\n\s+pass\b", src))
    swallows += list(re.finditer(r"except Exception[^\n]*\n", src))
    for m in swallows:
        # documented either in the preceding context or on the handler line itself
        window = src[max(0, m.start() - 600): m.end()].lower()
        assert "best-effort" in window or "best effort" in window, (
            "undocumented swallow handler near: " + src[m.start(): m.start() + 80])
