"""[CI] Intent Gate remediation, Phase 2 — telemetry completeness and counter
integrity.

Two defects, both established by controlled experiment during the independent
validation run:

  FINDING-5  Four blocks occurred. The gate/efficacy rollup captured two;
             tool_events captured zero. Both denominators of the published
             false-positive rate were wrong, in the same direction, and neither
             error was quantified.

  FINDING-4/S8  skill/no-sorted-fold-replay ran applied 0 -> 5, every increment
             written by actor 'gate', including one caused by the catering false
             positive. The instrument shared identity with its subject.

Environment class: [CI]. Emission paths, enum classification, uniqueness
constraints and projection arithmetic are all environment-independent.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from errors import AppError
from storage.gate import (
    STAGE_ACTED_UPON,
    STAGE_MATCHED,
    STAGE_OUTCOME_CLOSED,
    STAGE_SURFACED,
    STAGE_WRITER,
)
from storage.postgres import PostgresBackend
from storage.telemetry import ERROR_TYPES, build_event_row, classify_error_type
from tests.conftest import DATABASE_URL, SCHEMA, FakeEmbedder
from tests.gate_utils import (
    DECISION_TARGET_BRANCH,
    GATE_ON,
    SKILL_ANTI_PATTERN,
    seed,
    set_profile,
)

pytestmark = pytest.mark.ci

VIOLATING_GOAL = "rebuild the projection by replaying the event log sorted by timestamp"


@pytest_asyncio.fixture
async def gated(ns):
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=4)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    backend = PostgresBackend(pool, embedder=FakeEmbedder())
    await set_profile(backend, ns, dict(GATE_ON))
    yield backend
    await pool.close()


@pytest_asyncio.fixture
async def sid(gated, ns):
    return (await gated.session_create(ns, surface="test"))["session_id"]


# ---------------------------------------------------------------------------
# 2a — the unified emitter.
# ---------------------------------------------------------------------------
def test_block_verdict_travels_on_the_exception_not_just_the_result():
    """THE structural half of FINDING-5b, in one assertion.

    A blocked call raises, so its result is None. v1 read the gate verdict from
    the result alone, which meant the finally-path DID write a row for every
    block — with gate_tier and gate_decision NULL. The row existed and was
    invisible to `SELECT gate_decision, count(*) FROM tool_events`, so every
    analytics view concluded the gate never blocks anything.
    """
    row = build_event_row(
        tool="memory_delete", args={"namespace": "ns"}, result=None,
        outcome="error", error_code="gate_blocked",
        gate={"tier": 0, "decision": "gate_blocked", "rule": "confirm_mismatch"})

    assert row["gate_decision"] == "gate_blocked"
    assert row["gate_tier"] == 0
    assert row["gate_rule"] == "confirm_mismatch"
    assert row["error_type"] == "confirm_mismatch"


@pytest.mark.parametrize("rule,expected", [
    ("confirm_mismatch", "confirm_mismatch"),
    ("unresolved_conflict_destructive", "unresolved_conflict_destructive"),
    ("idempotency_conflict", "idempotency_conflict"),
    ("intent_mismatch", "intent_mismatch"),
    (None, "internal"),
    ("some_new_rule_nobody_mapped", "internal"),
])
def test_error_type_is_a_closed_low_cardinality_enum(rule, expected):
    """Raw exception messages must never become a dimension: unbounded
    cardinality, drift on every reword, and the likeliest place for user content
    to leak into telemetry. Anything unrecognised collapses to 'internal'."""
    got = classify_error_type(rule, None, "error")
    assert got == expected
    assert got in ERROR_TYPES


def test_successful_calls_carry_no_error_type():
    """NULL means 'nothing went wrong', never 'we could not tell'."""
    assert classify_error_type(None, None, "ok") is None
    row = build_event_row(tool="memory_save", args={"namespace": "ns"},
                          result={"verified_persisted": True}, outcome="ok")
    assert row["error_type"] is None


def test_no_raw_exception_message_becomes_a_dimension():
    row = build_event_row(
        tool="memory_save", args={"namespace": "ns"}, result=None,
        outcome="error", error_code="ValueError",
        gate={"tier": 0, "decision": "gate_blocked",
              "rule": "connection to patient-db-prod failed for user jdoe"})
    assert row["error_type"] == "internal"
    assert "jdoe" not in str(row["error_type"])


@pytest.fixture
def tools(gated):
    """Point the tool layer at the gated backend. The emitter lives in the tool
    layer's finally-path, so a test that calls the backend directly proves
    nothing about emission — the same gap that let FINDING-5 survive a green
    suite."""
    from server import mcp_server
    mcp_server.deps.backend = gated
    yield mcp_server
    mcp_server.deps.backend = None


async def test_four_block_telemetry(tools, gated, ns, sid):
    """[CI] four_block_telemetry. Ground truth from the validation run was four
    blocks; the rollup captured two and tool_events captured zero. A blocked
    call must now appear in tool_events WITH its verdict and rule — the v1
    failure was a row present with null gate columns, which is invisible to
    every query anyone would actually write."""
    await seed(gated, ns, DECISION_TARGET_BRANCH)
    await tools.intent_open(
        namespace=ns, goal="commit the gate schema migration directly to main",
        scope=["memory_save", "memory_delete"], session_id=sid)
    await tools.memory_save(namespace=ns, key="probe/target", value="v", kind="note")

    # unresolved_conflict_destructive: a delete under an unresolved conflict.
    with pytest.raises(Exception):
        await tools.memory_delete(namespace=ns, key="probe/target",
                                  meta={"session_id": sid})

    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT tool, gate_decision, gate_rule, error_type, discontinuity "
            "FROM tool_events WHERE namespace = %s AND gate_decision = %s",
            (ns, "gate_blocked"))
        rows = await cur.fetchall()

    assert rows, "a blocked call must appear in tool_events"
    assert all(r["gate_rule"] for r in rows), "the rule that fired must be named"
    assert all(r["error_type"] in ERROR_TYPES for r in rows)
    assert all(r["discontinuity"] is False for r in rows)


async def test_block_rows_are_findable_by_the_obvious_query(tools, gated, ns, sid):
    """The regression guard for FINDING-5b stated as the analyst would hit it:
    the canonical group-by must see the block. v1 wrote the row with null gate
    columns, so this exact query returned nothing and every v_* view concluded
    the gate never blocks."""
    await seed(gated, ns, DECISION_TARGET_BRANCH)
    await tools.intent_open(
        namespace=ns, goal="commit the gate schema migration directly to main",
        scope=["memory_save", "memory_delete"], session_id=sid)
    await tools.memory_save(namespace=ns, key="probe/t2", value="v", kind="note")
    with pytest.raises(Exception):
        await tools.memory_delete(namespace=ns, key="probe/t2",
                                  meta={"session_id": sid})

    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT gate_decision, count(*) AS n FROM tool_events "
            "WHERE namespace = %s GROUP BY gate_decision", (ns,))
        by_decision = {r["gate_decision"]: r["n"] for r in await cur.fetchall()}

    assert by_decision.get("gate_blocked", 0) >= 1


async def test_idempotent_emit(gated, ns):
    """[CI] idempotent_emit. A retried emission collapses instead of
    double-counting a block — otherwise the new numbers would be as
    untrustworthy as the ones they replace."""
    for _ in range(3):
        await gated.record_tool_event(
            tool="memory_save", args={"namespace": ns}, outcome="error",
            error_code="gate_blocked",
            gate={"tier": 0, "decision": "gate_blocked", "rule": "confirm_mismatch"},
            emit_event_id=f"test-emit:{ns}:1")

    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT count(*) AS n FROM tool_events WHERE emit_event_id = %s",
            (f"test-emit:{ns}:1",))
        assert (await cur.fetchone())["n"] == 1


async def test_unkeyed_emissions_are_not_deduplicated_against_each_other(gated, ns):
    """The other half of idempotency: rows WITHOUT an emit id must still all
    land. A partial unique index makes that work; a plain one would collapse
    every unkeyed row into a single NULL slot and hide most of the traffic."""
    for _ in range(3):
        await gated.record_tool_event(
            tool="memory_save", args={"namespace": ns}, outcome="ok")
    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT count(*) AS n FROM tool_events WHERE namespace = %s AND tool = %s",
            (ns, "memory_save"))
        assert (await cur.fetchone())["n"] == 3


async def test_crash_during_emit_never_fails_the_write(gated, ns, monkeypatch):
    """[CI] crash_during_emit. Telemetry is observability, not the user's
    persistence ack. A broken emitter must never manufacture a phantom failure
    for a write that genuinely succeeded."""
    async def boom(*a, **k):
        raise RuntimeError("telemetry backend down")

    ack = await gated.memory_save(ns, "probe/durable", "v", kind="note")
    assert ack["verified_persisted"] is True

    monkeypatch.setattr(gated, "record_tool_event", boom)
    ack2 = await gated.memory_save(ns, "probe/durable-2", "v", kind="note")
    assert ack2["verified_persisted"] is True


# ---------------------------------------------------------------------------
# 2b — event-sourced counters.
# ---------------------------------------------------------------------------
async def test_applied_0_to_5_inflation_replay(gated, ns, sid):
    """[CI] applied_0_to_5_inflation. Replays the exact live sequence: five
    matches against skill/no-sorted-fold-replay, one of them the catering false
    positive. v1 recorded applied:5. The replay must land 5 in `matched` ONLY,
    with acted_upon and outcome_closed at 0 — and, critically, must never write
    a revision of the skill it is measuring.
    """
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    goals = [
        VIOLATING_GOAL,
        "replay the event log in timestamp order",
        "reconcile the fold order by sorting on occurred_at",
        "rebuild the projection by replaying the event log in insertion order",
        "schedule the quarterly workshop catering",  # the false positive
    ]
    for goal in goals:
        await gated.intent_open(ns, goal=goal, scope=["memory_save"], session_id=sid)

    eff = await gated.skill_efficacy(ns, "skill/no-sorted-fold-replay")

    # FOUR, not five — and the missing one is the whole point.
    #
    # v1 recorded applied:5 because every retrieval incremented the counter, and
    # the catering intent was retrieved. Under the remediated guard the catering
    # goal does not clear the floor against this skill at all, so it contributes
    # no event to any stage. The inflated increment is not merely reclassified
    # as diagnostic; it never happens.
    assert eff["matched"] == 4
    assert eff["surfaced"] == 4

    # And nothing that could tune a threshold has moved.
    assert eff["acted_upon"] == 0
    assert eff["outcome_closed"] == 0

    # The catering intent left no trace against this skill, on any stage.
    catering = await gated.intent_open(
        ns, goal="schedule the quarterly workshop catering", scope=["memory_save"],
        session_id=sid)
    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT count(*) AS n FROM skill_efficacy_events "
            "WHERE namespace = %s AND intent_hash = %s", (ns, catering["intent_hash"]))
        assert (await cur.fetchone())["n"] == 0

    skill = await gated.memory_get(ns, "skill/no-sorted-fold-replay")
    assert skill["revision"] == 1
    # The v1 mutable counter is not resurrected as a side effect.
    assert (skill["meta"].get("efficacy") or {}).get("applied") in (0, None)


async def test_double_increment_is_structurally_impossible(gated, ns, sid):
    """[CI] double_increment. Repeating the SAME intent cannot inflate a stage:
    UNIQUE(namespace, skill_key, intent_hash, stage) makes it a schema property
    rather than a convention the next writer has to remember."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    for _ in range(4):
        await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                session_id=sid)
    eff = await gated.skill_efficacy(ns, "skill/no-sorted-fold-replay")
    assert eff["matched"] == 1
    assert eff["surfaced"] == 1


