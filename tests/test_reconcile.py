"""Phase 3 — backend reconciler: provenance-derived verdicts, append-only writes
to coord/_reconcile/*, the disabled (no-GitHub) path, and webhook signature auth."""
from __future__ import annotations

from storage.reconcile import CURRENT, STALE, UNVERIFIABLE, reconcile_claim, sha_match, verify_signature

REPO = "acme/widget"


async def _verdict(backend, ns, key):
    rec = await backend.memory_get(ns, f"coord/_reconcile/{key}")
    return rec  # value is wrapped on read; state is also in tags


async def test_pr_merged_and_recorded_is_current(reconcile_backend, ns):
    reconcile_backend.resolver.pulls[(REPO, 11)] = {"merged": True, "merge_sha": "61a0f55"}
    await reconcile_backend.memory_save(
        ns, "phase1", {"done": True}, kind="claim",
        meta={"repo": REPO, "pr": 11, "merge_sha": "61a0f55"},
    )
    out = await reconcile_backend.coord_reconcile(ns)
    assert out["resolver_enabled"] is True
    v = out["verdicts"][0]
    assert v["state"] == CURRENT and v["subject"] == "pr:11"
    # Verdict persisted as an append-only record, tagged with the state.
    rec = await reconcile_backend.memory_get(ns, "coord/_reconcile/phase1")
    assert "reconcile" in rec["tags"] and CURRENT in rec["tags"]


async def test_pr_merged_but_claim_unaware_is_stale(reconcile_backend, ns):
    # PR is merged upstream, but the claim recorded no merge_sha → it predates the merge.
    reconcile_backend.resolver.pulls[(REPO, 11)] = {"merged": True, "merge_sha": "61a0f55"}
    await reconcile_backend.memory_save(
        ns, "phase1", {"merged": False}, kind="claim", meta={"repo": REPO, "pr": 11},
    )
    out = await reconcile_backend.coord_reconcile(ns)
    assert out["verdicts"][0]["state"] == STALE


async def test_pr_not_merged_and_claim_silent_is_current(reconcile_backend, ns):
    reconcile_backend.resolver.pulls[(REPO, 12)] = {"merged": False, "merge_sha": None}
    await reconcile_backend.memory_save(
        ns, "open-pr", {"status": "in review"}, kind="claim", meta={"repo": REPO, "pr": 12},
    )
    out = await reconcile_backend.coord_reconcile(ns)
    assert out["verdicts"][0]["state"] == CURRENT


async def test_branch_head_match_vs_behind(reconcile_backend, ns):
    reconcile_backend.resolver.heads[(REPO, "main")] = "3ab6d4a"
    await reconcile_backend.memory_save(
        ns, "fresh", {"x": 1}, kind="claim", meta={"repo": REPO, "branch": "main"},
    )  # repo_sha matches head
    # set repo_sha via meta
    await reconcile_backend.memory_save(
        ns, "fresh", {"x": 1}, kind="claim",
        meta={"repo": REPO, "branch": "main", "repo_sha": "3ab6d4a"},
    )
    await reconcile_backend.memory_save(
        ns, "behind", {"x": 1}, kind="claim",
        meta={"repo": REPO, "branch": "main", "repo_sha": "e87f91c9"},
    )
    out = await reconcile_backend.coord_reconcile(ns)
    states = {v["key"]: v["state"] for v in out["verdicts"]}
    assert states["fresh"] == CURRENT
    assert states["behind"] == STALE


async def test_unresolvable_subject_is_unverifiable(reconcile_backend, ns):
    await reconcile_backend.memory_save(ns, "vague", {"x": 1}, kind="claim", meta={"repo": REPO})
    out = await reconcile_backend.coord_reconcile(ns)
    assert out["verdicts"][0]["state"] == UNVERIFIABLE


async def test_disabled_resolver_yields_unverifiable_never_current(backend, ns):
    # The plain `backend` fixture has no resolver (DisabledResolver).
    backend.resolver  # noqa: B018 - documents the disabled default
    await backend.memory_save(ns, "phase1", {"done": True}, kind="claim",
                              meta={"repo": REPO, "pr": 11, "merge_sha": "61a0f55"})
    out = await backend.coord_reconcile(ns)
    assert out["resolver_enabled"] is False
    assert out["verdicts"][0]["state"] == UNVERIFIABLE


async def test_reconcile_does_not_touch_the_claim(reconcile_backend, ns):
    reconcile_backend.resolver.pulls[(REPO, 11)] = {"merged": True, "merge_sha": "61a0f55"}
    await reconcile_backend.memory_save(ns, "phase1", {"done": True}, kind="claim",
                                        meta={"repo": REPO, "pr": 11, "merge_sha": "61a0f55"})
    await reconcile_backend.coord_reconcile(ns)
    claim = await reconcile_backend.memory_get(ns, "phase1")
    assert claim["revision"] == 1  # untouched; only coord/_reconcile/phase1 was written


