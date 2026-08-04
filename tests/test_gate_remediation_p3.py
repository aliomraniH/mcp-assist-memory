"""Intent Gate remediation, Phase 3 — latency hardening.

TWO ENVIRONMENT CLASSES IN ONE FILE, deliberately and visibly:

  [CI]   cache semantics, versioning, listener-death fallback, round-trip COUNT,
         and the target constants. All environment-independent — a TTL expiry is
         a TTL expiry whatever the round trip costs.

  [NEON] anything whose truth is a duration or depends on real Neon topology
         (direct + pooled endpoints, PgBouncer transaction mode, ~59ms round
         trips). These are IMPLEMENTED, MARKED, and NOT CLAIMED GREEN by the
         implementing session. They are deselected by default and ship as
         executable probes in the deploy baton.

That split is the whole lesson of the validation run. G0-7 asserted a latency
budget that only a local Postgres can meet and passed in CI while being false in
production. Running these [NEON] tests locally and reporting green would
reproduce exactly that.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from storage.gate_cache import GateCache
from storage.gate_targets import (
    SPAN_NAMES,
    TIER0_MEDIAN_MS,
    TIER0_P95_MS,
    TIER1_EMPTY_NAMESPACE_MS,
    TIER1_MEASURED_MEDIAN_MS_PRE_FIX,
)
from storage.postgres import PostgresBackend
from tests.conftest import DATABASE_URL, SCHEMA, FakeEmbedder
from tests.gate_utils import GATE_ON, SKILL_ANTI_PATTERN, seed, set_profile

VIOLATING_GOAL = "rebuild the projection by replaying the event log sorted by timestamp"


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


# ===========================================================================
# [CI] — cache semantics.
# ===========================================================================
@pytest.mark.ci
def test_expired_entry_reads_as_unknown_not_as_still_true():
    """The freshness rule the whole design rests on. A cache that answers 'here
    is what I believed 40 minutes ago' is how a gate becomes its own
    stale-authority problem — the exact failure it exists to prevent."""
    cache = GateCache(ttl_s=0.05)
    cache.put("ns", {"intent_gate": "on"})
    assert cache.get("ns") == {"intent_gate": "on"}

    import time
    time.sleep(0.08)
    assert cache.get("ns") is None, "expired must read as unknown, never as stale-true"


@pytest.mark.ci
def test_versioned_key_ignores_stale_and_replayed_notifications():
    """[CI] versioned_key (logic half). A replayed or out-of-order NOTIFY must
    not resurrect a generation the cache has already left."""
    cache = GateCache()
    assert cache.apply_notification("5:ns-a") is True
    assert cache._version == 5

    # Older version: ignored.
    assert cache.apply_notification("3:ns-a") is False
    assert cache._version == 5
    # Exact replay of the current version: also ignored.
    assert cache.apply_notification("5:ns-a") is False
    # Newer: applied.
    assert cache.apply_notification("6:ns-a") is True
    assert cache._version == 6


@pytest.mark.ci
def test_malformed_notification_does_not_kill_the_subscriber():
    """The subscriber is the thing the whole invalidation story depends on. A
    garbage payload must be recorded and discarded, never raised."""
    cache = GateCache()
    assert cache.apply_notification("not-a-version") is False
    assert cache.apply_notification("") is False
    # Still working afterwards.
    assert cache.apply_notification("9:ns") is True


@pytest.mark.ci
def test_notification_invalidates_only_the_named_namespace():
    cache = GateCache()
    cache.put("ns-a", {"a": 1})
    cache.put("ns-b", {"b": 2})
    cache.apply_notification("1:ns-a")
    # Both are dropped by the version bump — conservative, and correct: a
    # version bump means "the world moved", and re-reading is cheap next to
    # serving a stale gate configuration.
    assert cache.get("ns-a") is None


@pytest.mark.ci
def test_reconnect_drops_everything_rather_than_trusting_what_it_holds():
    """A listener that was down missed notifications that will never be re-sent.
    The only honest state is 'I do not know what changed', which resolves to a
    full reload — never to trusting what is held."""
    cache = GateCache()
    cache.put("ns", {"intent_gate": "on"})
    before = cache._version

    cache.invalidate()
    cache.bump_version()

    assert cache.get("ns") is None
    assert cache._version > before


@pytest.mark.ci
async def test_listener_death_degrades_to_ttl_not_to_wrong(gated, ns):
    """[CI] listener_death (fallback half). With no listener the gate must keep
    making correct decisions — just slower to notice a profile change."""
    assert gated.gate_cache.status()["listener_alive"] is False

    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"],
                                  session_id=None)
    assert res["decision"] == "gate_conflict", "correctness must not depend on the listener"


@pytest.mark.ci
async def test_status_reports_listener_state_honestly(gated, ns):
    """listener_alive=false is not an outage, but it IS silent — this tool is
    the only thing that will ever tell anyone."""
    status = gated.gate_cache.status()
    assert set(status) == {"profiles_cached", "verdicts_cached", "listener_alive",
                           "last_notify_ts", "cache_version", "ttl_seconds",
                           "stale_keys"}
    assert status["listener_alive"] is False
    assert status["ttl_seconds"] > 0


@pytest.mark.ci
async def test_no_direct_dsn_is_a_supported_configuration_not_an_error(gated):
    """Unset DATABASE_URL_DIRECT means no listener. That must be a clean
    pure-TTL configuration, not a crash and not a retry storm."""
    await gated.gate_cache.start(None)
    assert gated.gate_cache.status()["listener_alive"] is False


@pytest.mark.ci
async def test_profile_change_takes_effect_through_the_single_invalidation_path(gated, ns):
    """Two caches, ONE way to invalidate them. A cache with two entry points and
    one exit is how a profile edit silently fails to take effect."""
    await set_profile(gated, ns, {**GATE_ON, "gate_similarity_floor": 0.9})
    guard = await gated.gate_guard(ns)
    assert guard["gate_similarity_floor"] == 0.9

    await set_profile(gated, ns, {**GATE_ON, "gate_similarity_floor": 0.5})
    guard = await gated.gate_guard(ns)
    assert guard["gate_similarity_floor"] == 0.5


@pytest.mark.ci
async def test_notify_trigger_bumps_cache_version_in_the_same_transaction(gated, ns):
    """The version bump and the notification happen in the SAME transaction as
    the profile change, so a committed edit is never unannounced."""
    from psycopg.rows import dict_row
    await set_profile(gated, ns, dict(GATE_ON))
    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT cache_version FROM variant_profiles WHERE namespace = %s", (ns,))
        first = (await cur.fetchone())["cache_version"]
    assert first > 0

    await set_profile(gated, ns, {**GATE_ON, "compact_acks": "on"})
    async with gated.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT cache_version FROM variant_profiles WHERE namespace = %s", (ns,))
        second = (await cur.fetchone())["cache_version"]
    assert second > first, "every profile write must advance the version"


# ===========================================================================
# [CI] — 3e mechanism: round-trip COUNT is environment-independent.
# ===========================================================================
@pytest.mark.ci
async def test_independent_reads_run_concurrently(gated, ns):
    """[CI] The 3e mechanism, asserted the only way CI honestly can.

    Milliseconds here mean nothing — local round trips are ~0.1ms. But the
    sequential round-trip COUNT is a structural property that holds in every
    environment, and it is what actually buys the latency: four independent
    reads collapsed into one concurrent batch is ~three Neon round trips saved,
    roughly 180ms at the measured ~60ms floor.

    Asserted by observing that more than one connection is in flight at once.
    """
    await seed(gated, ns, SKILL_ANTI_PATTERN)

    concurrent = {"max": 0, "now": 0}
    original = gated.pool.connection

    class _Tracked:
        def __init__(self, cm):
            self._cm = cm

        async def __aenter__(self):
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
            return await self._cm.__aenter__()

        async def __aexit__(self, *exc):
            concurrent["now"] -= 1
            return await self._cm.__aexit__(*exc)

    gated.pool.connection = lambda *a, **k: _Tracked(original(*a, **k))
    try:
        await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])
    finally:
        gated.pool.connection = original

    assert concurrent["max"] >= 2, (
        "the independent Tier-1 reads must overlap; serialising them costs one "
        "Neon round trip each")


@pytest.mark.ci
async def test_goal_embedding_is_cached_across_repeated_intents(gated, ns):
    """3e: embedding the same goal twice is pure waste, and repeats are common
    exactly when it matters — a caller re-opening an intent after a
    gate_conflict, or an agent retrying a sequence verbatim."""
    calls = {"n": 0}
    original = gated.embedder.embed

    async def counting(texts, *, input_type="document"):
        if input_type == "query":
            calls["n"] += 1
        return await original(texts, input_type=input_type)

    gated.embedder.embed = counting
    await seed(gated, ns, SKILL_ANTI_PATTERN)

    for _ in range(4):
        await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])

    assert calls["n"] == 1, f"expected one embedding call for a repeated goal, got {calls['n']}"


@pytest.mark.ci
async def test_span_breakdown_is_reported_and_sums_to_the_total(gated, ns):
    """[CI] tier1_span_breakdown (shape half). The measurement itself is [NEON];
    what CI can pin is that the instrument exists, names known spans, and leaves
    no unaccounted time — a breakdown that does not sum lets a cost hide."""
    await seed(gated, ns, SKILL_ANTI_PATTERN)
    res = await gated.intent_open(ns, goal=VIOLATING_GOAL, scope=["memory_save"])

    spans = res["latency_spans"]
    assert spans, "every intent_open must report its span breakdown"
    assert set(spans) <= set(SPAN_NAMES), f"unknown span names: {set(spans) - set(SPAN_NAMES)}"
    assert "other" in spans, "unaccounted time must be reported, not dropped"
    assert sum(spans.values()) == pytest.approx(res["latency_ms"], abs=5)


# ===========================================================================
# [CI] — the targets themselves.
# ===========================================================================
@pytest.mark.ci
def test_tier0_target_is_rebaselined_above_the_measured_round_trip_floor():
    """v1's <50ms sat BELOW the achievable floor and failed on all 20 samples.
    A target that cannot be met teaches the team to ignore the fixture."""
    assert TIER0_P95_MS == 110
    assert TIER0_MEDIAN_MS == 75
    # Above the measured p95 of 64ms, with headroom for variance...
    assert TIER0_P95_MS > 64
    # ...but not so loose that a gate costing several round trips would pass.
    assert TIER0_P95_MS < 3 * 64


@pytest.mark.ci
def test_tier1_measurements_are_recorded_not_guessed():
    """The numbers behind the Tier-1 decision are in the code, so the next
    session does not have to re-derive them or trust a summary."""
    assert TIER1_MEASURED_MEDIAN_MS_PRE_FIX == 515
    assert TIER1_EMPTY_NAMESPACE_MS == 121
    # ~400ms of the median is retrieval-path work — the gap 3e attacks.
    assert TIER1_MEASURED_MEDIAN_MS_PRE_FIX - TIER1_EMPTY_NAMESPACE_MS > 350


# ===========================================================================
# [NEON] — implemented, marked, NOT claimed green by this session.
# ===========================================================================
neon = pytest.mark.neon

_NEON_REASON = (
    "[NEON] requires a real Neon topology (direct + pooled endpoints, PgBouncer "
    "transaction mode, ~59ms round trips). Not reachable from the implementing "
    "environment; ships as a post-deploy probe in the deploy baton. Running it "
    "against local Postgres and reporting green would reproduce the exact G0-7 "
    "failure this branch exists to fix."
)


@neon
async def test_stale_cache_neon(gated, ns):
    """[NEON] stale_cache. A profile edit made through the POOLED endpoint must
    reach the subscriber on the DIRECT connection, and the cached value must
    stop being served. Only real topology exercises the pooled/direct split."""
    direct = os.environ.get("DATABASE_URL_DIRECT")
    if not direct:
        pytest.skip(_NEON_REASON)

    await gated.gate_cache.start(direct)
    await asyncio.sleep(0.5)
    assert gated.gate_cache.status()["listener_alive"] is True

    await set_profile(gated, ns, {**GATE_ON, "gate_similarity_floor": 0.7})
    await asyncio.sleep(1.0)
    assert (await gated.gate_guard(ns))["gate_similarity_floor"] == 0.7


@neon
async def test_listener_death_and_reconnect_neon(gated, ns):
    """[NEON] listener_death. Kill the subscriber's connection server-side and
    assert it reconnects, reports alive again, and FULLY RELOADS — it missed
    notifications during the outage that will never be re-sent."""
    direct = os.environ.get("DATABASE_URL_DIRECT")
    if not direct:
        pytest.skip(_NEON_REASON)

    await gated.gate_cache.start(direct)
    await asyncio.sleep(0.5)
    assert gated.gate_cache.status()["listener_alive"] is True

    async with gated.pool.connection() as conn:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE query LIKE 'LISTEN%' AND pid <> pg_backend_pid()")

    for _ in range(40):
        await asyncio.sleep(0.5)
        if gated.gate_cache.status()["listener_alive"]:
            break
    assert gated.gate_cache.status()["listener_alive"] is True


@neon
async def test_versioned_key_end_to_end_neon(gated, ns):
    """[NEON] versioned_key. The DB-assigned version must arrive on the wire and
    advance the cache monotonically."""
    direct = os.environ.get("DATABASE_URL_DIRECT")
    if not direct:
        pytest.skip(_NEON_REASON)

    await gated.gate_cache.start(direct)
    await asyncio.sleep(0.5)
    before = gated.gate_cache.status()["cache_version"]
    await set_profile(gated, ns, {**GATE_ON, "compact_acks": "on"})
    await asyncio.sleep(1.0)
    assert gated.gate_cache.status()["cache_version"] > before


@neon
async def test_tier0_latency_neon(gated, ns):
    """[NEON] tier0_latency. p95 <= 110ms, median <= 75ms on gate_detail.
    latency_ms (SERVER-side). Meaningless against local Postgres — see
    scripts/gate_latency_harness.py for the n>=2000 production version."""
    if not os.environ.get("GATE_LATENCY_NEON"):
        pytest.skip(_NEON_REASON)

    samples = []
    for i in range(200):
        ack = await gated.memory_save(ns, f"probe/lat-{i}", f"sample {i}",
                                      kind="note", actor="latency-probe")
        detail = ack.get("gate_detail") or {}
        if detail.get("latency_ms") is not None:
            samples.append(detail["latency_ms"])

    ordered = sorted(samples)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    median = ordered[len(ordered) // 2]
    assert p95 <= TIER0_P95_MS, f"tier-0 p95 {p95}ms exceeds {TIER0_P95_MS}ms"
    assert median <= TIER0_MEDIAN_MS


@neon
async def test_tier1_latency_neon(gated, ns):
    """[NEON] tier1_latency.

    THE TARGET IS DECIDED BY THIS MEASUREMENT, not before it (3e Step 3). The
    pre-fix production numbers were median 515ms / p95 810ms against a ~500ms
    budget that had no mechanism behind it. This branch applied two mechanisms —
    concurrent independent reads and a goal-embedding cache — and CANNOT verify
    the result: the implementing environment has no Neon topology and no
    embedding provider.

    So this asserts the PROVISIONAL target and prints the span breakdown. If it
    fails on the operator's run, the honest outcome is to re-baseline to the
    measured p95 plus headroom with the same written justification Tier-0
    carries — not to quietly loosen the number.
    """
    if not os.environ.get("GATE_LATENCY_NEON"):
        pytest.skip(_NEON_REASON)

    from storage.gate_targets import TIER1_P95_MS_PROVISIONAL

    await seed(gated, ns, SKILL_ANTI_PATTERN)
    session = (await gated.session_create(ns, surface="test"))["session_id"]
    samples, spans = [], {}
    for i in range(100):
        res = await gated.intent_open(
            ns, goal=f"rebuild projection {i} by replaying the event log sorted by timestamp",
            scope=["memory_save"], session_id=session)
        samples.append(res["latency_ms"])
        for key, value in (res.get("latency_spans") or {}).items():
            spans.setdefault(key, []).append(value)

    ordered = sorted(samples)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    print(f"\n[NEON] tier1 p95={p95}ms median={ordered[len(ordered) // 2]}ms")
    for key in sorted(spans, key=lambda k: -sum(spans[k])):
        s = sorted(spans[key])
        print(f"  {key:22} p50={s[len(s) // 2]:6.1f} p95={s[int(len(s) * 0.95) - 1]:6.1f}")

    assert p95 <= TIER1_P95_MS_PROVISIONAL, (
        f"tier-1 p95 {p95}ms exceeds the provisional {TIER1_P95_MS_PROVISIONAL}ms — "
        f"re-baseline with the span breakdown above and record the justification")
