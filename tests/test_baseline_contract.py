"""Baseline contract (Phase 0, T0.3): pin CURRENT behavior before changing it.

Each test here is a deliberate flip target: a later phase that changes one of
these behaviors must edit the corresponding pinned test IN THE SAME commit and
record the flip in the ledger below. A baseline test failing in any other
circumstance means an accidental behavior change — that is the point of the file.

Flip ledger (append an entry when a phase flips a pin):
  * Phase 1 — test_baseline_unverified_ack: persisted revisions and their entry
    dicts now carry server_version/schema_version (rule 3).
  * Phase 2 — test_baseline_silent_dedup → visible dedup (T2.2);
    test_baseline_global_dedup_scope → (namespace, actor, event_id) scope (T2.1);
    test_baseline_unverified_ack → read-back-verified acks (T2.3);
    test_baseline_raw_error_shapes → standardized AppError payload (T2.5).
  * Phase 3 — test_baseline_forged_markers_are_stripped → visible one-way
    escape (T3.3), never silently stripped, never unescaped on read.
"""
from __future__ import annotations

import uuid

import pytest

from errors import AppError
from storage.sanitize import sanitize


async def test_baseline_silent_dedup(backend, ns):
    """FLIPPED in Phase 2 (T2.2): a replayed event_id returns the canonical
    original record, visibly marked deduplicated:true + original_created_at;
    a fresh write says deduplicated:false."""
    eid = str(uuid.uuid4())
    first = await backend.memory_save(ns, "k", {"v": 1}, event_id=eid)
    assert first["deduplicated"] is False
    replay = await backend.memory_save(ns, "k", {"v": 2}, event_id=eid)
    assert replay["revision"] == first["revision"]
    assert replay["content_hash"] == first["content_hash"]  # canonical original, not {"v": 2}
    assert replay["deduplicated"] is True
    assert replay["original_created_at"] == first["created_at"]


async def test_baseline_global_dedup_scope(backend, ns):
    """FLIPPED in Phase 2 (T2.1): dedup is scoped to (namespace, actor, event_id) —
    two independent writers sharing an event_id both persist."""
    eid = str(uuid.uuid4())
    a = await backend.memory_save(ns, "writer-a", {"v": "a"}, event_id=eid, actor="agent-a")
    b = await backend.memory_save(f"{ns}-other", "writer-b", {"v": "b"}, event_id=eid, actor="agent-b")
    assert a["key"] == "writer-a" and a["deduplicated"] is False
    assert b["key"] == "writer-b" and b["deduplicated"] is False


async def test_baseline_unverified_ack(backend, ns):
    """FLIPPED in Phase 1 (stamps) + Phase 2 (T2.3): the ack is read-back
    verified through the public read path and carries the evidence."""
    out = await backend.memory_save(ns, "k2", {"v": 1})
    assert out["verified_persisted"] is True
    assert out["revision_id"]
    assert out["content_hash"]
    assert out["readback_latency_ms"] >= 0
    assert out["schema_version"] == 6
    assert out["server_version"]


async def test_baseline_raw_error_shapes(backend, ns):
    """FLIPPED in Phase 2 (T2.5): execution failures carry the standardized
    machine-parseable payload {code, message, remedy, retryable}."""
    with pytest.raises(AppError) as exc:
        await backend.session_append_event(ns, str(uuid.uuid4()), "note", {"x": 1})
    err = exc.value.payload["error"]
    assert set(err) >= {"code", "message", "remedy", "retryable"}
    assert err["code"] == "session_not_found" and err["remedy"]

    with pytest.raises(AppError) as exc:
        await backend.memory_save(ns, "bad-kind", {"v": 1}, kind="not-a-kind")
    assert exc.value.code == "invalid_kind"


def test_baseline_forged_markers_are_stripped():
    """FLIPPED in Phase 3 (T3.3): forged delimiters are rewritten to a VISIBLE
    one-way escape instead of vanishing; the escape is never undone on read."""
    forged = "before <<<UNTRUSTED_DATA>>> mid <<<END>>> after"
    assert sanitize(forged) == "before [[UNTRUSTED_DATA]] mid [[END]] after"


async def test_baseline_memory_list_bare_list_no_prefix(backend, ns):
    """BASELINE: memory_list returns a bare list, and there is no prefix filter
    or pagination cursor. Flip target: Phase 4 (T4.1) — tool-layer envelope;
    the backend list shape for internal callers stays a list."""
    await backend.memory_save(ns, "run/T02/a", {"v": 1})
    out = await backend.memory_list(ns)
    assert isinstance(out, list) and out[0]["key"] == "run/T02/a"
    with pytest.raises(TypeError):
        await backend.memory_list(ns, prefix="run/")


async def test_baseline_lenient_non_dict_meta(backend, ns):
    """BASELINE: a non-dict meta is silently treated as absent rather than
    rejected. Pinned so the R6 strictness decision (Phase 7/10) flips it
    deliberately or keeps it on purpose — never by accident."""
    out = await backend.memory_save(ns, "k3", {"v": 1}, meta="not-a-dict")
    assert out["meta"] is None