async def test_webhook_repo_scoped_reconcile_across_namespaces(reconcile_backend, ns):
    import uuid
    # coord_reconcile_repo is store-wide; use a repo unique to this test so the
    # scan isn't polluted by sibling tests' acme/widget claims in the shared DB.
    repo = f"acme/widget-{uuid.uuid4().hex[:8]}"
    ns2 = f"proj-test-{uuid.uuid4().hex[:12]}"
    reconcile_backend.resolver.pulls[(repo, 11)] = {"merged": True, "merge_sha": "61a0f55"}
    for n in (ns, ns2):
        await reconcile_backend.memory_save(n, "phase1", {"done": True}, kind="claim",
                                            meta={"repo": repo, "pr": 11, "merge_sha": "61a0f55"})
    out = await reconcile_backend.coord_reconcile_repo(repo, pr=11)
    assert out["reconciled"] == 2
    assert all(v["state"] == CURRENT for v in out["verdicts"])


def test_sha_match_tolerates_abbreviation():
    full = "6e942ca0c84733da5772f476a6ca98c81ea4d02b"
    assert sha_match("6e942ca", full) is True          # short recorded vs full from API
    assert sha_match(full, "6e942ca") is True           # symmetric
    assert sha_match("6E942CA", full) is True           # case-insensitive
    assert sha_match(full, full) is True                # identical
    assert sha_match("deadbee", full) is False          # different commit
    assert sha_match("6e9", full) is False              # too short to trust (<7)
    assert sha_match(None, full) is False
    assert sha_match("6e942ca", None) is False


async def test_pr_merged_with_SHORT_recorded_sha_is_current(reconcile_backend, ns):
    # Live-found regression: claim records a 7-char sha, GitHub returns the full 40.
    full = "6e942ca0c84733da5772f476a6ca98c81ea4d02b"
    reconcile_backend.resolver.pulls[(REPO, 7)] = {"merged": True, "merge_sha": full}
    await reconcile_backend.memory_save(
        ns, "pr7", {"done": True}, kind="claim",
        meta={"repo": REPO, "pr": 7, "merge_sha": "6e942ca"},
    )
    out = await reconcile_backend.coord_reconcile(ns)
    assert out["verdicts"][0]["state"] == CURRENT


def test_verify_signature_roundtrip_and_rejects_tampering():
    import hashlib
    import hmac as _hmac

    secret, body = "s3cr3t", b'{"action":"closed"}'
    good = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, good) is True
    assert verify_signature(secret, body, "sha256=deadbeef") is False
    assert verify_signature(secret, b'{"action":"opened"}', good) is False  # body changed
    assert verify_signature("", body, good) is False  # no secret configured
    assert verify_signature(secret, body, None) is False  # no header


async def test_reconcile_claim_unit_disabled():
    class _Off:
        enabled = False
        async def merged_state(self, *a): return None
        async def branch_head(self, *a): return None

    v = await reconcile_claim({"key": "k", "meta": {"repo": REPO, "pr": 11}}, _Off())
    assert v["state"] == UNVERIFIABLE


async def test_real_github_resolver_end_to_end(backend, ns, monkeypatch):
    """End-to-end through the REAL GitHubResolver (its HTTP fetch + verdict
    derivation) with the GitHub API mocked — covers the gap between the offline
    token tests (test_github_token) and the FakeResolver verdict tests above.

    A PR claim that records the same merge_sha the API reports must read CURRENT;
    a branch claim whose repo_sha is behind the live head must read STALE."""
    import uuid

    from storage.reconcile import GitHubResolver

    repo = f"acme/widget-{uuid.uuid4().hex[:8]}"
    head_sha, merge_sha = "3ab6d4a", "61a0f55"

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            if "/pulls/" in url:
                return FakeResp({"merged": True, "merge_commit_sha": merge_sha})
            if "/branches/" in url:
                return FakeResp({"commit": {"sha": head_sha}})
            raise AssertionError(f"unexpected GitHub path: {url}")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def _token():  # stand-in for the connector/PAT token provider
        return "ghp_fake"

    backend.resolver = GitHubResolver(_token)

    await backend.memory_save(ns, "merged-pr", {"done": True}, kind="claim",
                              meta={"repo": repo, "pr": 7, "merge_sha": merge_sha})
    await backend.memory_save(ns, "behind-branch", {"x": 1}, kind="claim",
                              meta={"repo": repo, "branch": "main", "repo_sha": "deadbee"})

    out = await backend.coord_reconcile(ns)
    assert out["resolver_enabled"] is True
    states = {v["key"]: v["state"] for v in out["verdicts"]}
    assert states["merged-pr"] == CURRENT
    assert states["behind-branch"] == STALE
