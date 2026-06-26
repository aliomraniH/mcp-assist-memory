"""Backend reconciler — resolve a claim's truth against GitHub (Phase 3).

This is to coordination what ``embeddings`` is to search: an **optional,
best-effort, injected** dependency. With no GitHub token the factory returns a
``DisabledResolver`` (``enabled = False``) and every claim reconciles to
``unverifiable`` — the server runs identically, never blocked, never guessing.

Design contract (see docs/coord-spine.md):
* The reconciler never rewrites a user's entry. ``coord_reconcile`` writes an
  append-only ``coord/_reconcile/<key>`` record holding the verdict.
* A claim's freshness is derived from *provenance* (meta.repo + meta.pr /
  meta.branch + repo_sha), not from parsing its prose — so the rule is mechanical.
* When the resolver is blind (disabled, or the call fails) the verdict is
  ``unverifiable``. It is NEVER silently treated as ``current``.

Vector/HTTP note: ``httpx`` is imported lazily so the module never hard-requires
it when reconciliation is disabled.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Protocol, runtime_checkable

# Verdict states.
CURRENT = "current"
STALE = "stale"
UNVERIFIABLE = "unverifiable"


@runtime_checkable
class Resolver(Protocol):
    """Resolves external mutable truth. ``enabled`` lets callers short-circuit
    (no network) when reconciliation is off."""

    enabled: bool

    async def merged_state(self, repo: str, pr: int) -> dict | None: ...
    async def branch_head(self, repo: str, branch: str) -> str | None: ...


class DisabledResolver:
    """No-op resolver used when no GitHub token is set. Claims stay unverifiable."""

    enabled = False

    async def merged_state(self, repo: str, pr: int) -> dict | None:
        return None

    async def branch_head(self, repo: str, branch: str) -> str | None:
        return None


class GitHubResolver:
    """Read-only GitHub REST resolver. Every call is best-effort: any failure
    (network, auth, 404, rate-limit) returns None, which the caller maps to
    ``unverifiable`` rather than a wrong answer."""

    enabled = True

    def __init__(self, token: str, api_url: str = "https://api.github.com", *, timeout: float = 15.0) -> None:
        self._token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json"}

    async def _get(self, path: str) -> dict | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.api_url}{path}", headers=self._headers())
                resp.raise_for_status()
                return resp.json()
        except Exception:  # noqa: BLE001 - best-effort: any failure -> unverifiable
            return None

    async def merged_state(self, repo: str, pr: int) -> dict | None:
        data = await self._get(f"/repos/{repo}/pulls/{pr}")
        if data is None:
            return None
        return {"merged": bool(data.get("merged")), "merge_sha": data.get("merge_commit_sha")}

    async def branch_head(self, repo: str, branch: str) -> str | None:
        data = await self._get(f"/repos/{repo}/branches/{branch}")
        if data is None:
            return None
        return (data.get("commit") or {}).get("sha")


def build_resolver(settings: Any) -> Resolver:
    """Pick a resolver from config: GitHub when a token is present, else disabled.
    Decoupled from ``config`` (only config.py reads the environment)."""
    token = getattr(settings, "github_token", None)
    if token:
        return GitHubResolver(token, getattr(settings, "github_api_url", "https://api.github.com"))
    return DisabledResolver()


async def reconcile_claim(entry: dict, resolver: Resolver) -> dict:
    """Derive a freshness verdict for one claim from its provenance + the resolver.

    Mechanical, prose-free: it looks only at meta.repo + meta.pr / meta.branch and
    the entry's repo_sha. Returns ``{key, subject, state, ...evidence}``."""
    meta = entry.get("meta") or {}
    repo = meta.get("repo")
    pr = meta.get("pr")
    branch = meta.get("branch") or entry.get("branch")
    repo_sha = entry.get("repo_sha")
    base = {"key": entry.get("key"), "repo": repo}

    if not resolver.enabled:
        return {**base, "state": UNVERIFIABLE, "reason": "resolver disabled (no GitHub access)"}

    if repo and pr is not None:
        resolved = await resolver.merged_state(repo, int(pr))
        if resolved is None:
            return {**base, "subject": f"pr:{pr}", "state": UNVERIFIABLE, "reason": "could not resolve PR"}
        recorded = meta.get("merge_sha")
        if resolved["merged"]:
            # Merged upstream: current only if the claim already recorded that merge.
            state = CURRENT if recorded and recorded == resolved.get("merge_sha") else STALE
        else:
            # Not merged upstream: current only if the claim didn't assert a merge.
            state = STALE if recorded else CURRENT
        return {**base, "subject": f"pr:{pr}", "state": state,
                "resolved": resolved, "recorded_merge_sha": recorded}

    if repo and branch:
        head = await resolver.branch_head(repo, branch)
        if head is None:
            return {**base, "subject": f"branch:{branch}", "state": UNVERIFIABLE,
                    "reason": "could not resolve branch head"}
        state = CURRENT if repo_sha and repo_sha == head else STALE
        return {**base, "subject": f"branch:{branch}", "state": state,
                "resolved": {"head": head}, "claim_repo_sha": repo_sha}

    return {**base, "state": UNVERIFIABLE,
            "reason": "claim has no resolvable subject (need meta.repo + meta.pr or meta.branch)"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of a GitHub ``X-Hub-Signature-256`` header
    (``sha256=<hexdigest>``) over the raw request body."""
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)
