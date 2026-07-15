"""v3 P0/1 item 5 — temporal_mode: a claim's time-binding forks reconciliation.

The acceptance pair: a run-record fixture backfilled as historical_snapshot
reaches a TERMINAL non-stale state (sha-exists verification, never compared to
head), while a head_tracking fixture pinned to the same old sha still goes
stale when the head moves.
"""
from __future__ import annotations

import pytest

from errors import AppError

REPO = "acme/widget"
OLD = "398b38aef9d0ce1636993c2be6e3b617a3b3e8b2"   # the milestone the run recorded
NEW = "f" * 40                                       # the branch head has moved on


async def test_snapshot_terminal_non_stale_while_head_tracking_goes_stale(reconcile_backend, ns):
    b = reconcile_backend
    b.resolver.heads[(REPO, "main")] = NEW
    b.resolver.commits[(REPO, OLD)] = OLD  # the old sha still exists upstream

    # The run record, backfilled as a snapshot of the milestone commit...
    await b.memory_save(ns, "run/p2-build", {"milestone": "p2 build complete"},
                        kind="claim",
                        meta={"repo": REPO, "branch": "main", "repo_sha": OLD,
                              "temporal_mode": "historical_snapshot"})
    # ...and the same pin recorded as a head-tracking assertion.
    await b.memory_save(ns, "state/main-branch", {"head": OLD}, kind="claim",
                        meta={"repo": REPO, "branch": "main", "repo_sha": OLD})

    verdicts = {v["key"]: v for v in (await b.coord_reconcile(ns))["verdicts"]}
    snap, tracking = verdicts["run/p2-build"], verdicts["state/main-branch"]

    assert snap["state"] == "current" and snap["terminal"] is True
    assert snap["temporal_mode"] == "historical_snapshot"
    assert snap["temporal_mode_origin"] == "recorded"
    assert snap["subject"] == f"commit:{OLD}"  # the commit, never the head

    assert tracking["state"] == "stale"  # head moved: still goes stale
    assert tracking["temporal_mode"] == "head_tracking"
    assert tracking["temporal_mode_origin"] == "inferred"  # advisory default


async def test_snapshot_with_unobservable_sha_is_unverifiable_not_current(reconcile_backend, ns):
    b = reconcile_backend
    await b.memory_save(ns, "run/x", {"m": 1}, kind="claim",
                        meta={"repo": REPO, "repo_sha": OLD,
                              "temporal_mode": "historical_snapshot"})
    v = (await b.coord_reconcile(ns))["verdicts"][0]
    assert v["state"] == "unverifiable"  # no commits registered in the fake


async def test_snapshot_excluded_from_health_stale_projection(backend, ns):
    # Namespace's most-recently-observed sha is NEW: the snapshot pinning OLD
    # deliberately must not appear in coord_health.stale, while a head-tracking
    # sibling with the same old pin IS still projected stale.
    await backend.memory_save(ns, "run/p2-build", {"m": 1}, kind="claim",
                              meta={"repo": REPO, "repo_sha": OLD,
                                    "temporal_mode": "historical_snapshot"})
    await backend.memory_save(ns, "old-head-claim", {"m": 3}, kind="claim",
                              meta={"repo": REPO, "repo_sha": OLD})
    await backend.memory_save(ns, "current-work", {"m": 2},
                              meta={"repo": REPO, "repo_sha": NEW})
    health = await backend.coord_health(ns)
    assert health["latest_repo_sha"] == NEW
    assert {s["key"] for s in health["stale"]} == {"old-head-claim"}
    # The snapshot also never becomes the namespace's "latest observed" anchor:
    # its deliberate old pin must not drag latest_repo_sha backwards.
    await backend.memory_save(ns, "run/p2-build-2", {"m": 4}, kind="claim",
                              meta={"repo": REPO, "repo_sha": OLD,
                                    "temporal_mode": "historical_snapshot"})
    health = await backend.coord_health(ns)
    assert health["latest_repo_sha"] == NEW


async def test_timeless_and_interval_forks(reconcile_backend, ns):
    b = reconcile_backend
    await b.memory_save(ns, "fact", {"pi": "3.14159"}, kind="claim",
                        meta={"repo": REPO, "branch": "main",
                              "temporal_mode": "timeless"})
    await b.memory_save(ns, "window", {"valid": "Q3"}, kind="claim",
                        meta={"repo": REPO, "branch": "main",
                              "temporal_mode": "interval"})
    verdicts = {v["key"]: v for v in (await b.coord_reconcile(ns))["verdicts"]}
    assert verdicts["fact"]["state"] == "current" and verdicts["fact"]["terminal"] is True
    assert verdicts["window"]["state"] == "unverifiable"  # not mechanized: never guessed


async def test_invalid_temporal_mode_rejected_at_the_boundary(backend, ns):
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1},
                                  meta={"temporal_mode": "forever"})
    assert err.value.code == "invalid_temporal_mode"


async def test_mode_is_projected_and_returned(backend, ns):
    out = await backend.memory_save(ns, "run/x", {"m": 1}, kind="claim",
                                    meta={"repo": REPO, "repo_sha": OLD,
                                          "temporal_mode": "historical_snapshot"})
    assert out["temporal_mode"] == "historical_snapshot"
    got = await backend.memory_get(ns, "run/x")
    assert got["temporal_mode"] == "historical_snapshot"


async def test_snapshot_skips_the_stale_pin_advisory(reconcile_backend, ns):
    from psycopg.types.json import Jsonb

    b = reconcile_backend
    async with b.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO variant_profiles (namespace, profile) VALUES (%s, %s) "
            "ON CONFLICT (namespace) DO UPDATE SET profile = EXCLUDED.profile",
            (ns, Jsonb({"advisory_mode": "full"})))
    b._profile_cache.clear()
    b.resolver.heads[(REPO, "main")] = NEW
    out = await b.memory_save(ns, "run/x", {"m": 1}, kind="claim",
                              meta={"repo": REPO, "branch": "main", "repo_sha": OLD,
                                    "temporal_mode": "historical_snapshot"})
    assert "advisories" not in out  # deliberately-old pin: no stale_pin noise
