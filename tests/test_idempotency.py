"""event_id gives exactly-once writes — the Phase 2 reconciliation guarantee."""
from __future__ import annotations

import uuid


async def test_same_event_id_applied_once(backend, ns):
    eid = str(uuid.uuid4())
    first = await backend.memory_save(ns, "k", "v1", event_id=eid)
    second = await backend.memory_save(ns, "k", "v2-ignored", event_id=eid)
    # Second save is a no-op returning the already-applied revision.
    assert first["revision"] == 1
    assert second["revision"] == 1
    hist = await backend.memory_history(ns, "k")
    assert len(hist) == 1
    assert "v1" in str(hist[0]["value"])


async def test_distinct_event_ids_both_apply(backend, ns):
    await backend.memory_save(ns, "k", "v1", event_id=str(uuid.uuid4()))
    await backend.memory_save(ns, "k", "v2", event_id=str(uuid.uuid4()))
    assert len(await backend.memory_history(ns, "k")) == 2
