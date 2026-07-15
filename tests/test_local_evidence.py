"""v3 P0/1 item 6 — local evidence states: local_attested → pending_remote →
remote_confirmed, with promotion ONLY via the resolver observing the sha
remotely.

Fixtures are SYNTHETIC by design: the motivating 5cb3d29 case resolved itself
(content merged via PR #1, bundle commits abandoned) and must not be referenced
as live state.
"""
from __future__ import annotations

import pytest

from errors import AppError

REPO = "acme/widget"
LOCAL_SHA = "5cb3d29aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # synthetic 40-hex shape


def _attested_meta(**extra):
    return {
        "repo": REPO, "branch": "main", "repo_sha": LOCAL_SHA,
        "evidence_state": "local_attested",
        "attestation": {"sha": LOCAL_SHA, "method": "local_git",
                        "attested_at": "2026-07-15T00:00:00Z",
                        "command_hash": "c" * 64, "evidence_hash": "e" * 64},
        **extra,
    }


async def test_local_attested_never_satisfies_a_verification_gate(reconcile_backend, ns):
    """The hard rule: even when the branch head happens to MATCH the attested
    sha, an unobserved locally-attested commit can never read current."""
    b = reconcile_backend
    b.resolver.heads[(REPO, "main")] = LOCAL_SHA  # head "matches" ...
    # ... but the commit itself is NOT observable remotely (no commits entry).
    await b.memory_save(ns, "push/pending", {"pushed": "4 commits"},
                        kind="claim", meta=_attested_meta())
    v = (await b.coord_reconcile(ns))["verdicts"][0]
    assert v["state"] == "unverifiable"
    assert v["evidence"]["observed_remotely"] is False
    assert v["evidence"]["recorded_state"] == "local_attested"
    assert "never verification" in v["reason"]


async def test_promotion_only_via_resolver_observation(reconcile_backend, ns):
    b = reconcile_backend
    b.resolver.heads[(REPO, "main")] = LOCAL_SHA
    b.resolver.commits[(REPO, LOCAL_SHA)] = LOCAL_SHA  # now observable remotely
    await b.memory_save(ns, "push/pending", {"pushed": "4 commits"},
                        kind="claim", meta=_attested_meta())
    v = (await b.coord_reconcile(ns))["verdicts"][0]
    assert v["state"] == "current"  # subject verdict proceeds normally now
    assert v["evidence"]["observed_remotely"] is True
    assert v["evidence"]["promoted_to"] == "remote_confirmed"


async def test_pending_remote_behaves_the_same(reconcile_backend, ns):
    b = reconcile_backend
    await b.memory_save(ns, "push/pending", {"p": 1}, kind="claim",
                        meta=_attested_meta(evidence_state="pending_remote"))
    v = (await b.coord_reconcile(ns))["verdicts"][0]
    assert v["state"] == "unverifiable"
    assert v["evidence"]["recorded_state"] == "pending_remote"


async def test_remote_confirmed_cannot_be_self_declared(backend, ns):
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1},
                                  meta={"evidence_state": "remote_confirmed"})
    assert err.value.code == "invalid_evidence_state"
    with pytest.raises(AppError):
        await backend.memory_save(ns, "k", {"v": 1},
                                  meta={"evidence_state": "totally_verified"})


async def test_attestation_schema_requires_a_sha(backend, ns):
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1},
                                  meta={"attestation": {"method": "local_git"}})
    assert err.value.code == "invalid_attestation"
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1},
                                  meta={"attestation": {"sha": "not-hex"}})
    assert err.value.code == "invalid_sha"


async def test_clinical_namespace_rejects_raw_command_fields(backend, ns):
    from psycopg.types.json import Jsonb

    async with backend.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO variant_profiles (namespace, profile) VALUES (%s, %s) "
            "ON CONFLICT (namespace) DO UPDATE SET profile = EXCLUDED.profile",
            (ns, Jsonb({"clinical": True})))
    backend._profile_cache.clear()
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1}, meta={
            "attestation": {"sha": LOCAL_SHA, "command": "git log --oneline"}})
    assert err.value.code == "invalid_attestation"
    # Hashes-only attestation is fine in the same clinical namespace.
    out = await backend.memory_save(ns, "k", {"v": 1}, meta={
        "evidence_state": "local_attested",
        "attestation": {"sha": LOCAL_SHA, "command_hash": "c" * 64}})
    assert out["verified_persisted"] is True


async def test_non_clinical_namespace_allows_raw_fields(backend, ns):
    out = await backend.memory_save(ns, "k", {"v": 1}, meta={
        "attestation": {"sha": LOCAL_SHA, "command": "git rev-parse HEAD"}})
    assert out["verified_persisted"] is True
