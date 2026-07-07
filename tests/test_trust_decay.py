"""Phase 6: staleness demotion (T6.1) and the too-clean heuristic (T6.2)."""
from __future__ import annotations

from psycopg.types.json import Jsonb


async def _age_verdict(backend, ns, claim_key, hours):
    """Backdate the reconcile verdict for a claim (test-only time travel)."""
    async with backend.pool.connection() as conn:
        await conn.execute(
            "UPDATE memory_entry SET created_at = now() - make_interval(hours => %s) "
            "WHERE namespace = %s AND key = %s",
            (hours, ns, f"coord/_reconcile/{claim_key}"))


async def test_never_reconciled_claim_needs_reverification(backend, ns):
    await backend.memory_save(ns, "claim/a", {"c": "x"}, kind="claim",
                              meta={"repo": "o/r", "pr": 1})
    health = await backend.coord_health(ns)
    flags = {f["key"]: f for f in health["needs_reverification"]}
    assert flags["claim/a"]["reason"] == "never_reconciled"
    assert health["claim_staleness_hours"] == 72


async def test_fresh_current_verdict_is_not_demoted(reconcile_backend, ns):
    b = reconcile_backend
    await b.memory_save(ns, "claim/pr1", {"c": "merged"}, kind="claim",
                        meta={"repo": "o/r", "pr": 1, "merge_sha": "abc1234"})
    b.resolver.pulls[("o/r", 1)] = {"merged": True, "merge_sha": "abc1234" + "0" * 33}
    await b.coord_reconcile(ns)
    health = await b.coord_health(ns)
    assert health["needs_reverification"] == []


async def test_expired_current_verdict_is_demoted(reconcile_backend, ns):
    b = reconcile_backend
    await b.memory_save(ns, "claim/pr1", {"c": "merged"}, kind="claim",
                        meta={"repo": "o/r", "pr": 1, "merge_sha": "abc1234"})
    b.resolver.pulls[("o/r", 1)] = {"merged": True, "merge_sha": "abc1234" + "0" * 33}
    await b.coord_reconcile(ns)
    await _age_verdict(b, ns, "claim/pr1", hours=100)  # past the 72h default
    health = await b.coord_health(ns)
    flags = {f["key"]: f for f in health["needs_reverification"]}
    assert flags["claim/pr1"]["reason"] == "verdict_expired"
    assert flags["claim/pr1"]["last_verdict_state"] == "current"  # even `current` expires
    assert flags["claim/pr1"]["verdict_age_hours"] > 72


async def test_staleness_window_is_per_namespace(reconcile_backend, ns):
    b = reconcile_backend
    async with b.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO variant_profiles (namespace, profile) VALUES (%s, %s) "
            "ON CONFLICT (namespace) DO UPDATE SET profile = EXCLUDED.profile",
            (ns, Jsonb({"claim_staleness_hours": 168})))
    await b.memory_save(ns, "claim/pr1", {"c": "merged"}, kind="claim",
                        meta={"repo": "o/r", "pr": 1, "merge_sha": "abc1234"})
    b.resolver.pulls[("o/r", 1)] = {"merged": True, "merge_sha": "abc1234" + "0" * 33}
    await b.coord_reconcile(ns)
    await _age_verdict(b, ns, "claim/pr1", hours=100)  # inside the widened window
    health = await b.coord_health(ns)
    assert health["claim_staleness_hours"] == 168
    assert health["needs_reverification"] == []


async def test_unverifiable_verdict_stays_flagged_but_current_does_not(reconcile_backend, ns):
    """An `unverifiable` verdict is not a clean bill of health — the claim was
    never actually confirmed. It must stay in needs_reverification even while the
    verdict is fresh, so a permanently-unverifiable claim doesn't read as
    'handled' for the whole staleness window. A fresh `current` verdict in the
    same namespace is still NOT flagged (the fix doesn't over-broaden). Finding 2.
    """
    b = reconcile_backend
    # (1) resolves CURRENT — provenance is present and matches upstream.
    await b.memory_save(ns, "claim/ok", {"c": "merged"}, kind="claim",
                        meta={"repo": "o/r", "pr": 1, "merge_sha": "abc1234"})
    b.resolver.pulls[("o/r", 1)] = {"merged": True, "merge_sha": "abc1234" + "0" * 33}
    # (2) no resolvable provenance (subject only, no repo) -> UNVERIFIABLE.
    await b.memory_save(ns, "claim/vague", {"c": "ci is green"}, kind="claim",
                        meta={"subject": "ci"})

    out = await b.coord_reconcile(ns)
    states = {v["key"]: v["state"] for v in out["verdicts"]}
    assert states["claim/vague"] == "unverifiable"  # sanity on the fixture
    assert states["claim/ok"] == "current"

    health = await b.coord_health(ns)
    flags = {f["key"]: f for f in health["needs_reverification"]}
    # unverifiable surfaces immediately, on a brand-new (in-window) verdict...
    assert flags["claim/vague"]["reason"] == "unverifiable"
    assert flags["claim/vague"]["last_verdict_state"] == "unverifiable"
    assert flags["claim/vague"]["verdict_age_hours"] < 72
    # ...while the fresh `current` verdict is left alone.
    assert "claim/ok" not in flags


async def test_skepticism_all_current_verdicts(reconcile_backend, ns):
    b = reconcile_backend
    for i in range(21):
        await b.memory_save(ns, f"claim/pr{i}", {"c": f"merged {i}"}, kind="claim",
                            meta={"repo": "o/r", "pr": i, "merge_sha": f"abc{i:04d}"})
        b.resolver.pulls[("o/r", i)] = {"merged": True, "merge_sha": f"abc{i:04d}" + "0" * 33}
    await b.coord_reconcile(ns)
    health = await b.coord_health(ns)
    skept = health["skepticism"]
    assert skept["all_verdicts_current"]["claims"] == 21
    # informational only: nothing else changed shape
    assert health["needs_reverification"] == []


async def test_skepticism_identical_content_from_different_actors(backend, ns):
    for i in range(5):
        await backend.memory_save(ns, f"echo/{i}", {"result": "all tests pass"},
                                  actor=f"agent-{i}")
    health = await backend.coord_health(ns)
    runs = health["skepticism"]["identical_content_runs"]
    assert runs and runs[0]["count"] == 5
    assert len(runs[0]["actors"]) == 5


async def test_no_skepticism_on_healthy_namespace(backend, ns):
    await backend.memory_save(ns, "a", {"v": 1})
    await backend.memory_save(ns, "b", {"v": 2})
    health = await backend.coord_health(ns)
    assert health["skepticism"] == {}
