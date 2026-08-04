"""[CI] Intent Gate remediation, Phase 1 — the deterministic core.

These fixtures pin the two failures the independent validation run reproduced
LIVE against the deployed server, plus the guard boundaries and the deliberate
behaviour change that comes with predicate-first escalation.

Environment class: [CI]. Every assertion here is about gate LOGIC, which local
Postgres exercises faithfully — the retrieval path, the guard arithmetic, and
the predicate evaluation are all environment-independent. The two things that
are NOT [CI]-decidable (round-trip latency and real-encoder cosine
distributions) are deliberately absent from this file; they live in the [NEON]
set. That separation is the whole point of the marker taxonomy: G1-5 originally
"passed" in CI against a 5-entry namespace and was false in production, so the
size-scaled variants below exist to make the floor actually get exercised.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from storage.postgres import PostgresBackend
from storage.profiles import (
    DEFAULT_SIMILARITY_FLOOR,
    DEFAULT_TOP_FRACTION_ALPHA,
    resolve_gate_guard,
    resolve_profile,
)
from storage.gate import _guarded_candidates
from tests.conftest import DATABASE_URL, SCHEMA, FakeEmbedder
from tests.gate_utils import (
    GATE_ON,
    SKILL_ANTI_PATTERN,
    SKILL_ANTI_PATTERN_NO_TRIGGER,
    seed,
    set_profile,
)

pytestmark = pytest.mark.ci

CATERING_GOAL = "schedule the quarterly workshop catering"
COMPLIANT_GOAL = "rebuild the projection by replaying the event log in insertion order"
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


# Filler templated over actions x objects x conditions, embedded with the same
# encoder as everything else. The point is volume of PLAUSIBLE neighbours: the
# original G1-5 fixture used a 5-entry namespace, so top-k never saturated and
# the floor was never exercised. Noise only becomes visible at scale.
_ACTIONS = ["record", "summarise", "review", "archive", "publish", "measure",
            "annotate", "compare", "escalate", "retire"]
_OBJECTS = ["latency sample", "release note", "runbook", "dashboard", "budget",
            "onboarding guide", "incident report", "roadmap", "invoice", "survey"]
_CONDITIONS = ["for the quarterly review", "before the next deploy",
               "in the shared workspace", "with the operator present",
               "under the new policy"]


async def _fill(backend, ns: str, n: int) -> None:
    i = 0
    for cond in _CONDITIONS:
        for obj in _OBJECTS:
            for act in _ACTIONS:
                if i >= n:
                    return
                await backend.memory_save(
                    ns, f"note/filler-{i:04d}",
                    f"{act} the {obj} {cond}. B7 latency sample {i}.",
                    kind="note", actor="seed-writer", origin="tool")
                i += 1


# ---------------------------------------------------------------------------
# The two live failures, at three namespace sizes.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("size", [5, 45, 200], ids=["size_5", "size_45", "size_200"])
async def test_catering_false_positive(gated, ns, sid, size):
    """[CI] catering_false_positive. Live, this goal returned gate_conflict
    against skill/no-sorted-fold-replay at cosine 0.288 — fixture G1-5 failing
    in production while passing in CI.

    Parameterised over namespace size precisely because the original fixture's
    5-entry namespace is what hid the defect.
    """
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await _fill(gated, ns, size)

    res = await gated.intent_open(
        ns, goal=CATERING_GOAL, scope=["memory_save"], session_id=sid,
        actor="claude-code-gate-remediation", verbose_gate=True)

    assert res["decision"] == "gate_approved"
    assert res["conflict"] is None
    # Even if the skill is retrieved for display, it cannot escalate: the
    # predicate does not match a catering intent.
    for entry in res["gate_audit"]:
        assert entry["escalated"] is False
        if entry["skill_key"] == SKILL_ANTI_PATTERN["key"]:
            assert entry["predicate_match"] is False


@pytest.mark.parametrize("size", [5, 45, 200], ids=["size_5", "size_45", "size_200"])
async def test_compliant_insertion_order(gated, ns, sid, size):
    """[CI] compliant_insertion_order. The harder of the two live failures: this
    goal OBEYS skill/no-sorted-fold-replay ("replay must fold in insertion
    order") and was conflicted against it anyway, because compliance and
    violation are adjacent in embedding space. No cosine threshold fixes this
    case — only a predicate does.
    """
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await _fill(gated, ns, size)

    res = await gated.intent_open(
        ns, goal=COMPLIANT_GOAL, scope=["memory_save"], session_id=sid,
        actor="claude-code-gate-remediation", verbose_gate=True)

    assert res["decision"] == "gate_approved"
    assert res["conflict"] is None
    audit = {a["skill_key"]: a for a in res["gate_audit"]}
    assert audit[SKILL_ANTI_PATTERN["key"]]["predicate_match"] is False
    assert audit[SKILL_ANTI_PATTERN["key"]]["escalation_reason"] == "predicate_did_not_match"


@pytest.mark.parametrize("size", [5, 45, 200], ids=["size_5", "size_45", "size_200"])
async def test_violating_intent_still_escalates(gated, ns, sid, size):
    """The negative control that keeps the remediation honest. Suppressing false
    positives is trivial if you also suppress the true ones; this asserts the
    gate still fires on the case the skill actually prohibits, at every size."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await _fill(gated, ns, size)

    res = await gated.intent_open(
        ns, goal=VIOLATING_GOAL, scope=["memory_save"], session_id=sid,
        actor="claude-code-gate-remediation", verbose_gate=True)

    assert res["decision"] == "gate_conflict"
    assert res["conflict"]["basis"] == "anti_pattern_predicate"
    assert res["conflict"]["skill_key"] == SKILL_ANTI_PATTERN["key"]


# ---------------------------------------------------------------------------
# authored_trigger_suite — one violating + one compliant intent per trigger
# authored under 1e.
# ---------------------------------------------------------------------------
AUTHORED_TRIGGER_SUITE = [
    pytest.param(SKILL_ANTI_PATTERN, VIOLATING_GOAL, COMPLIANT_GOAL,
                 id="skill/no-sorted-fold-replay"),
]


@pytest.mark.parametrize("skill,violating,compliant", AUTHORED_TRIGGER_SUITE)
async def test_authored_trigger_suite(gated, ns, sid, skill, violating, compliant):
    """[CI] authored_trigger_suite. Every trigger authored in 1e carries a paired
    fixture: one intent that MUST escalate and one that MUST NOT. A trigger with
    only a positive case is indistinguishable from the v1 always-escalate bug."""
    await seed(gated, ns, skill)

    hit = await gated.intent_open(ns, goal=violating, scope=["memory_save"],
                                  session_id=sid, verbose_gate=True)
    assert hit["decision"] == "gate_conflict"
    assert hit["conflict"]["skill_key"] == skill["key"]

    miss = await gated.intent_open(ns, goal=compliant, scope=["memory_save"],
                                   session_id=sid, verbose_gate=True)
    assert miss["decision"] == "gate_approved"


# ---------------------------------------------------------------------------
# The deliberate behaviour change (1e).
# ---------------------------------------------------------------------------
async def test_skill_without_trigger_is_display_only(gated, ns, sid):
    """THE BEHAVIOUR CHANGE, pinned. On deploy every existing anti-pattern skill
    has trigger = NULL and becomes display-only, so the gate produces zero
    gate_conflict escalations for it until a trigger is authored.

    This is intended and it is deliberate fail-toward-silence: the conflict
    stream being replaced was false-positive dominated, and a gate that cries
    wolf trains its operator to ignore it. The skill is still SURFACED — the
    advice is not lost, only its ability to block.
    """
    await seed(gated, ns, SKILL_ANTI_PATTERN_NO_TRIGGER)

    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid, verbose_gate=True)

    assert res["decision"] == "gate_approved"
    assert res["conflict"] is None
    keys = [m["key"] for m in res["matched"]]
    assert SKILL_ANTI_PATTERN_NO_TRIGGER["key"] in keys, "advice must still surface"
    audit = {a["skill_key"]: a for a in res["gate_audit"]}
    entry = audit[SKILL_ANTI_PATTERN_NO_TRIGGER["key"]]
    assert entry["escalated"] is False
    assert entry["escalation_reason"] == "no_trigger"
    assert entry["predicate_evaluated"] is False
    m = next(m for m in res["matched"] if m["key"] == SKILL_ANTI_PATTERN_NO_TRIGGER["key"])
    assert "display_only_no_trigger" in m["flags"]


