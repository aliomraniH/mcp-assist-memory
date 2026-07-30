"""Intent Gate — GitHub awakening boundary (spec GH-1..GH-4, doctrine S6).

Zero GitHub calls from Tier 0/1 except the single lazily-awakened resolver hop,
asserted via telemetry counters (tool_events tool='gate_awaken' + the in-process
counter), never via absence of errors. All tests fail at the pre-gate baseline.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tests.conftest import DATABASE_URL, SCHEMA, FakeResolver
from storage.postgres import PostgresBackend
from tests.gate_utils import GATE_ON, set_profile

REPO = "aliomraniH/mcp-assist-memory"
OLD_SHA = "0d0fe9b291c9b3eaeb413d6a2617be8e6b70fb8b"
HEAD_SHA = "4bd1fc1e666ffe9fa337b075b2986d665832fd57"


@pytest_asyncio.fixture
async def gated(ns):
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=4)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    resolver = FakeResolver()
    resolver.heads[(REPO, "main")] = HEAD_SHA
    resolver.commits[(REPO, OLD_SHA)] = OLD_SHA
    resolver.commits[(REPO, HEAD_SHA)] = HEAD_SHA
    backend = PostgresBackend(pool, resolver=resolver)
    await set_profile(backend, ns, dict(GATE_ON))
    yield backend
    await pool.close()


async def _awaken_telemetry_count(backend, ns) -> int:
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT count(*) AS n FROM tool_events WHERE tool = 'gate_awaken' "
            "AND namespace = %s", (ns,))
        return (await cur.fetchone())["n"]


async def _seed_claim_with_verdict(backend, ns):
    await backend.memory_save(
        ns, "claim/probe-head", "gate-probe anchor claim", kind="claim",
        meta={"repo": REPO, "branch": "main", "repo_sha": OLD_SHA})
    await backend.coord_reconcile(ns)


async def test_gh_1_non_coding_intent_zero_github_calls(gated, ns):
    before = await _awaken_telemetry_count(gated, ns)
    ack = await gated.memory_save(
        ns, "probe/noncoding", "workshop scheduling note, no repo refs anywhere",
        kind="knowledge")
    assert ack["gate"]["decision"] == "gate_approved"
    assert gated.gate_awaken_count == 0
    assert await _awaken_telemetry_count(gated, ns) - before == 0


async def test_gh_2_coding_intent_expired_verdict_awakens_once(gated, ns):
    await set_profile(gated, ns, {**GATE_ON, "claim_staleness_hours": 0.000001})
    await _seed_claim_with_verdict(gated, ns)
    before = await _awaken_telemetry_count(gated, ns)
    import time
    t0 = time.monotonic()
    ack = await gated.memory_save(
        ns, "probe/coding-stale", "notes on head state", kind="knowledge",
        meta={"repo": REPO, "derived_from": ["claim/probe-head"]})
    wall_ms = (time.monotonic() - t0) * 1000
    assert gated.gate_awaken_count == 1  # exactly once
    assert await _awaken_telemetry_count(gated, ns) - before == 1
    assert wall_ms <= 2000 + 500  # resolver hop bounded by the 2s budget
    assert "stale_context" in ack["gate"]["flags"]
    awaken = ack["gate_detail"]["awaken"]
    assert awaken["status"] == "ok"
    assert awaken["resolved"]["head"] == HEAD_SHA


async def test_gh_2_timeout_still_returns_with_stale_context(gated, ns, monkeypatch):
    from storage import awaken as awaken_mod
    monkeypatch.setattr(awaken_mod, "AWAKEN_BUDGET_S", 0.05)

    class SlowResolver(FakeResolver):
        async def branch_head(self, repo, branch):
            await asyncio.sleep(0.5)
            return HEAD_SHA

    await set_profile(gated, ns, {**GATE_ON, "claim_staleness_hours": 0.000001})
    await _seed_claim_with_verdict(gated, ns)
    gated.resolver = SlowResolver()
    ack = await gated.memory_save(
        ns, "probe/coding-timeout", "notes", kind="knowledge",
        meta={"repo": REPO, "derived_from": ["claim/probe-head"]})
    assert ack["verified_persisted"] is True  # never blocks the write
    assert "stale_context" in ack["gate"]["flags"]
    assert ack["gate_detail"]["awaken"]["status"] == "timeout"


async def test_gh_3_resolver_dead_degrades_to_stored_verdicts(gated, ns):
    class DeadResolver(FakeResolver):
        async def branch_head(self, repo, branch):
            raise RuntimeError("resolver down")

    await set_profile(gated, ns, {**GATE_ON, "claim_staleness_hours": 0.000001})
    await _seed_claim_with_verdict(gated, ns)
    gated.resolver = DeadResolver()
    ack = await gated.memory_save(
        ns, "probe/resolver-dead", "notes", kind="knowledge",
        meta={"repo": REPO, "derived_from": ["claim/probe-head"]})
    assert ack["verified_persisted"] is True  # no block, no protocol error
    assert "stale_context" in ack["gate"]["flags"]
    assert ack["gate_detail"]["awaken"]["status"] == "unresolved"


async def test_gh_4_fresh_verdicts_no_resolver_call(gated, ns):
    await set_profile(gated, ns, {**GATE_ON, "claim_staleness_hours": 9999})
    await _seed_claim_with_verdict(gated, ns)
    before_inproc = gated.gate_awaken_count
    before = await _awaken_telemetry_count(gated, ns)
    ack = await gated.memory_save(
        ns, "probe/coding-fresh", "notes", kind="knowledge",
        meta={"repo": REPO, "derived_from": ["claim/probe-head"]})
    assert ack["gate"]["decision"] == "gate_approved"
    assert gated.gate_awaken_count - before_inproc == 0
    assert await _awaken_telemetry_count(gated, ns) - before == 0


async def test_module_boundary_gate_never_imports_github(gated):
    """S6 enforced by module boundary, not convention: storage/gate.py must not
    import the resolver/reconcile/awaken machinery at module level."""
    import storage.gate as gate_mod
    src = open(gate_mod.__file__).read()
    for forbidden in ("storage.reconcile", "storage.awaken", "storage.github_token",
                      "import httpx"):
        assert forbidden not in src, f"gate.py must not reference {forbidden}"
