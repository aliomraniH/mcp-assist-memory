"""Behavioral round-trips for the tool surface — assert values, not types."""
from __future__ import annotations

import base64

from storage.sanitize import UNTRUSTED_OPEN


async def test_memory_save_then_get_returns_value(backend, ns):
    await backend.memory_save(ns, "plan", {"step": "build phase 0"}, kind="decision", tags=["a"])
    got = await backend.memory_get(ns, "plan")
    assert got["revision"] == 1
    assert got["kind"] == "decision"
    assert got["value"]["step"].startswith(UNTRUSTED_OPEN)  # wrapped on read
    assert "build phase 0" in got["value"]["step"]


async def test_memory_save_increments_revision(backend, ns):
    await backend.memory_save(ns, "k", "v1")
    second = await backend.memory_save(ns, "k", "v2")
    assert second["revision"] == 2
    assert (await backend.memory_get(ns, "k"))["revision"] == 2


async def test_memory_list_returns_latest_per_key(backend, ns):
    await backend.memory_save(ns, "a", "1")
    await backend.memory_save(ns, "a", "2")
    await backend.memory_save(ns, "b", "x")
    listed = {e["key"]: e["revision"] for e in await backend.memory_list(ns)}
    assert listed == {"a": 2, "b": 1}


async def test_memory_history_includes_all_revisions(backend, ns):
    await backend.memory_save(ns, "k", "1")
    await backend.memory_save(ns, "k", "2")
    hist = await backend.memory_history(ns, "k")
    assert [h["revision"] for h in hist] == [2, 1]


async def test_memory_delete_tombstones(backend, ns):
    await backend.memory_save(ns, "k", "v")
    await backend.memory_delete(ns, "k")
    assert await backend.memory_get(ns, "k") is None
    assert await backend.memory_history(ns, "k")  # history preserved


async def test_memory_search_finds_substring(backend, ns):
    await backend.memory_save(ns, "k", {"note": "rollout schedule"})
    hits = await backend.memory_search(ns, "rollout")
    assert any("rollout" in str(h["value"]) for h in hits)


async def test_handoff_round_trip_across_surfaces(backend, ns):
    key = f"handoff-{base64.b16encode(b'x').decode()}"
    await backend.handoff_save(ns, key, {"summary": "did X"}, source_surface="web")
    loaded = await backend.handoff_load(ns, key)
    assert "did X" in str(loaded["value"])
    assert loaded["source_surface"] == "web"
    assert any(h["key"] == key for h in await backend.handoff_list(ns))


async def test_session_events_in_order(backend, ns):
    s = await backend.session_create(ns, surface="cli")
    sid = s["session_id"]
    await backend.session_append_event(ns, sid, "start", {"n": 1})
    await backend.session_append_event(ns, sid, "step", {"n": 2})
    evs = await backend.session_events(ns, sid)
    assert [e["seq"] for e in evs] == [1, 2]
    got = await backend.session_get(ns, sid)
    assert got["surface"] == "cli"
    assert got["namespace"] == ns
    assert any(x["session_id"] == sid for x in await backend.session_list(ns))


async def test_artifact_put_get_list(backend):
    data = b"hello-artifact-bytes"
    put = await backend.artifact_put(data, content_type="text/plain")
    got = await backend.artifact_get(put["sha256"])
    assert got["size"] == len(data)
    read = await backend.artifact_read_range(put["sha256"], 0, len(data))
    assert read == data


async def test_stats_counts(backend, ns):
    await backend.memory_save(ns, "k", "v")
    s = await backend.stats()
    assert s["memory_revisions"] >= 1