async def test_invalid_trigger_fails_closed_and_is_flagged(gated, ns, sid):
    """[CI] forged_predicate, end to end. A trigger that does not validate must
    make the skill display-only — never escalate on the strength of having been
    retrieved — and must be flagged loudly, because something wrote a predicate
    that does not parse and a human should see that."""
    forged = dict(SKILL_ANTI_PATTERN_NO_TRIGGER)
    forged = {**forged, "key": "skill/forged-trigger",
              "meta": {**forged["meta"], "trigger": {"method": ["os", "system"]}}}
    await seed(gated, ns, forged)

    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=sid, verbose_gate=True)

    assert res["decision"] == "gate_approved"
    audit = {a["skill_key"]: a for a in res["gate_audit"]}
    assert audit["skill/forged-trigger"]["escalation_reason"] == "invalid_trigger"
    assert audit["skill/forged-trigger"]["trigger_schema_errors"]
    m = next(m for m in res["matched"] if m["key"] == "skill/forged-trigger")
    assert "invalid_trigger" in m["flags"]


# ---------------------------------------------------------------------------
# floor_boundary / alpha_boundary — pure guard arithmetic.
# ---------------------------------------------------------------------------
def _cand(key: str, sim: float) -> dict:
    return {"key": key, "similarity": sim}


