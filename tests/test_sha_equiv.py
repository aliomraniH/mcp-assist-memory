"""v3 P0/1 item 1 — the shared SHA-equivalence module and its adoption in every
consumer: coord_reconcile, coord_health's stale projection, the write boundary
(memory_save/_split_meta, which curate flows through), and the R5 advisory.

Acceptance fixture = the V2 live probe (dev/v3-probe-20260714): a claim pinned
to a 7-char prefix of the live head used to read `current` from reconcile and
`stale` from health AT THE SAME TIME. After the fix both consumers must agree.
Regression fixture = the rev-1 claim/p2-build-completion defect shape (a 7-char
abbreviation in the projected repo_sha column).
"""
from __future__ import annotations

import pytest

from errors import AppError
from storage.sha_equiv import (
    FULL_SHA_LEN,
    AmbiguousShaRef,
    canonicalize,
    equivalent,
    is_hex_ref,
    sha_match,
    validate_ref,
)

REPO = "acme/widget"
FULL = "0d0fe9b291c9b3eaeb413d6a2617be8e6b70fb8b"  # the probe's live head shape
PREFIX = FULL[:7]


# ------------------------------------------------------------------ unit rules
def test_sha_match_behavior_unchanged_from_reconcile():
    # The exact contract that moved out of storage.reconcile, verbatim.
    assert sha_match(PREFIX, FULL) is True
    assert sha_match(FULL, PREFIX) is True
    assert sha_match(PREFIX.upper(), FULL) is True
    assert sha_match(FULL, FULL) is True
    assert sha_match("deadbee", FULL) is False
    assert sha_match(FULL[:6], FULL) is False  # below the 7-char trust floor
    assert sha_match(None, FULL) is False
    assert sha_match(PREFIX, None) is False


def test_reconcile_still_reexports_the_shared_rule():
    from storage import reconcile, sha_equiv

    assert reconcile.sha_match is sha_equiv.sha_match


def test_equivalent_adds_exact_equality_for_legacy_short_refs():
    assert equivalent("a1b2c", "a1b2c") is True      # <7: equal to itself
    assert equivalent("a1b2c", "A1B2C") is True      # case-insensitive
    assert equivalent("a1b2c", FULL) is False        # <7 never prefix-matches
    assert equivalent(PREFIX, FULL) is True          # sha_match still applies
    assert equivalent(None, FULL) is False


def test_validate_ref_accepts_abbreviated_and_full_hex():
    assert validate_ref(PREFIX) == PREFIX
    assert validate_ref(FULL.upper()) == FULL        # normalized to lowercase
    assert is_hex_ref("DEADBEE") is True


@pytest.mark.parametrize("bad", ["not-a-sha", "0d0fe9", "g" * 40, "f" * 41, 123, {}, ""])
def test_validate_ref_rejects_non_hex_and_out_of_range(bad):
    with pytest.raises(AppError) as err:
        validate_ref(bad)
    assert err.value.code == "invalid_sha"


async def test_canonicalize_resolves_abbreviation_and_preserves_input():
    class R:
        enabled = True

        async def commit_sha(self, repo, ref):
            return FULL

    canonical, resolved = await canonicalize(PREFIX, repo=REPO, resolver=R())
    assert canonical == FULL and resolved == FULL


async def test_canonicalize_is_best_effort_on_miss_and_disabled():
    class Miss:
        enabled = True

        async def commit_sha(self, repo, ref):
            return None

    class Off:
        enabled = False

    assert await canonicalize(PREFIX, repo=REPO, resolver=Miss()) == (PREFIX, None)
    assert await canonicalize(PREFIX, repo=REPO, resolver=Off()) == (PREFIX, None)
    assert await canonicalize(PREFIX, repo=None, resolver=Miss()) == (PREFIX, None)
    # A full sha never needs the network at all.
    assert await canonicalize(FULL, repo=REPO, resolver=Off()) == (FULL, FULL)


async def test_canonicalize_rejects_wrong_or_malformed_resolution():
    class Wrong:
        enabled = True

        async def commit_sha(self, repo, ref):
            return "e" * FULL_SHA_LEN  # does not extend the abbreviation

    assert await canonicalize(PREFIX, repo=REPO, resolver=Wrong()) == (PREFIX, None)


async def test_ambiguous_abbreviation_is_a_distinct_error():
    class Ambig:
        enabled = True

        async def commit_sha(self, repo, ref):
            raise AmbiguousShaRef(ref)

    with pytest.raises(AppError) as err:
        await canonicalize(PREFIX, repo=REPO, resolver=Ambig())
    assert err.value.code == "ambiguous_sha"


# ------------------------------------------------ write boundary (memory_save)
async def test_write_boundary_rejects_non_hex_repo_sha(backend, ns):
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1}, meta={"repo_sha": "not-a-sha"})
    assert err.value.code == "invalid_sha"


async def test_write_boundary_rejects_too_short_and_base_sha(backend, ns):
    with pytest.raises(AppError):
        await backend.memory_save(ns, "k", {"v": 1}, meta={"repo_sha": FULL[:6]})
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 1}, meta={"repo_sha": FULL, "base_sha": "xyz"})
    assert err.value.code == "invalid_sha"


