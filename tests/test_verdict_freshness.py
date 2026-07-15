"""v3 P0/1 item 2 — verdict freshness travels WITH the verdict read.

Motivating incident (S6): a context pack trusted a stale coord/_reconcile
snapshot of a since-repaired claim — verdict decay misleading a real consumer.
skill-transfer verdicts were observed at 289.2h against a 72h window. A reader
must see expiry inline on the verdict read itself, without having to know to
call coord_health first.
"""
from __future__ import annotations

REPO = "acme/widget"
FULL = "0d0fe9b291c9b3eaeb413d6a2617be8e6b70fb8b"


async def _reconciled_verdict_key(b, ns, key="phase1"):
    b.resolver.pulls[(REPO, 11)] = {"merged": True, "merge_sha": FULL}
    await b.memory_save(ns, key, {"done": True}, kind="claim",
                        meta={"repo": REPO, "pr": 11, "merge_sha": FULL})
    await b.coord_reconcile(ns)
    return f"coord/_reconcile/{key}"


async def _age_row(b, ns, key, hours):
    async with b.pool.connection() as conn:
        await conn.execute(
            "UPDATE memory_entry SET created_at = now() - make_interval(hours => %s) "
            "WHERE namespace = %s AND key = %s",
            (hours, ns, key),
        )


async def test_fresh_verdict_reads_fresh_with_age(reconcile_backend, ns):
    vkey = await _reconciled_verdict_key(reconcile_backend, ns)
    rec = await reconcile_backend.memory_get(ns, vkey)
    assert rec["freshness"] == "fresh"
    assert rec["checked_at"] == rec["created_at"]
    assert rec["age_hours"] < 1.0
    # The stored verdict value is self-dating too (checked_at written in-band).
    assert "checked_at" in rec["value"]


async def test_expired_verdict_surfaces_inline_without_coord_health(reconcile_backend, ns):
    """The item's acceptance test: a verdict aged past the namespace window
    reads freshness:"expired" from a plain memory_get — no coord_health call."""
    vkey = await _reconciled_verdict_key(reconcile_backend, ns)
    await _age_row(reconcile_backend, ns, vkey, 289)  # the observed skill-transfer age
    rec = await reconcile_backend.memory_get(ns, vkey)
    assert rec["freshness"] == "expired"
    assert 288 < rec["age_hours"] < 291
    assert rec["checked_at"] == rec["created_at"]


async def test_expired_annotation_on_list_and_history_reads(reconcile_backend, ns):
    vkey = await _reconciled_verdict_key(reconcile_backend, ns)
    await _age_row(reconcile_backend, ns, vkey, 100)  # > default 72h window
    page = await reconcile_backend.memory_list_page(ns, prefix="coord/_reconcile/")
    verdicts = {e["key"]: e for e in page["entries"]}
    assert verdicts[vkey]["freshness"] == "expired"
    hist = await reconcile_backend.memory_history(ns, vkey)
    assert hist[0]["freshness"] == "expired" and hist[0]["age_hours"] > 99


async def test_namespace_staleness_window_is_respected(reconcile_backend, ns):
    from psycopg.types.json import Jsonb

    b = reconcile_backend
    async with b.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO variant_profiles (namespace, profile) VALUES (%s, %s) "
            "ON CONFLICT (namespace) DO UPDATE SET profile = EXCLUDED.profile",
            (ns, Jsonb({"claim_staleness_hours": 500})))
    b._profile_cache.clear()
    vkey = await _reconciled_verdict_key(b, ns)
    await _age_row(b, ns, vkey, 289)
    rec = await b.memory_get(ns, vkey)
    assert rec["freshness"] == "fresh"  # 289h < the namespace's 500h window


async def test_non_verdict_entries_are_not_annotated(backend, ns):
    await backend.memory_save(ns, "plain", {"v": 1})
    rec = await backend.memory_get(ns, "plain")
    assert "freshness" not in rec and "age_hours" not in rec and "checked_at" not in rec
