"""v3 P0/1 item 3 (interim) — dedup replays escalate to a TOP-LEVEL non-success
indicator instead of a buried deduplicated:true.

Motivating shape: the skill-transfer T02 collision — a writer replayed an
event_id with a DIFFERENT payload and got back what looked like a clean success
ack for content it never wrote (phantom ack). The replay echo must be
unmistakable at the top level of the envelope.
"""
from __future__ import annotations

import uuid


async def test_t02_collision_replay_is_visibly_non_success(backend, ns):
    """The T02 shape: same (namespace, actor, event_id), different payload."""
    eid = str(uuid.uuid4())
    first = await backend.memory_save(ns, "run/T02/step", {"result": "pass"},
                                      event_id=eid, actor="skill-runner")
    assert first["deduplicated"] is False
    assert first.get("status") != "deduplicated_replay"

    replay = await backend.memory_save(ns, "run/T02/step", {"result": "FAIL"},
                                       event_id=eid, actor="skill-runner")
    # Top-level, single-field check — the escalation this item is about.
    assert replay["status"] == "deduplicated_replay"
    assert replay["deduplicated"] is True
    # And the echo is the ORIGINAL record, not the colliding payload.
    assert replay["content_hash"] == first["content_hash"]
    assert replay["original_created_at"] == first["created_at"]


async def test_racing_duplicate_event_id_also_escalates(backend, ns):
    eid = str(uuid.uuid4())
    await backend.memory_save(ns, "k", {"v": 1}, event_id=eid)
    replay = await backend.memory_save(ns, "k", {"v": 1}, event_id=eid)
    assert replay["status"] == "deduplicated_replay"


async def test_session_event_replay_escalates_too(backend, ns):
    s = await backend.session_create(ns)
    eid = str(uuid.uuid4())
    first = await backend.session_append_event(
        ns, s["session_id"], "tool_call", {"t": 1}, actor="runner", event_id=eid)
    assert first["deduplicated"] is False and first.get("status") != "deduplicated_replay"
    replay = await backend.session_append_event(
        ns, s["session_id"], "tool_call", {"t": 2}, actor="runner", event_id=eid)
    assert replay["status"] == "deduplicated_replay" and replay["deduplicated"] is True
