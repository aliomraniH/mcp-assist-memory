"""Intent Gate — efficacy ledger, outcome closure, skill counters, PHI
(spec MD-3, MD-4, G2-4, ADV-5). All tests fail at the pre-gate baseline."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from errors import AppError
from tests.conftest import DATABASE_URL, SCHEMA, FakeEmbedder
from storage.postgres import PostgresBackend
from tests.gate_utils import (
    DECISION_TARGET_BRANCH, GATE_ON, SKILL_ANTI_PATTERN, seed, set_profile, unwrap,
)

MONTH_KEY = f"gate/efficacy/{datetime.now(timezone.utc).strftime('%Y%m')}"


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


async def test_md_3_one_decision_one_event_one_rollup_idempotent(gated, ns, sid):
    eid = str(uuid.uuid4())
    ack = await gated.memory_save(
        ns, "probe/ledger", "ledger increment probe", kind="knowledge",
        event_id=eid, actor="claude-code-gate-impl", meta={"session_id": sid})
    assert ack["gate"]["decision"] == "gate_approved"
    events = [e for e in await gated.session_events(ns, sid)
              if e["kind"] == "gate_decision"]
    assert len(events) == 1
    roll = await gated.memory_get(ns, MONTH_KEY)
    assert roll["value"]["decisions"]["gate_approved"] == 1
    roll_rev = roll["revision"]

    # byte-identical replay: zero additional increments (idempotent, actor-scoped)
    ack2 = await gated.memory_save(
        ns, "probe/ledger", "ledger increment probe", kind="knowledge",
        event_id=eid, actor="claude-code-gate-impl", meta={"session_id": sid})
    assert ack2["deduplicated"] is True
    events = [e for e in await gated.session_events(ns, sid)
              if e["kind"] == "gate_decision"]
    assert len(events) == 1
    roll2 = await gated.memory_get(ns, MONTH_KEY)
    assert roll2["revision"] == roll_rev


async def test_md_3_gate_decision_event_payload_shape(gated, ns, sid):
    await gated.intent_open(ns, goal="routine build step", scope=["memory_save"],
                            session_id=sid)
    ev = [e for e in await gated.session_events(ns, sid)
          if e["kind"] == "gate_decision"][-1]
    payload = ev["payload"]
    text = str(payload)
    for field in ("intent_hash", "tier", "decision", "matched", "latency_ms"):
        assert field in text


async def test_md_4_skill_counters_move_only_via_gate_outcomes(gated, ns, sid):
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    # a matching intent applies the skill -> applied+1, via the gate only
    await gated.intent_open(
        ns, goal="rebuild the projection by replaying the event log sorted by timestamp",
        scope=["memory_save"], session_id=sid)
    skill = await gated.memory_get(ns, "skill/no-sorted-fold-replay")
    assert skill["meta"]["efficacy"]["applied"] == 1
    # a raw write to the skill does not move counters through the gate machinery
    await gated.memory_save(
        ns, "skill/no-sorted-fold-replay", unwrap(skill["value"]), kind="knowledge",
        meta={**skill["meta"], "note": "raw touch"}, actor="raw-writer")
    skill2 = await gated.memory_get(ns, "skill/no-sorted-fold-replay")
    assert skill2["meta"]["efficacy"]["applied"] == 1  # unchanged by raw write


async def test_g2_4_outcome_closure_false_positive_and_confirmed(gated, ns, sid):
    # deterministic block: destructive + unresolved conflict, tier2 OFF
    await seed(gated, ns, DECISION_TARGET_BRANCH)
    res = await gated.intent_open(
        ns, goal="commit the gate schema migration directly to main",
        scope=["memory_save", "memory_delete"], session_id=sid)
    assert res["decision"] == "gate_conflict"
    await gated.memory_save(ns, "probe/fp-target", "v", kind="note")
    with pytest.raises(AppError) as ei:
        await gated.memory_delete(ns, "probe/fp-target", meta={"session_id": sid})
    assert ei.value.code == "gate_blocked"

    # FALSE POSITIVE path: retried unchanged + operator override -> succeeds
    out = await gated.memory_delete(
        ns, "probe/fp-target", meta={"session_id": sid, "gate_override": True},
        actor="operator")
    assert out["tombstone"] is True  # override executes; the gate records, not vetoes
    assert "gate_override" in out["gate"]["flags"]
    roll = await gated.memory_get(ns, MONTH_KEY)
    assert roll["value"]["closures"]["false_positive"] == 1

    # CONFIRMED path: a blocked write whose override retry then genuinely fails
    eid = str(uuid.uuid4())
    await gated.memory_save(ns, "probe/cc", "payload A", kind="note", event_id=eid,
                            actor="claude-code-gate-impl")
    with pytest.raises(AppError):
        # same event_id, different payload, destructive-conflict session context
        await gated.memory_save(ns, "probe/cc", "payload B", kind="note",
                                event_id=eid, actor="claude-code-gate-impl",
                                meta={"session_id": sid})
    with pytest.raises(AppError) as ei2:
        await gated.memory_save(ns, "probe/cc", "payload B", kind="note",
                                event_id=eid, actor="claude-code-gate-impl",
                                meta={"session_id": sid, "gate_override": True})
    assert ei2.value.code == "idempotency_conflict"
    roll2 = await gated.memory_get(ns, MONTH_KEY)
    assert roll2["value"]["closures"]["confirmed_correct"] == 1


async def test_adv_5_clinical_profile_persists_hash_never_verbatim(gated, ns, sid):
    await set_profile(gated, ns, {**GATE_ON, "clinical": True})
    phi_goal = "record that patient John Q. Smith DOB 1961-03-04 MRN 448821 missed dialysis"
    res = await gated.intent_open(ns, goal=phi_goal, scope=["memory_save"],
                                  session_id=sid)
    assert res["intent_hash"]
    assert "phi_screened" in res["flags"]
    # the response itself never echoes the verbatim goal
    assert "John Q. Smith" not in str(res)

    # read-back scan: no persisted row anywhere carries the verbatim intent
    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT goal, labels, intent_hash FROM gate_intent WHERE namespace = %s",
            (ns,))
        rows = await cur.fetchall()
        assert rows and all(r["goal"] is None for r in rows)
        assert all(r["intent_hash"] for r in rows)
        cur = await conn.execute(
            "SELECT count(*) AS n FROM memory_entry WHERE namespace = %s "
            "AND value::text ILIKE %s", (ns, "%John Q. Smith%"))
        assert (await cur.fetchone())["n"] == 0
        cur = await conn.execute(
            "SELECT count(*) AS n FROM session_event WHERE namespace = %s "
            "AND payload::text ILIKE %s", (ns, "%John Q. Smith%"))
        assert (await cur.fetchone())["n"] == 0
    # session events carry intent_hash + screened labels only
    ev = [e for e in await gated.session_events(ns, sid)
          if e["kind"] == "gate_decision"][-1]
    assert res["intent_hash"][:12] in str(ev["payload"])