async def test_actor_forgery_is_rejected_per_stage(gated, ns):
    """[CI] actor_forgery. Each stage has a distinct writer actor and the wrong
    one is rejected. Not ceremony: event dedup is scoped to (namespace, actor),
    so sharing an actor across stages would let one stage's dedup silently
    swallow another's events."""
    from storage.gate import record_efficacy_event

    ihash = "a" * 64
    with pytest.raises(AppError):
        await record_efficacy_event(
            gated, ns, "skill/x", ihash, STAGE_OUTCOME_CLOSED,
            writer_actor="gate-eval", outcome="followed", strict=True)

    # The legitimate writer succeeds.
    assert await record_efficacy_event(
        gated, ns, "skill/x", ihash, STAGE_OUTCOME_CLOSED,
        writer_actor=STAGE_WRITER[STAGE_OUTCOME_CLOSED], outcome="followed",
        strict=True) is True


def test_each_stage_has_a_distinct_writer_where_it_matters():
    """Closure must never share an actor with evaluation — the subject under
    measurement and the instrument recording it stay separable."""
    assert STAGE_WRITER[STAGE_OUTCOME_CLOSED] == "gate-closure"
    assert STAGE_WRITER[STAGE_ACTED_UPON] == "gate-linkage"
    assert STAGE_WRITER[STAGE_MATCHED] == "gate-eval"
    assert STAGE_WRITER[STAGE_OUTCOME_CLOSED] != STAGE_WRITER[STAGE_MATCHED]
    assert STAGE_WRITER[STAGE_OUTCOME_CLOSED] != STAGE_WRITER[STAGE_ACTED_UPON]