def test_floor_boundary():
    """[CI] floor_boundary at 0.44 / 0.46 around the 0.45 default."""
    guard = resolve_gate_guard(None)
    assert guard["gate_similarity_floor"] == DEFAULT_SIMILARITY_FLOOR == 0.45
    # alpha neutralised so this isolates the absolute floor
    guard = {**guard, "gate_top_fraction_alpha": 0.0}
    kept = {c["key"] for c in _guarded_candidates(
        [_cand("below", 0.44), _cand("above", 0.46)], guard)}
    assert kept == {"above"}
    # exactly at the floor is accepted (>=, not >)
    kept = {c["key"] for c in _guarded_candidates([_cand("at", 0.45)], guard)}
    assert kept == {"at"}


def test_alpha_boundary():
    """[CI] alpha_boundary at 0.84x / 0.86x top around the 0.85 default."""
    guard = resolve_gate_guard(None)
    assert guard["gate_top_fraction_alpha"] == DEFAULT_TOP_FRACTION_ALPHA == 0.85
    top = 0.80
    guard = {**guard, "gate_similarity_floor": 0.0}
    kept = {c["key"] for c in _guarded_candidates(
        [_cand("top", top), _cand("below", 0.84 * top), _cand("above", 0.86 * top)],
        guard)}
    assert kept == {"top", "above"}


def test_guard_rejects_the_measured_noise_band():
    """The validator measured true matches at 0.504-0.609 and noise topping out
    at 0.392, including seven unrelated latency-sample notes at 0.369-0.376.
    The shipped floor must separate exactly that."""
    guard = {**resolve_gate_guard(None), "gate_top_fraction_alpha": 0.0}
    band = [_cand(f"noise-{i}", s) for i, s in enumerate(
        [0.250, 0.288, 0.369, 0.376, 0.392])]
    band += [_cand("true-low", 0.504), _cand("true-high", 0.609)]
    kept = {c["key"] for c in _guarded_candidates(band, guard)}
    assert kept == {"true-low", "true-high"}


def test_out_of_range_floor_falls_back_to_the_server_default():
    """A profile typo must never silently DISABLE the floor. Failing to the
    default is the only safe direction for a value whose job is suppression."""
    for bad in ({"gate_similarity_floor": -1}, {"gate_similarity_floor": 5},
                {"gate_similarity_floor": "0.9"}, {"gate_similarity_floor": True},
                {"gate_similarity_floor": None}):
        assert resolve_gate_guard(bad)["gate_similarity_floor"] == DEFAULT_SIMILARITY_FLOOR


def test_guard_keys_stay_out_of_the_echoed_profile():
    """Additive constraint: old acks unchanged under the default profile. The
    guard is read by the gate and must not widen every ack the server returns."""
    echoed = resolve_profile({"gate_similarity_floor": 0.6})
    assert "gate_similarity_floor" not in echoed
    assert "gate_top_fraction_alpha" not in echoed
    assert set(echoed) == {"convention_stmt", "advisory_mode", "arg_strictness",
                           "remedy_errors", "compact_acks", "intent_gate", "tier2"}


def test_per_namespace_floor_overrides_the_default():
    guard = resolve_gate_guard({"gate_similarity_floor": 0.60,
                                "gate_top_fraction_alpha": 0.50})
    assert guard["gate_similarity_floor"] == 0.60
    assert guard["gate_top_fraction_alpha"] == 0.50


# ---------------------------------------------------------------------------
# Freshness discipline — a floor with no calibration date is a stale authority.
# ---------------------------------------------------------------------------
def test_guard_carries_calibration_provenance():
    """Durable memory is an amplifier, not a corrective. A stored signal that
    cannot go stale becomes an authority nobody re-examines, so the floor
    carries when it was calibrated and under what temporal mode."""
    default = resolve_gate_guard(None)
    assert default["calibration_ts"] is None
    assert default["temporal_mode"] == "server_default"

    calibrated = resolve_gate_guard({
        "gate_calibration_ts": "2026-08-04T00:00:00Z",
        "gate_temporal_mode": "historical_snapshot"})
    assert calibrated["calibration_ts"] == "2026-08-04T00:00:00Z"
    assert calibrated["temporal_mode"] == "historical_snapshot"


