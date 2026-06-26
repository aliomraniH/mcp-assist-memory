"""Phase 2 — the git-free drift detectors: content_hash, coord_health (per
namespace), and coord_drift_scan (store-wide namespace drift)."""
from __future__ import annotations


async def test_content_hash_stable_and_change_detecting(backend, ns):
    a = await backend.memory_save(ns, "k", {"x": 1, "y": 2})
    b = await backend.memory_save(ns, "k2", {"y": 2, "x": 1})  # same fact, key order flipped
    c = await backend.memory_save(ns, "k3", {"x": 1, "y": 3})  # different
    assert a["content_hash"] == b["content_hash"]  # canonical → order-independent
    assert a["content_hash"] != c["content_hash"]


async def test_health_flags_stale_against_latest_repo_sha(backend, ns):
    # An older entry pinned to an old SHA; a newer entry advances the namespace SHA.
    await backend.memory_save(ns, "coord/verify", {"ok": True}, kind="claim",
                              meta={"repo_sha": "e87f91c9"})
    await backend.memory_save(ns, "coord/head", {"note": "now"}, meta={"repo_sha": "3ab6d4a"})
    health = await backend.coord_health(ns)
    assert health["latest_repo_sha"] == "3ab6d4a"
    stale_keys = {s["key"] for s in health["stale"]}
    assert "coord/verify" in stale_keys
    assert "coord/head" not in stale_keys


async def test_health_flags_duplicate_content(backend, ns):
    await backend.memory_save(ns, "a", {"fact": "same"})
    await backend.memory_save(ns, "b", {"fact": "same"})
    await backend.memory_save(ns, "c", {"fact": "different"})
    dup = (await backend.coord_health(ns))["duplicate_content"]
    assert len(dup) == 1
    assert sorted(dup[0]["keys"]) == ["a", "b"]


async def test_health_flags_claim_collisions_by_subject(backend, ns):
    await backend.memory_save(ns, "merged-by-web", {"merged": True}, kind="claim",
                              meta={"pr": 11})
    await backend.memory_save(ns, "merged-by-cli", {"merged": False}, kind="claim",
                              meta={"pr": 11})
    await backend.memory_save(ns, "unrelated", {"x": 1}, kind="claim", meta={"pr": 99})
    coll = (await backend.coord_health(ns))["claim_collisions"]
    assert len(coll) == 1
    assert coll[0]["subject"] == "pr:11"
    assert sorted(coll[0]["keys"]) == ["merged-by-cli", "merged-by-web"]


async def test_tombstoned_entry_drops_out_of_health(backend, ns):
    await backend.memory_save(ns, "a", {"fact": "same"})
    await backend.memory_save(ns, "b", {"fact": "same"})
    await backend.memory_delete(ns, "b")
    dup = (await backend.coord_health(ns))["duplicate_content"]
    assert dup == []  # only one live copy left


async def test_drift_scan_finds_same_fact_across_namespaces(backend):
    import uuid
    ns1 = f"proj-test-{uuid.uuid4().hex[:12]}"
    ns2 = f"proj-test-{uuid.uuid4().hex[:12]}"
    # A marker unique to this run makes the content_hash unique, so the store-wide
    # scan is isolated from other entries in the shared test DB.
    shared = {"plugin": "glp1", "phase": 1, "marker": uuid.uuid4().hex}
    await backend.memory_save(ns1, "state", shared)
    await backend.memory_save(ns2, "state", shared)
    await backend.memory_save(ns1, "local-only", {"x": 1})

    scan = await backend.coord_drift_scan(limit=500)
    hit = [d for d in scan["suspected_namespace_drift"]
           if set(d["namespaces"]) >= {ns1, ns2}]
    assert hit, "the fact shared across ns1/ns2 should be flagged as drift"
    assert sorted(hit[0]["entries"]) == sorted([f"{ns1}/state", f"{ns2}/state"])