async def test_closure_path(gated, ns, sid):
    """[CI] closure_path. outcome_closed is the only stage that may tune a
    threshold, and it only moves when someone deliberately records what
    happened."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid)

    closed = await gated.gate_close_outcome(
        ns, intent_hash=res["intent_hash"], outcome="followed", actor="operator")
    assert "skill/no-sorted-fold-replay" in closed["closed"]

    eff = await gated.skill_efficacy(ns, "skill/no-sorted-fold-replay")
    assert eff["outcome_closed"] == 1
    assert eff["outcomes"]["followed"] == 1


async def test_closure_is_one_per_intent_ever(gated, ns, sid):
    """'One outcome_closed increment per intent, ever' — and a replay must be a
    VISIBLE no-op, never look like a fresh close."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid)
    first = await gated.gate_close_outcome(
        ns, intent_hash=res["intent_hash"], outcome="followed", actor="operator")
    second = await gated.gate_close_outcome(
        ns, intent_hash=res["intent_hash"], outcome="overridden", actor="operator")

    assert first["closed"] and not first["already_closed"]
    assert second["already_closed"] and not second["closed"]

    eff = await gated.skill_efficacy(ns, "skill/no-sorted-fold-replay")
    assert eff["outcome_closed"] == 1
    assert eff["outcomes"] == {"followed": 1}


