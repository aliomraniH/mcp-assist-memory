"""Three diagnosed-but-unfixed defects from the v0.4.0 post-deploy review.

Each was found in a LIVE observation the CI suite was structurally unable to
reproduce, so each fixture here is written to be potent in the environment that
hid the bug — not merely to pass:

  1. latency_spans arithmetic (live T15 FAIL). `parallel_reads` double-counted
     its own children and `other = max(0, ...)` clamped the residual to zero,
     so 78-194ms of real latency was reported as fully accounted for. The
     pre-existing CI test passed ONLY because FakeEmbedder makes goal_embedding
     0ms locally: with a zero-cost child there is nothing to double-count. The
     test below therefore makes the embedding cost non-zero on purpose, and
     asserts the fixture is potent before asserting the invariant.

  2. Brand-new skills flagged `expired_skill`. last_validated was set only
     inside `if trigger_valid:`, so a skill published with no trigger — a
     documented, legitimate way to ship display-only advice — was born expired.

  3. gate_close_outcome never reached gate/efficacy/<yyyymm>. Two unrelated
     concepts share the word "closure"; the populated block-closure counter
     sitting next to the missing one made the hole look like a zero.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from storage.gate import (
    DEFAULT_SKILL_VALIDITY_HOURS,
    _skill_expired,
    month_key,
)
from storage.gate_targets import ACCOUNTED_SPANS, NESTED_SPANS, SPAN_NAMES
from storage.postgres import PostgresBackend
from tests.conftest import DATABASE_URL, SCHEMA, FakeEmbedder
from tests.gate_utils import GATE_ON, SKILL_ANTI_PATTERN, seed, set_profile

VIOLATING_GOAL = "rebuild the projection by replaying the event log sorted by timestamp"

# Large enough to dominate every other span against a local Postgres, so the
# double-count is unmistakable rather than lost in millisecond noise.
SLOW_EMBED_MS = 60


@pytest_asyncio.fixture
async def gated(ns):
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=8)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    backend = PostgresBackend(pool, embedder=FakeEmbedder())
    await set_profile(backend, ns, dict(GATE_ON))
    yield backend
    await backend.gate_cache.stop()
    await pool.close()


@pytest_asyncio.fixture
async def sid(gated, ns):
    return (await gated.session_create(ns, surface="test"))["session_id"]


def _slow_query_embedder(backend, ms: int = SLOW_EMBED_MS):
    """Give the goal-embedding call a real, non-zero cost.

    This is the whole point. On the deployed server that call is a network round
    trip to the embedding provider and is the single largest span on the path;
    in CI it is an in-process fake that returns instantly. A zero-cost child
    cannot double-count, which is precisely why the original span test was green
    in CI while the same arithmetic failed live.
    """
    original = backend.embedder.embed

    async def slow(texts, *, input_type="document"):
        if input_type == "query":
            await asyncio.sleep(ms / 1000)
        return await original(texts, input_type=input_type)

    backend.embedder.embed = slow


# ===========================================================================
# DEFECT 1 — latency_spans arithmetic.
# ===========================================================================
@pytest.mark.ci
async def test_nested_spans_are_not_double_counted_and_other_is_not_clamped(gated, ns):
    """The additive breakdown is the SEQUENTIAL timeline; the concurrent legs
    are nested detail. Summing both counts the same milliseconds twice."""
    _slow_query_embedder(gated)
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])

    spans = res["latency_spans"]
    total = res["latency_ms"]

    assert set(spans) <= set(SPAN_NAMES), f"unknown span names: {set(spans) - set(SPAN_NAMES)}"

    # The embedding cost is real and attributed to its own span...
    assert spans["goal_embedding"] >= SLOW_EMBED_MS * 0.5, (
        "fixture is not exercising the bug: goal_embedding must cost real time")
    # ...and it happened INSIDE the concurrent block, not in addition to it.
    assert spans["parallel_reads"] >= spans["goal_embedding"], (
        "goal_embedding is nested inside parallel_reads; a parent span shorter "
        "than its own child means the nesting model is wrong")

    # FIXTURE POTENCY. Summing every span — parent AND children — must overshoot
    # the total here. That overshoot is exactly what v1 computed, and exactly
    # what the max(0, ...) clamp then swallowed. If this does not hold, the
    # assertions below cannot detect the regression.
    assert sum(spans.values()) > total, (
        "with a non-zero embedding cost the naive all-span sum must exceed the "
        "total; otherwise this test would pass against the buggy arithmetic")

    # THE INVARIANT: the accounted (non-overlapping) spans sum to the total.
    # Relative tolerance, because an absolute one is a latency assertion in
    # disguise and would mean something different on Neon than it does here.
    accounted = {k: v for k, v in spans.items() if k not in NESTED_SPANS}
    assert set(accounted) <= set(ACCOUNTED_SPANS)
    assert sum(accounted.values()) == pytest.approx(total, rel=0.05, abs=3)

    # NO CLAMP. Under the old formula `other` was max(0, total - naive_sum),
    # which the overshoot above drove to a tidy, lying zero. The residual is
    # small but real: span values truncate to whole ms and some path work is
    # deliberately unnamed.
    assert spans["other"] > 0, (
        "`other` must be the true residual; a clamped zero is how 78-194ms of "
        "live latency reported itself as fully accounted for")


@pytest.mark.ci
async def test_persist_span_is_named_rather_than_folded_into_other(gated, ns):
    """The gate_intent INSERT is a round trip. An unnamed round trip is a cost
    that hides — `other` should be a residual, not a bucket."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])

    spans = res["latency_spans"]
    assert "persist" in spans, "the intent INSERT must have its own span"
    assert "persist" in ACCOUNTED_SPANS and "persist" not in NESTED_SPANS


