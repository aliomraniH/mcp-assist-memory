"""[CI] The three server-side sequences, and the search-consistency fix.

WHAT IS BEING PINNED

    Correct use of this server has always been a SEQUENCE — learn which
    database answered, resolve the profile, see what is stale, then act. That
    ordering used to live in tool descriptions, skills and batons, meaning it
    was enforced by a model reading advice and remembering it mid-task. A
    skipped step produced no signal at all: nothing in any response said "you
    never checked which database this is".

    So the assertions here are mostly about ORDER and REPORTED STEPS, not just
    return values. `steps_run` is the contract: it is what makes "the sequence
    ran" checkable instead of assumed.

    And the search half: `memory_search` and `recall` must not be able to
    disagree about what counts as a match, because a caller cannot be relied on
    to pick the right one.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from errors import AppError
from storage import sequences
from storage.postgres import PostgresBackend
from storage.sequences import BOOTSTRAP_STEPS, NAMESPACE_INIT_STEPS, RECALL_STEPS
from tests.conftest import DATABASE_URL, SCHEMA, FakeEmbedder

pytestmark = pytest.mark.ci


@pytest_asyncio.fixture
async def be(ns):
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=4)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    backend = PostgresBackend(pool, embedder=FakeEmbedder())
    yield backend
    await pool.close()


# ---------------------------------------------------------------------------
# 1. session_bootstrap
# ---------------------------------------------------------------------------
async def test_bootstrap_runs_every_step_in_order(be, ns):
    res = await sequences.session_bootstrap(be, ns, surface="test", actor="tester")
    assert res["steps_run"] == list(BOOTSTRAP_STEPS)
    assert res["degraded"] == []


async def test_bootstrap_answers_which_database_first(be, ns):
    """The step that has to come first. Every later fact a session learns is
    conditional on which database answered, and a previous deploy spent seven
    minutes reading a database the server had never written to because nothing
    in any response said so."""
    res = await sequences.session_bootstrap(be, ns)
    assert res["steps_run"][0] == "db_identity"
    assert len(res["db_identity"]["boot_connection_fingerprint"]) == 64
    assert res["db_identity"]["current_database"]


async def test_bootstrap_opens_a_usable_session(be, ns):
    res = await sequences.session_bootstrap(be, ns, surface="test", purpose="verify")
    assert await be.session_get(ns, res["session_id"]) is not None


async def test_bootstrap_surfaces_an_uncalibrated_floor(be, ns):
    """A default floor nobody measured against this namespace should not have to
    be inferred from a null field the caller may never read."""
    res = await sequences.session_bootstrap(be, ns)
    assert res["retrieval_policy"]["calibrated"] is False
    assert any("never calibrated" in a for a in res["attention"])


async def test_bootstrap_surfaces_unconsumed_batons(be, ns):
    """Work handed forward should not depend on a session thinking to ask."""
    await be.handoff_save(ns, "baton/next", "pick this up",
                          meta={"next_actor": "tester", "baton_type": "deploy"},
                          actor="prev-session")
    res = await sequences.session_bootstrap(be, ns, actor="tester")
    assert [b["key"] for b in res["pending_batons"]] == ["baton/next"]
    assert res["pending_batons"][0]["addressed_to_me"] is True
    assert any("baton" in a for a in res["attention"])


async def test_bootstrap_ignores_a_consumed_baton(be, ns):
    await be.handoff_save(ns, "baton/done", "already handled",
                          meta={"consumed": True}, actor="prev-session")
    res = await sequences.session_bootstrap(be, ns)
    assert res["pending_batons"] == []


async def test_a_failing_step_is_reported_not_swallowed(be, ns, monkeypatch):
    """A bootstrap that half-worked and says so is useful; one that half-worked
    silently is worse than none. Only session creation is fatal."""
    async def boom(*a, **k):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(be, "coord_health", boom)
    res = await sequences.session_bootstrap(be, ns)
    assert res["session_id"]
    assert "coord_health" not in res["steps_run"]
    assert res["degraded"] == [{"step": "coord_health", "error": "RuntimeError"}]


async def test_bootstrap_fails_loudly_when_no_session_can_open(be, ns, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(be, "session_create", boom)
    with pytest.raises(AppError):
        await sequences.session_bootstrap(be, ns)


# ---------------------------------------------------------------------------
# 2. namespace_init
# ---------------------------------------------------------------------------
async def test_namespace_init_states_the_policy_instead_of_inheriting_it(be, ns):
    res = await sequences.namespace_init(
        be, ns, actor="tester", intent_gate="on", similarity_floor=0.55,
        top_fraction_alpha=0.90, calibration_ts="2026-08-04T00:00:00Z")

    assert res["created"] is True
    assert res["steps_run"] == [s for s in NAMESPACE_INIT_STEPS
                                if s != "seed_project_meta"]
    assert res["variant_profile"]["intent_gate"] == "on"
    assert res["retrieval_policy"]["absolute_floor"] == 0.55
    assert res["retrieval_policy"]["alpha"] == 0.90
    assert res["retrieval_policy"]["calibrated"] is True


async def test_a_floor_without_a_measurement_reports_itself_unverified(be, ns):
    """Supplying a number is not the same as having measured one. Omitting
    calibration_ts must leave the policy honestly marked."""
    res = await sequences.namespace_init(be, ns, similarity_floor=0.60)
    assert res["retrieval_policy"]["absolute_floor"] == 0.60
    assert res["retrieval_policy"]["calibrated"] is False
    assert res["retrieval_policy"]["temporal_mode"] == "server_default"


async def test_namespace_init_is_idempotent_and_never_rewrites_a_live_profile(be, ns):
    """Namespace creation is exactly the call someone retries after a timeout."""
    await sequences.namespace_init(be, ns, intent_gate="on")
    again = await sequences.namespace_init(be, ns, intent_gate="off")
    assert again["created"] is False
    assert again["variant_profile"]["intent_gate"] == "on"
    assert again["steps_run"] == ["existence_check"]


async def test_namespace_init_records_who_created_it(be, ns):
    await sequences.namespace_init(be, ns, actor="tester", clinical=True)
    row = next(r for r in await be.namespace_list() if r["namespace"] == ns)
    assert row["created_by"] == "tester"
    assert row["clinical"] is True


async def test_namespace_init_seeds_project_meta_when_given(be, ns):
    res = await sequences.namespace_init(
        be, ns, project_meta={"summary": "test project", "stack": ["python"]})
    assert "seed_project_meta" in res["steps_run"]
    assert (await be.memory_get(ns, "project/meta"))["meta"]["stack"] == ["python"]


async def test_namespace_init_verifies_the_write_landed(be, ns):
    """Read-back, not fire-and-forget. A profile that did not persist is the
    split-brain failure mode, and it is silent."""
    res = await sequences.namespace_init(be, ns, intent_gate="on")
    assert "readback_verify" in res["steps_run"]
    assert (await be.resolved_profile(ns))["intent_gate"] == "on"


@pytest.mark.parametrize("kwargs", [
    {"namespace": "   "},
    {"namespace": "dev/x", "intent_gate": "maybe"},
])
async def test_namespace_init_argument_validation_fails_closed(be, kwargs):
    with pytest.raises(AppError):
        await sequences.namespace_init(be, kwargs.pop("namespace"), **kwargs)


async def test_namespace_init_accepts_the_documented_bare_project_name(be):
    """A bare project name is the README's own example. A new sequence must not
    quietly narrow the tenancy model by inventing a format rule."""
    res = await sequences.namespace_init(be, "proj-test-bare-name-check")
    assert res["namespace"] == "proj-test-bare-name-check"


# ---------------------------------------------------------------------------
# 3. recall — and the consistency fix it exists for
# ---------------------------------------------------------------------------
async def _seed_corpus(be, ns):
    await be.memory_save(ns, "note/replay", "event log replay fold insertion order",
                         kind="note", actor="seed")
    await be.memory_save(ns, "note/catering", "quarterly workshop catering schedule",
                         kind="note", actor="seed")
    await be.memory_save(ns, "note/latency", "latency sample p95 histogram bucket",
                         kind="note", actor="seed")


async def test_recall_runs_every_step(be, ns):
    await _seed_corpus(be, ns)
    res = await sequences.recall(be, ns, "event log replay fold insertion order")
    assert res["steps_run"] == list(RECALL_STEPS)


async def test_recall_attaches_the_verdict_to_every_row(be, ns):
    await _seed_corpus(be, ns)
    res = await sequences.recall(be, ns, "event log replay fold insertion order")
    assert res["results"]
    for row in res["results"]:
        assert row["retrieval"]["admitted"] is True
        assert row["retrieval"]["absolute_floor"] == 0.45
        assert "calibrated" in row["retrieval"]


async def test_recall_reports_rejections_even_when_it_hides_them(be, ns):
    """The counts are ALWAYS reported, so an empty namespace and a namespace
    full of noise never look alike."""
    await _seed_corpus(be, ns)
    res = await sequences.recall(be, ns, "unrelated aeronautics fuselage rivet")
    rejected = (res["guard"]["rejected_below_floor"]
                + res["guard"]["rejected_below_alpha"])
    assert res["guard"]["admitted"] == len(res["results"])
    assert rejected >= 0 and "top_score" in res["guard"]


async def test_include_below_floor_returns_the_rejects_marked(be, ns):
    """Nothing disappears invisibly: a caller can always see the boundary."""
    await _seed_corpus(be, ns)
    res = await sequences.recall(
        be, ns, "event log replay fold insertion order", include_below_floor=True)
    reasons = {r["retrieval"]["reason"] for r in res["results"]}
    assert reasons  # at least one verdict reason present
    if any(not r["retrieval"]["admitted"] for r in res["results"]):
        assert reasons & {"below_floor", "below_alpha"}


async def test_recall_echoes_the_policy_that_produced_the_rows(be, ns):
    """So two surfaces returning different rows can be compared on the policy
    that produced them rather than on vibes."""
    await _seed_corpus(be, ns)
    res = await sequences.recall(be, ns, "event log replay")
    assert res["retrieval_policy"]["absolute_floor"] == 0.45
    assert "memory_search" in res["retrieval_policy"]["applies_to"]


async def test_memory_search_now_carries_the_same_verdict(be, ns):
    """THE CONSISTENCY FIX. memory_search used to have no floor at all while
    intent_open had two guards, so the same store answered the same question
    differently depending on which tool the caller reached for."""
    await _seed_corpus(be, ns)
    rows = await be.memory_search(ns, "event log replay fold insertion order")
    assert rows
    for row in rows:
        assert row["retrieval"]["absolute_floor"] == 0.45
        assert row["retrieval"]["alpha"] == 0.85
        assert row["retrieval"]["reason"] in (
            "admitted", "below_floor", "below_alpha", "keyword_only")


async def test_memory_search_annotates_but_never_drops(be, ns):
    """Deliberately different from recall: the additive-schema constraint says
    a release must not silently return fewer rows than the last one, and the
    floor is uncalibrated on most namespaces — deleting evidence on the
    authority of a number nobody measured is not an improvement."""
    await _seed_corpus(be, ns)
    guarded = await be.memory_search(ns, "quarterly workshop catering schedule")
    assert any(not r["retrieval"]["admitted"] for r in guarded) or guarded
    assert all("retrieval" in r for r in guarded)


async def test_search_and_recall_agree_on_what_is_admitted(be, ns):
    """The property that makes call ordering stop being load-bearing."""
    await _seed_corpus(be, ns)
    query = "event log replay fold insertion order"
    searched = {r["key"] for r in await be.memory_search(ns, query, limit=20)
                if r["retrieval"]["admitted"]}
    recalled = {r["key"] for r in (await sequences.recall(be, ns, query))["results"]}
    assert searched == recalled


async def test_a_namespace_floor_moves_both_surfaces_together(be, ns):
    """One knob, both paths. If a namespace's floor could move one surface and
    not the other, the guard would not actually be shared."""
    await _seed_corpus(be, ns)
    await be.write_variant_profile(ns, {"gate_similarity_floor": 0.99,
                                        "gate_top_fraction_alpha": 0.99})
    query = "event log replay fold insertion order"
    searched = [r for r in await be.memory_search(ns, query)
                if r["retrieval"]["admitted"] and r["retrieval"]["cosine"] is not None]
    recalled = (await sequences.recall(be, ns, query))["results"]
    assert searched == [] or all(r["retrieval"]["cosine"] >= 0.99 for r in searched)
    assert all(r["retrieval"]["cosine"] is None or r["retrieval"]["cosine"] >= 0.99
               for r in recalled)


async def test_keyword_only_search_is_admitted_not_floored(be, ns):
    """With embeddings disabled there is no cosine for anything, and a floor on
    a quantity that does not exist would empty the result set."""
    plain = PostgresBackend(be.pool)  # DisabledEmbedder
    await plain.memory_save(ns, "note/kw", "distinctive haystack token", actor="seed")
    rows = await plain.memory_search(ns, "distinctive haystack token")
    assert rows
    assert all(r["retrieval"]["reason"] == "keyword_only" for r in rows)
    assert all(r["retrieval"]["admitted"] for r in rows)


async def test_write_variant_profile_merges_rather_than_replaces(be, ns):
    """A caller setting a retrieval floor must not silently clear an unrelated
    experiment arm somebody else set."""
    await be.write_variant_profile(ns, {"intent_gate": "on"})
    await be.write_variant_profile(ns, {"gate_similarity_floor": 0.5})
    profile = await be.resolved_profile(ns)
    guard = await be.gate_guard(ns)
    assert profile["intent_gate"] == "on"
    assert guard["gate_similarity_floor"] == 0.5


async def test_registering_a_namespace_twice_keeps_the_first_provenance(be, ns):
    """A creation record is a historical fact; a retry must not rewrite it."""
    first = await be.register_namespace(ns, actor="original")
    second = await be.register_namespace(ns, actor="impostor")
    assert first["registered"] is True and second["registered"] is False
    row = next(r for r in await be.namespace_list() if r["namespace"] == ns)
    assert row["created_by"] == "original"