async def test_closure_does_not_reopen_earlier_stages(gated, ns, sid):
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid)
    await gated.gate_close_outcome(ns, intent_hash=res["intent_hash"],
                                   outcome="abandoned", actor="operator")
    eff = await gated.skill_efficacy(ns, "skill/no-sorted-fold-replay")
    assert eff["matched"] == 1 and eff["surfaced"] == 1


@pytest.mark.parametrize("bad", ["applied", "great", "", None])
async def test_closure_outcome_is_a_closed_enum(gated, ns, sid, bad):
    with pytest.raises(AppError):
        await gated.gate_close_outcome(ns, intent_hash="a" * 64, outcome=bad)


async def test_negative_attribution(gated, ns, sid):
    """[CI] negative_attribution. THE DOCUMENTED BIAS, pinned so it stays
    visible instead of becoming folklore.

    Session linkage is a weak causal proxy: ANY subsequent gated write in the
    intent's session flips acted_upon, including a write with nothing to do with
    the surfaced skill. So acted_upon over-counts from day one. That is a
    property of the design, not a bug awaiting a fix.

    It is tolerable only because of where the number is allowed to go — it is
    diagnostic and never tunes a threshold. This test asserts BOTH halves: the
    proxy fires (over-counting, as documented) AND outcome_closed stays at zero,
    because nobody recorded what actually happened.
    """
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                            session_id=sid)

    # A write with no relationship whatsoever to the surfaced skill.
    await gated.memory_save(ns, "note/unrelated-grocery-list", "milk, eggs",
                            kind="note", meta={"session_id": sid})

    eff = await gated.skill_efficacy(ns, "skill/no-sorted-fold-replay")
    assert eff["acted_upon"] == 1, "documented proxy behaviour: it over-counts"
    assert eff["outcome_closed"] == 0, "no closure was recorded, so none may be implied"
    assert "over-counts" in eff["note"]


async def test_projection_restates_the_bias_at_every_read(gated, ns, sid):
    """A consumer must not be able to pick the number up without the caveat
    attached to it."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                            session_id=sid)
    eff = await gated.skill_efficacy(ns, "skill/no-sorted-fold-replay")
    assert "outcome_closed" in eff["note"]
    assert "diagnostic" in eff["note"]


async def test_efficacy_events_store_no_raw_goal_text(gated, ns, sid):
    """PHI hard gate on the second NEW storage surface. Asserted against the
    live column list so a future migration cannot quietly add a goal column."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                            session_id=sid)
    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'skill_efficacy_events'")
        columns = {r["column_name"] for r in await cur.fetchall()}
    assert "goal" not in columns
    assert "intent_hash" in columns