@pytest.mark.ci
async def test_every_concurrent_leg_is_measured(gated, ns):
    """Three of the four concurrent legs were never timed at all, so 'which leg
    is slow' — the question the concurrency change exists to answer — could not
    be answered for them."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])

    spans = res["latency_spans"]
    for leg in ("goal_embedding", "ann_query", "trigger_scan", "structured_scan",
                "project_block"):
        assert leg in spans, f"concurrent leg {leg} is unmeasured"


# ===========================================================================
# DEFECT 2 — a brand-new triggerless skill must not be born expired.
# ===========================================================================
@pytest.mark.ci
async def test_skill_authored_without_a_trigger_is_not_born_expired(gated, ns):
    """Publishing advice with no predicate is a documented use of this tool: the
    skill advises and never escalates. It was unusable from the moment it was
    written — flagged expired_skill by the very first intent_open."""
    res = await gated.skill_define(
        ns, key="skill/advice-only", polarity="best-practice",
        guidance="Prefer an explicit timeout on every outbound call.",
        trigger=None, trigger_author="human", actor="operator")

    assert res["display_only"] is True, "no trigger: advises, never escalates"

    row = await gated.memory_get(ns, "skill/advice-only")
    stamped = (row["meta"] or {}).get("last_validated")
    assert stamped, "a human authoring a skill IS the validation event"
    assert _skill_expired(stamped, DEFAULT_SKILL_VALIDITY_HOURS,
                          datetime.now(timezone.utc)) is False, (
        "a skill is expired the instant it is written")


@pytest.mark.ci
async def test_triggerless_skill_does_not_surface_as_expired(gated, ns):
    """Asserted through the gate, not just the stored meta — the flag is what a
    caller actually sees."""
    await gated.skill_define(
        ns, key="skill/advice-only", polarity="anti-pattern",
        guidance="ANTI-PATTERN: replaying an event log sorted by timestamp.",
        trigger_intent="replay event log projection rebuild fold order",
        trigger=None, trigger_author="human", actor="operator")

    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])
    surfaced = [m for m in res["matched"] if m["key"] == "skill/advice-only"]
    assert surfaced, "the skill must be retrievable at all for this to mean anything"
    assert "expired_skill" not in surfaced[0]["flags"]


@pytest.mark.ci
async def test_a_triggerless_skill_still_never_earns_escalation_provenance(gated, ns):
    """The freshness fix must not become an escalation change. curator_provenance
    is about the PREDICATE having been validated; with no predicate there is
    nothing to provenance, and the skill stays display-only."""
    await gated.skill_define(
        ns, key="skill/advice-only", polarity="anti-pattern",
        guidance="ANTI-PATTERN: replaying an event log sorted by timestamp.",
        trigger_intent="replay event log projection rebuild fold order",
        trigger=None, trigger_author="human", actor="operator")

    row = await gated.memory_get(ns, "skill/advice-only")
    assert not (row["meta"] or {}).get("curator_provenance")

    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])
    surfaced = [m for m in res["matched"] if m["key"] == "skill/advice-only"]
    assert "unprovenanced_skill" in surfaced[0]["flags"]
    assert res["decision"] != "gate_conflict", (
        "a skill with no trigger predicate must never escalate")


@pytest.mark.ci
async def test_unvalidated_author_does_not_get_a_freshness_stamp(gated, ns):
    """An 'unvalidated' author is explicitly not a validation event."""
    await gated.skill_define(
        ns, key="skill/anon-advice", polarity="best-practice",
        guidance="Some advice from nobody in particular.",
        trigger=None, trigger_author="unvalidated", actor="operator")

    row = await gated.memory_get(ns, "skill/anon-advice")
    assert not (row["meta"] or {}).get("last_validated")


# ===========================================================================
# DEFECT 3 — outcome closure must reach the monthly efficacy rollup.
# ===========================================================================
@pytest.mark.ci
async def test_outcome_closure_reaches_the_monthly_rollup(gated, ns, sid):
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid)
    closed = await gated.gate_close_outcome(
        ns, intent_hash=res["intent_hash"], outcome="followed", actor="operator")
    assert closed["closed"], "precondition: something was actually closed"

    rollup = await gated.memory_get(ns, month_key())
    assert rollup is not None, "the monthly rollup must exist"
    assert rollup["value"]["outcomes"]["followed"] == 1


@pytest.mark.ci
async def test_outcome_closure_and_block_closure_stay_separate_counters(gated, ns, sid):
    """The two concepts that share the word 'closure' answer different questions
    and must never be summed into one population."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid)
    await gated.gate_close_outcome(
        ns, intent_hash=res["intent_hash"], outcome="overridden", actor="operator")

    value = (await gated.memory_get(ns, month_key()))["value"]
    assert value["outcomes"]["overridden"] == 1
    # No block was closed, so the block-closure counters stay at zero.
    assert value["closures"] == {"confirmed_correct": 0, "false_positive": 0,
                                 "unknown": 0}


@pytest.mark.ci
async def test_replaying_a_closure_does_not_double_count_the_rollup(gated, ns, sid):
    """A retried close must be a visible no-op everywhere, including here — or
    the retry inflates the very number it reports."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid)
    ihash = res["intent_hash"]

    first = await gated.gate_close_outcome(ns, intent_hash=ihash,
                                           outcome="followed", actor="operator")
    second = await gated.gate_close_outcome(ns, intent_hash=ihash,
                                            outcome="followed", actor="operator")
    assert first["closed"] and second["already_closed"] and not second["closed"]

    value = (await gated.memory_get(ns, month_key()))["value"]
    assert value["outcomes"]["followed"] == 1, "a replay must not re-count"
