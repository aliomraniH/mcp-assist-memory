"""v3 P0/1 item 4 — idempotency fingerprint (S2 draft rev -07 + S3 RFC 8785).

sha256 over the JCS canonicalization of (tool, namespace, key, kind, payload,
meta), computed at the API boundary over the incoming payload (never a jsonb
round-trip). Same token + same fingerprint → visible dedup with the original
result; same token + different fingerprint → idempotency_conflict (the draft's
422 payload-mismatch case, MUST-NOT reuse); new token → normal write.
"""
from __future__ import annotations

import uuid

import pytest

from errors import AppError
from storage.idempotency import idem_fingerprint


# ------------------------------------------------------------- the fingerprint
def test_fingerprint_is_key_order_independent_and_type_sensitive():
    base = dict(tool="memory_save", namespace="p", key="k", kind="note", meta=None)
    a = idem_fingerprint(payload={"x": 1, "y": [1, 2]}, **base)
    b = idem_fingerprint(payload={"y": [1, 2], "x": 1}, **base)  # reordered keys
    assert a == b  # JCS canonical order, not Python dict order
    c = idem_fingerprint(payload={"x": 1, "y": [2, 1]}, **base)  # different content
    assert c != a
    # Every identity component participates.
    assert idem_fingerprint(payload={"x": 1, "y": [1, 2]},
                            **{**base, "kind": "claim"}) != a
    assert idem_fingerprint(payload={"x": 1, "y": [1, 2]},
                            **{**base, "tool": "handoff_save"}) != a


def test_nan_and_infinity_hard_error_never_skip():
    base = dict(tool="memory_save", namespace="p", key="k", kind="note", meta=None)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(AppError) as err:
            idem_fingerprint(payload={"x": bad}, **base)
        assert err.value.code == "unrepresentable_number"


def test_integer_beyond_2_53_documented_round_trip():
    """RFC 8785 numbers are IEEE-754 doubles: ints beyond 2^53 cannot round-trip
    and MUST be sent as JSON strings in fingerprinted payloads. The server
    rejects the raw int with the remedy saying exactly that; the string form
    fingerprints fine and round-trips exactly."""
    base = dict(tool="memory_save", namespace="p", key="k", kind="note", meta=None)
    with pytest.raises(AppError) as err:
        idem_fingerprint(payload={"n": 2**53 + 1}, **base)
    assert err.value.code == "unrepresentable_number"
    assert "JSON string" in (err.value.remedy or "") or "strings" in (err.value.remedy or "")
    assert idem_fingerprint(payload={"n": str(2**53 + 1)}, **base)  # the documented form
    # The largest safe integer (2^53 - 1) is still exactly representable; the
    # compliant library draws the line at 2^53 itself (not round-trip-safe).
    assert idem_fingerprint(payload={"n": 2**53 - 1}, **base)
    with pytest.raises(AppError):
        idem_fingerprint(payload={"n": 2**53}, **base)


# ------------------------------------------------------------- write semantics
async def test_same_token_same_payload_dedups_with_original_result(backend, ns):
    eid = str(uuid.uuid4())
    first = await backend.memory_save(ns, "k", {"v": 1, "w": 2}, event_id=eid)
    assert first["idem_fingerprint"]
    # Reordered dict keys = same JCS identity: dedup, not conflict.
    replay = await backend.memory_save(ns, "k", {"w": 2, "v": 1}, event_id=eid)
    assert replay["status"] == "deduplicated_replay"
    assert replay["revision"] == first["revision"]
    history = await backend.memory_history(ns, "k")
    assert len(history) == 1  # zero new revisions


async def test_t02_shape_same_token_different_payload_is_conflict_not_phantom_ack(backend, ns):
    eid = str(uuid.uuid4())
    await backend.memory_save(ns, "run/T02/step", {"result": "pass"},
                              event_id=eid, actor="skill-runner")
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "run/T02/step", {"result": "FAIL"},
                                  event_id=eid, actor="skill-runner")
    assert err.value.code == "idempotency_conflict"
    history = await backend.memory_history(ns, "run/T02/step")
    assert len(history) == 1  # nothing was written for the colliding payload


async def test_different_meta_is_a_different_payload(backend, ns):
    eid = str(uuid.uuid4())
    await backend.memory_save(ns, "k", {"v": 1}, event_id=eid,
                              meta={"branch": "main"})
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1}, event_id=eid,
                                  meta={"branch": "other"})
    assert err.value.code == "idempotency_conflict"


async def test_new_token_writes_normally(backend, ns):
    await backend.memory_save(ns, "k", {"v": 1}, event_id=str(uuid.uuid4()))
    out = await backend.memory_save(ns, "k", {"v": 2}, event_id=str(uuid.uuid4()))
    assert out["deduplicated"] is False and out["revision"] == 2


async def test_legacy_rows_without_fingerprint_still_dedup(backend, ns):
    eid = str(uuid.uuid4())
    first = await backend.memory_save(ns, "k", {"v": 1}, event_id=eid)
    async with backend.pool.connection() as conn:  # simulate a pre-0007 row
        await conn.execute(
            "UPDATE memory_entry SET idem_fingerprint = NULL WHERE namespace = %s AND key = %s",
            (ns, "k"))
    replay = await backend.memory_save(ns, "k", {"v": "changed"}, event_id=eid)
    assert replay["status"] == "deduplicated_replay"  # fail open to visible dedup
    assert replay["content_hash"] == first["content_hash"]


async def test_writes_without_event_id_are_not_fingerprinted(backend, ns):
    out = await backend.memory_save(ns, "k", {"v": 1})
    assert out["idem_fingerprint"] is None
    # NaN without an idempotency key never reaches the fingerprint path (the
    # jsonb layer has its own opinion; this asserts no NEW gate was added).


# ------------------------------------------------------- curator replay (both levels)
async def test_byte_identical_curator_replay_writes_zero_new_revisions(curate_backend, ns):
    s = await curate_backend.session_create(ns)
    sid = s["session_id"]
    curate_backend.curator.result = {"operations": [
        {"op": "ADD", "key": "lesson/x", "kind": "note", "value": {"lesson": "L"}},
    ]}
    first = await curate_backend.coord_curate(ns, sid)
    assert first["counts"]["added"] == 1
    replay = await curate_backend.coord_curate(ns, sid)  # byte-identical re-apply
    assert replay["counts"]["added"] == 1  # the dedup echo, visibly marked...
    history = await curate_backend.memory_history(ns, "lesson/x")
    assert len(history) == 1  # ...but ZERO new revisions


async def test_curator_replay_with_changed_content_surfaces_conflict_per_op(curate_backend, ns):
    s = await curate_backend.session_create(ns)
    sid = s["session_id"]
    curate_backend.curator.result = {"operations": [
        {"op": "ADD", "key": "lesson/x", "kind": "note", "value": {"lesson": "L"}},
    ]}
    await curate_backend.coord_curate(ns, sid)
    curate_backend.curator.result = {"operations": [
        {"op": "ADD", "key": "lesson/x", "kind": "note", "value": {"lesson": "DIFFERENT"}},
    ]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["counts"]["idempotency_conflict"] == 1
    assert out["idempotency_conflicts"][0]["key"] == "lesson/x"
    history = await curate_backend.memory_history(ns, "lesson/x")
    assert len(history) == 1  # the colliding content was never written