async def test_embedding_drift_surfaces_uncalibrated_floor(gated, ns, sid):
    """[CI] embedding_drift. A namespace whose floor was never calibrated (or was
    calibrated under a different encoder) must SAY so in the audit rather than
    presenting 0.45 as though it were measured here."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=COMPLIANT_GOAL, scope=["memory_save"],
                                  session_id=sid, verbose_gate=True)
    assert res["gate_guard"]["temporal_mode"] == "server_default"
    assert res["gate_guard"]["calibration_ts"] is None

    await set_profile(gated, ns, {**GATE_ON,
                                  "gate_calibration_ts": "2026-08-04T00:00:00Z",
                                  "gate_temporal_mode": "historical_snapshot"})
    res = await gated.intent_open(ns, goal=COMPLIANT_GOAL, scope=["memory_save"],
                                  session_id=sid, verbose_gate=True)
    assert res["gate_guard"]["calibration_ts"] == "2026-08-04T00:00:00Z"


# ---------------------------------------------------------------------------
# gate_match_log — the calibration dataset.
# ---------------------------------------------------------------------------
async def test_every_match_is_logged_escalated_or_not(gated, ns, sid):
    """1a: log EVERY match. A table holding only escalations cannot calibrate
    the floor that produced it."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await _fill(gated, ns, 45)
    res = await gated.intent_open(ns, goal=COMPLIANT_GOAL, scope=["memory_save"],
                                  session_id=sid)
    assert res["decision"] == "gate_approved"  # nothing escalated

    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT skill_key, cosine, absolute_floor, alpha, passed_guard, "
            "predicate_match, intent_hash FROM gate_match_log WHERE namespace = %s",
            (ns,))
        rows = await cur.fetchall()

    assert rows, "a non-escalating match must still be logged"
    keys = {r["skill_key"] for r in rows}
    assert SKILL_ANTI_PATTERN["key"] in keys
    for r in rows:
        assert r["absolute_floor"] == pytest.approx(DEFAULT_SIMILARITY_FLOOR)
        assert r["alpha"] == pytest.approx(DEFAULT_TOP_FRACTION_ALPHA)
        assert r["intent_hash"] == res["intent_hash"]


async def test_match_log_stores_no_raw_goal_text(gated, ns, sid):
    """[CI] PHI hard gate on a NEW storage surface. gate_match_log carries
    intent_hash only — it has no goal column to leak into, and the assertion is
    made against the live column list so a future migration cannot quietly add
    one."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                            session_id=sid)

    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'gate_match_log'")
        columns = {r["column_name"] for r in await cur.fetchall()}
    assert "goal" not in columns
    assert "intent_hash" in columns

    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT count(*) AS n FROM gate_match_log WHERE namespace = %s "
            "AND skill_key LIKE %s", (ns, "%timestamp%"))
        assert (await cur.fetchone())["n"] == 0


async def test_clinical_namespace_keeps_features_off_the_free_text_channel(gated, ns, sid):
    """PHI: in a clinical namespace the extracted `raw` feature is withheld, so
    no free text can reach a predicate, a match-log row, or an audit block."""
    await set_profile(gated, ns, {**GATE_ON, "clinical": True})
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(
        ns, goal="rebuild the projection for patient Jane Doe sorted by timestamp",
        scope=["memory_save"], session_id=sid, verbose_gate=True)

    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT goal FROM gate_intent WHERE namespace = %s", (ns,))
        assert all(r["goal"] is None for r in await cur.fetchall())
    assert "Jane" not in str(res.get("gate_audit"))


# ---------------------------------------------------------------------------
# Cosine can never escalate — stated as an executable invariant.
# ---------------------------------------------------------------------------
async def test_cosine_never_escalates_at_any_similarity(gated, ns, sid):
    """The invariant the whole phase rests on. Even a candidate retrieved at the
    top of the ranking cannot escalate without a matching predicate."""
    await seed(gated, ns, SKILL_ANTI_PATTERN_NO_TRIGGER)
    # A goal deliberately built to maximise lexical overlap with the skill body.
    res = await gated.intent_open(
        ns,
        goal=("replaying an event log by occurred_at event_id sort breaks "
              "sticky tombstone resurrection insertion order rowid ASC"),
        scope=["memory_save"], session_id=sid, verbose_gate=True)
    audit = {a["skill_key"]: a for a in res["gate_audit"]}
    entry = audit[SKILL_ANTI_PATTERN_NO_TRIGGER["key"]]
    assert entry["cosine"] is None or entry["cosine"] > 0.5, "should rank highly"
    assert entry["escalated"] is False
    assert res["decision"] == "gate_approved"