async def test_write_boundary_rejects_ambiguous_abbreviation(reconcile_backend, ns):
    reconcile_backend.resolver.ambiguous.add(PREFIX)
    with pytest.raises(AppError) as err:
        await reconcile_backend.memory_save(
            ns, "k", {"v": 1}, kind="claim", meta={"repo": REPO, "repo_sha": PREFIX})
    assert err.value.code == "ambiguous_sha"


async def test_write_boundary_canonicalizes_and_preserves_input(reconcile_backend, ns):
    reconcile_backend.resolver.commits[(REPO, PREFIX)] = FULL
    out = await reconcile_backend.memory_save(
        ns, "k", {"v": 1}, meta={"repo": REPO, "repo_sha": PREFIX})
    assert out["repo_sha"] == FULL                       # projected column: canonical
    assert out["meta"]["repo_sha"] == FULL               # envelope agrees
    assert out["meta"]["repo_sha_input"] == PREFIX       # input ref preserved


async def test_write_boundary_without_resolver_stores_validated_abbreviation(backend, ns):
    out = await backend.memory_save(ns, "k", {"v": 1}, meta={"repo": REPO, "repo_sha": PREFIX})
    assert out["repo_sha"] == PREFIX
    assert "repo_sha_input" not in (out["meta"] or {})


async def test_curate_write_path_flows_through_the_same_boundary(curate_backend, ns):
    # curate ops -> apply_curation -> _write_curation_op -> _append: the boundary
    # must reject the same defect shape there too.
    curate_backend.curator.result = {"operations": [
        {"op": "ADD", "key": "lesson/x", "kind": "note", "value": {"v": 1},
         "meta": {"repo_sha": "zz-not-hex"}},
    ]}
    with pytest.raises(AppError) as err:
        await curate_backend.coord_curate(ns, await _session(curate_backend, ns))
    assert err.value.code == "invalid_sha"


async def _session(backend, ns) -> str:
    s = await backend.session_create(ns)
    return s["session_id"]


# ------------------------------------- the V2 probe scenario, as a fixture (2x2)
async def test_probe_prefix_claim_reconciles_identically_across_consumers(reconcile_backend, ns):
    """The live 2x2 probe: 7-char prefix of the live head -> reconcile said
    `current`, health said `stale`. After the fix BOTH must read it clean."""
    b = reconcile_backend
    b.resolver.heads[(REPO, "main")] = FULL
    # Full-sha control entry (the namespace's most-recently-observed sha)...
    await b.memory_save(ns, "probe/sha-full", {"probe": "full"}, kind="claim",
                        meta={"repo": REPO, "branch": "main", "repo_sha": FULL})
    # ...and the probe: the SAME commit recorded as a 7-char prefix. No commit
    # resolution configured, so the abbreviation is stored as-is (the defect
    # shape reaches the store exactly as in the live probe).
    await b.memory_save(ns, "probe/sha-prefix", {"probe": "prefix"}, kind="claim",
                        meta={"repo": REPO, "branch": "main", "repo_sha": PREFIX})

    verdicts = {v["key"]: v["state"] for v in (await b.coord_reconcile(ns))["verdicts"]}
    assert verdicts["probe/sha-prefix"] == "current"
    assert verdicts["probe/sha-full"] == "current"

    health = await b.coord_health(ns)
    stale_keys = {s["key"] for s in health["stale"]}
    assert "probe/sha-prefix" not in stale_keys          # the probe's failing leg
    assert "probe/sha-full" not in stale_keys
    # Reconcile and health now agree on the SAME claim — the acceptance gate.


async def test_head_move_still_reads_stale_everywhere(reconcile_backend, ns):
    # Guard against over-fixing: a genuinely-behind pin must STILL read stale
    # from both consumers once the namespace has observed a newer sha.
    b = reconcile_backend
    new_head = "f" * 40
    b.resolver.heads[(REPO, "main")] = new_head
    await b.memory_save(ns, "old-pin", {"x": 1}, kind="claim",
                        meta={"repo": REPO, "branch": "main", "repo_sha": FULL})
    await b.memory_save(ns, "new-pin", {"x": 2}, kind="claim",
                        meta={"repo": REPO, "branch": "main", "repo_sha": new_head})
    verdicts = {v["key"]: v["state"] for v in (await b.coord_reconcile(ns))["verdicts"]}
    assert verdicts["old-pin"] == "stale" and verdicts["new-pin"] == "current"
    stale_keys = {s["key"] for s in (await b.coord_health(ns))["stale"]}
    assert "old-pin" in stale_keys and "new-pin" not in stale_keys


# ------------------------------------------- rev-1 defect shape (S6 regression)
async def test_rev1_defect_shape_projected_prefix_column_not_flagged(backend, ns):
    """claim/p2-build-completion rev 1: curator wrote a 7-char abbreviation into
    the projected repo_sha column while a sibling entry carried the full sha of
    the SAME milestone commit. health must not call the abbreviation stale."""
    milestone_full = "398b38aef9d0ce1636993c2be6e3b617a3b3e8b2"
    await backend.memory_save(ns, "claim/p2-build-completion", {"milestone": "p2 build"},
                              kind="claim",
                              meta={"repo": REPO, "branch": "main", "repo_sha": "398b38a"})
    await backend.memory_save(ns, "build/context", {"note": "same milestone, full sha"},
                              meta={"repo": REPO, "branch": "main", "repo_sha": milestone_full})
    health = await backend.coord_health(ns)
    assert health["latest_repo_sha"] == milestone_full
    assert {s["key"] for s in health["stale"]} == set()
