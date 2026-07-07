"""Curator (write-side LLM curation) tests. Real Postgres, fake LLM: the
``curate_backend`` fixture injects a FakeCurator whose ``.result`` the apply-worker
consumes — there is never a live Anthropic call in the suite. Mirrors
``test_reconcile.py``: drive the deterministic apply path through canned operations."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _seed_session(backend, ns):
    s = await backend.session_create(ns, surface="cli")
    sid = s["session_id"]
    await backend.session_append_event(ns, sid, "start", {"task": "do the thing"})
    await backend.session_append_event(ns, sid, "finish", {"result": "ok"})
    return sid


async def test_add_creates_live_entry_with_scores(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "knowledge/lesson", "kind": "knowledge",
        "value": {"lesson": "prefer X over Y"}, "abstraction": "lesson",
        "salience": 8, "confidence": 0.9, "subjects": ["module:foo"], "tags": ["procedure"],
        "embeddings": {"summary": "prefer X over Y when Z", "hyde": "when should I use X over Y?"},
    }]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["curator_enabled"] is True
    assert out["counts"]["added"] == 1

    entry = await curate_backend.memory_get(ns, "knowledge/lesson")
    assert entry is not None
    assert entry["kind"] == "knowledge"
    assert entry["salience"] == 8
    assert entry["confidence"] == pytest.approx(0.9)
    assert entry["meta"]["subjects"] == ["module:foo"]
    # Both embedding legs are populated when an embedder is wired in.
    from psycopg.rows import dict_row
    async with curate_backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT embedding IS NOT NULL AS s, hyde_embedding IS NOT NULL AS h "
            "FROM memory_entry WHERE namespace=%s AND key=%s ORDER BY revision DESC LIMIT 1",
            (ns, "knowledge/lesson"),
        )
        row = await cur.fetchone()
    assert row["s"] is True and row["h"] is True


async def test_supersede_sets_boundary_keeps_history(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    # Pre-existing decision that the curator will supersede.
    await curate_backend.memory_save(ns, "decision/curator-sync",
                                     {"decision": "sync gate"}, kind="decision")
    curate_backend.curator.result = {"operations": [{
        "op": "SUPERSEDE", "key": "decision/curator-async", "kind": "decision",
        "value": {"decision": "async consolidation"}, "salience": 9, "confidence": 0.9,
        "supersedes": "decision/curator-sync",
        "embeddings": {"summary": "curator runs async", "hyde": "sync or async curator?"},
    }]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["counts"]["superseded"] == 1

    # Old key is no longer the live latest...
    assert await curate_backend.memory_get(ns, "decision/curator-sync") is None
    # ...but its history is preserved (nothing hard-deleted).
    hist = await curate_backend.memory_history(ns, "decision/curator-sync")
    assert len(hist) >= 2
    # The new entry is live.
    assert await curate_backend.memory_get(ns, "decision/curator-async") is not None


async def test_noop_writes_nothing_returns_reason(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [
        {"op": "NOOP", "reason": "phi-risk: generalize instead", "subjects": ["module:parser"]},
    ]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["counts"]["noop"] == 1
    assert out["counts"]["added"] == 0
    assert out["noops"][0]["reason"].startswith("phi-risk")
    assert await curate_backend.memory_list(ns) == []


async def test_phi_gate_fails_closed(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "note/contact", "kind": "note",
        "value": {"text": "reach patient at john.doe@example.com"},
    }]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["counts"]["phi_dropped"] == 1
    assert out["counts"]["added"] == 0
    assert await curate_backend.memory_get(ns, "note/contact") is None


async def test_claim_without_provenance_downgraded(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "claim/unverifiable", "kind": "claim",
        "value": {"summary": "PR landed"},  # no meta.repo + pr/branch
    }]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["counts"]["downgraded"] == 1
    entry = await curate_backend.memory_get(ns, "claim/unverifiable")
    assert entry["kind"] == "note"
    assert "claim-downgraded" in entry["tags"]


async def test_claim_with_provenance_stays_claim(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "claim/verifiable", "kind": "claim",
        "value": {"summary": "PR #7 merged"},
        "meta": {"repo": "owner/repo", "pr": 7, "branch": "main"},
    }]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["counts"]["added"] == 1
    assert out["counts"]["downgraded"] == 0
    entry = await curate_backend.memory_get(ns, "claim/verifiable")
    assert entry["kind"] == "claim"


async def test_idempotent_double_curate(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "knowledge/once", "kind": "knowledge",
        "value": {"lesson": "only once"},
    }]}
    await curate_backend.coord_curate(ns, sid)
    await curate_backend.coord_curate(ns, sid)
    # event_id gate holds: exactly one revision despite two applies.
    hist = await curate_backend.memory_history(ns, "knowledge/once")
    assert len(hist) == 1


async def test_dry_run_writes_nothing(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "knowledge/dry", "kind": "knowledge",
        "value": {"lesson": "do not persist"},
    }]}
    out = await curate_backend.coord_curate(ns, sid, dry_run=True)
    assert out["dry_run"] is True
    assert len(out["operations"]) == 1
    assert "counts" not in out  # nothing applied
    assert await curate_backend.memory_get(ns, "knowledge/dry") is None


async def test_phi_gate_drops_numeric_identifier(curate_backend, ns):
    # A raw numeric MRN-shaped value (no PHI-named key, not a string) must still trip
    # the fail-closed gate — numeric scalars are stringified and long-digit-checked.
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "note/record", "kind": "note",
        "value": {"record": 123456789}, "meta": {"account": 987654321},
    }]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["counts"]["phi_dropped"] == 1
    assert out["counts"]["added"] == 0
    assert await curate_backend.memory_get(ns, "note/record") is None


async def test_superseded_row_excluded_from_coord_health(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    # Two distinct keys holding the SAME fact would be a duplicate_content collision...
    await curate_backend.memory_save(ns, "decision/old", {"decision": "same fact"}, kind="decision")
    curate_backend.curator.result = {"operations": [{
        "op": "SUPERSEDE", "key": "decision/new", "kind": "decision",
        "value": {"decision": "same fact"}, "supersedes": "decision/old",
    }]}
    await curate_backend.coord_curate(ns, sid)
    health = await curate_backend.coord_health(ns)
    # ...but once "decision/old" is superseded it must not appear as live in the report.
    flagged = {k for group in health.get("duplicate_content", []) for k in group["keys"]}
    assert "decision/old" not in flagged


async def test_disabled_curator_is_clean_noop(curate_backend, ns):
    sid = await _seed_session(curate_backend, ns)
    curate_backend.curator.enabled = False
    curate_backend.curator.result = {"operations": [{
        "op": "ADD", "key": "knowledge/never", "kind": "knowledge",
        "value": {"lesson": "should never be written"},
    }]}
    out = await curate_backend.coord_curate(ns, sid)
    assert out["curator_enabled"] is False
    assert out["curator_status"] == "disabled"
    assert out["operations"] == []
    assert await curate_backend.memory_get(ns, "knowledge/never") is None
    # The curator is never even consulted when disabled.
    assert curate_backend.curator.calls == []


async def test_curator_status_distinguishes_noop_from_error(curate_backend, ns):
    """Finding 3: an empty operations list is no longer ambiguous. A deliberate
    NOOP (model ran, chose to persist nothing) surfaces curator_status='ok'; a
    fail-closed model failure surfaces 'error' + a structural curator_error.
    Both write nothing (fail-closed either way)."""
    sid = await _seed_session(curate_backend, ns)

    # deliberate NOOP — the model succeeded and returned zero operations.
    curate_backend.curator.result = {"operations": [], "curator_status": "ok"}
    noop = await curate_backend.coord_curate(ns, sid)
    assert noop["curator_status"] == "ok"
    assert noop["operations"] == []
    assert "curator_error" not in noop

    # swallowed model failure — also zero operations, but NOT a deliberate NOOP.
    curate_backend.curator.result = {
        "operations": [], "curator_status": "error", "curator_error": "RateLimitError"}
    err = await curate_backend.coord_curate(ns, sid)
    assert err["curator_status"] == "error"
    assert err["curator_error"] == "RateLimitError"
    assert err["operations"] == []  # fail-closed: still writes nothing
    assert await curate_backend.memory_list(ns) == []
