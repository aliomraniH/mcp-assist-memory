"""Coordination provenance (0003): the `meta` envelope is projected into indexed
columns and surfaced on reads, the new claim/knowledge kinds are accepted, and
writes without `meta` behave exactly as before (backward compatible)."""
from __future__ import annotations

ENVELOPE = {
    "repo_sha": "3ab6d4a",
    "base_sha": "61a0f55",
    "branch": "main",
    "dirty": False,
    "session_id": "b75f7982-aaaa-bbbb-cccc-ddddeeeeffff",
    "surface": "claude-code-web",   # extra key — kept in meta, not a column
    "pr": 11,
}


async def test_meta_projected_into_columns_and_returned(backend, ns):
    saved = await backend.memory_save(ns, "coord/x", {"v": 1}, kind="claim", meta=ENVELOPE)
    for field in ("repo_sha", "base_sha", "branch", "session_id"):
        assert saved[field] == ENVELOPE[field]
    assert saved["dirty"] is False
    # Full envelope kept losslessly, including keys that aren't columns.
    assert saved["meta"]["pr"] == 11
    assert saved["meta"]["surface"] == "claude-code-web"

    got = await backend.memory_get(ns, "coord/x")
    assert got["repo_sha"] == "3ab6d4a"
    assert got["meta"]["pr"] == 11


async def test_no_meta_is_backward_compatible(backend, ns):
    saved = await backend.memory_save(ns, "k", {"v": 1})
    assert saved["repo_sha"] is None
    assert saved["session_id"] is None
    assert saved["meta"] is None
    got = await backend.memory_get(ns, "k")
    assert got["meta"] is None
    assert got["value"]  # still readable


async def test_claim_and_knowledge_kinds_accepted(backend, ns):
    c = await backend.memory_save(ns, "claim-k", "merged?", kind="claim")
    k = await backend.memory_save(ns, "know-k", "sdk fields", kind="knowledge")
    assert c["kind"] == "claim"
    assert k["kind"] == "knowledge"
    kinds = {e["key"]: e["kind"] for e in await backend.memory_list(ns)}
    assert kinds == {"claim-k": "claim", "know-k": "knowledge"}


async def test_handoff_carries_meta(backend, ns):
    saved = await backend.handoff_save(ns, "h", {"summary": "did X"}, meta={"repo_sha": "deadbee"})
    assert saved["repo_sha"] == "deadbee"
    loaded = await backend.handoff_load(ns, "h")
    assert loaded["repo_sha"] == "deadbee"


async def test_non_dict_meta_is_ignored_not_fatal(backend, ns):
    # A malformed envelope must never fail the write — it degrades to no provenance.
    saved = await backend.memory_save(ns, "k", "v", meta="not-a-dict")  # type: ignore[arg-type]
    assert saved["meta"] is None
    assert saved["repo_sha"] is None


async def test_new_revision_can_update_provenance(backend, ns):
    await backend.memory_save(ns, "coord/verify", {"ok": True}, kind="claim",
                              meta={"repo_sha": "e87f91c9"})
    r2 = await backend.memory_save(ns, "coord/verify", {"ok": True}, kind="claim",
                                   meta={"repo_sha": "3ab6d4a"})
    assert r2["revision"] == 2
    assert r2["repo_sha"] == "3ab6d4a"
