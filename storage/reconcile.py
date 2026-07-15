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
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

# The equivalence rule now lives in ONE shared module (v3 P0/1 item 1) so
# reconcile, coord_health, the write boundary, and the R5 advisory can never
# diverge again. Re-exported here behavior-unchanged for existing importers.
from storage.sha_equiv import MIN_ABBREV_LEN as _MIN_SHA_LEN  # noqa: F401
from storage.sha_equiv import AmbiguousShaRef, sha_match  # noqa: F401

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
    async def commit_sha(self, repo: str, ref: str) -> str | None: ...


class DisabledResolver:
    """No-op resolver used when no GitHub token is set. Claims stay unverifiable."""

    enabled = False

    async def merged_state(self, repo: str, pr: int) -> dict | None:
        return None

    async def branch_head(self, repo: str, branch: str) -> str | None:
        return None

    async def commit_sha(self, repo: str, ref: str) -> str | None:
        return None


class GitHubResolver:
    """Read-only GitHub REST resolver. Every call is best-effort: any failure
    (network, auth, 404, rate-limit) returns None, which the caller maps to
    ``unverifiable`` rather than a wrong answer.

    The token is supplied by an async provider resolved per request, so a
    refreshing OAuth token (Replit connector) and a static PAT are handled the
    same way. A plain string is accepted too and wrapped as a constant provider."""

    enabled = True

    def __init__(
        self,
        token_provider: "Callable[[], Awaitable[str | None]] | str",
        api_url: str = "https://api.github.com",
        *,
        timeout: float = 15.0,
    ) -> None:
        if isinstance(token_provider, str):
            _token = token_provider

            async def _const() -> str | None:
                return _token

            self._token_provider: "Callable[[], Awaitable[str | None]]" = _const
        else:
            self._token_provider = token_provider
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    async def _get(self, path: str) -> dict | None:
        import httpx

        try:
            token = await self._token_provider()
            if not token:
                return None
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.api_url}{path}", headers=headers)
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

    async def commit_sha(self, repo: str, ref: str) -> str | None:
        """Resolve a (possibly abbreviated) commit ref to its full 40-char sha.

        Best-effort like every other call — any failure returns None — with ONE
        exception: GitHub answers an ambiguous abbreviation with 422, and that is
        a real defect in the recorded ref, so it raises ``AmbiguousShaRef`` for
        the write boundary to reject instead of silently storing."""
        import httpx

        try:
            token = await self._token_provider()
            if not token:
                return None
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.api_url}/repos/{repo}/commits/{ref}", headers=headers)
                if resp.status_code == 422:
                    raise AmbiguousShaRef(ref)
                resp.raise_for_status()
                return resp.json().get("sha")
        except AmbiguousShaRef:
            raise
        except Exception:  # noqa: BLE001 - best-effort: any failure -> unresolved
            return None


def build_resolver(settings: Any) -> Resolver:
    """Pick a resolver from config, decoupled from ``config`` (only config.py
    reads the environment):

    1. an explicit ``github_token`` (PAT) wins — durable and simplest;
    2. else the connected GitHub account via the Replit connector proxy;
    3. else disabled (claims reconcile to ``unverifiable``)."""
    api_url = getattr(settings, "github_api_url", "https://api.github.com")
    token = getattr(settings, "github_token", None)
    if token:
        return GitHubResolver(token, api_url)

    from storage.github_token import build_connector_token_provider

    provider = build_connector_token_provider(settings)
    if provider is not None:
        return GitHubResolver(provider, api_url)
    return DisabledResolver()


async def reconcile_claim(entry: dict, resolver: Resolver) -> dict:
    """Derive a freshness verdict for one claim from its provenance + the resolver.

    Mechanical, prose-free: it looks only at meta.repo + meta.pr / meta.branch,
    the entry's repo_sha, and its temporal_mode. Returns
    ``{key, subject, state, temporal_mode, ...evidence}``.

    Temporal forks (v3 item 5): ``historical_snapshot`` asserts about a specific
    commit as of a moment — it verifies the pinned sha EXISTS upstream and NEVER
    compares it to the live head, so a verified snapshot is terminally non-stale.
    ``timeless`` has no external mutable subject at all. ``interval``
    reconciliation is not mechanized in this phase (verdicts stay unverifiable,
    never guessed). Claims without a recorded mode get head-comparison semantics
    as before, and the verdict says so ADVISORILY:
    ``temporal_mode: "head_tracking", temporal_mode_origin: "inferred"``."""
    meta = entry.get("meta") or {}
    repo = meta.get("repo")
    pr = meta.get("pr")
    branch = meta.get("branch") or entry.get("branch")
    repo_sha = entry.get("repo_sha") or meta.get("repo_sha")
    mode = entry.get("temporal_mode") or meta.get("temporal_mode")
    mode_origin = "recorded" if mode else "inferred"
    base = {"key": entry.get("key"), "repo": repo}

    if not resolver.enabled:
        return {**base, "state": UNVERIFIABLE, "reason": "resolver disabled (no GitHub access)"}

    if mode == "timeless":
        return {**base, "state": CURRENT, "temporal_mode": mode,
                "temporal_mode_origin": mode_origin, "terminal": True,
                "reason": "timeless: no external mutable subject to compare"}

    if mode == "interval":
        return {**base, "state": UNVERIFIABLE, "temporal_mode": mode,
                "temporal_mode_origin": mode_origin,
                "reason": "interval reconciliation is not mechanized in this phase"}

    if mode == "historical_snapshot":
        if not (repo and repo_sha):
            return {**base, "state": UNVERIFIABLE, "temporal_mode": mode,
                    "temporal_mode_origin": mode_origin,
                    "reason": "historical_snapshot needs meta.repo + a pinned repo_sha"}
        try:
            full = await resolver.commit_sha(repo, repo_sha)
        except AmbiguousShaRef:
            full = None
        if full and sha_match(repo_sha, full):
            # Terminal non-stale: the snapshot's subject is the commit itself,
            # never the moving head.
            return {**base, "subject": f"commit:{repo_sha}", "state": CURRENT,
                    "temporal_mode": mode, "temporal_mode_origin": mode_origin,
                    "terminal": True, "resolved": {"commit_sha": full},
                    "reason": "pinned sha exists upstream; snapshots never compare to head"}
        return {**base, "subject": f"commit:{repo_sha}", "state": UNVERIFIABLE,
                "temporal_mode": mode, "temporal_mode_origin": mode_origin,
                "reason": "pinned sha not observable upstream"}

    # head_tracking (recorded or inferred): the pre-item-5 comparison semantics.
    head_mode = {"temporal_mode": "head_tracking", "temporal_mode_origin": mode_origin}

    if repo and pr is not None:
        resolved = await resolver.merged_state(repo, int(pr))
        if resolved is None:
            return {**base, **head_mode, "subject": f"pr:{pr}", "state": UNVERIFIABLE,
                    "reason": "could not resolve PR"}
        recorded = meta.get("merge_sha")
        if resolved["merged"]:
            # Merged upstream: current only if the claim already recorded that merge.
            state = CURRENT if sha_match(recorded, resolved.get("merge_sha")) else STALE
        else:
            # Not merged upstream: current only if the claim didn't assert a merge.
            state = STALE if recorded else CURRENT
        return {**base, **head_mode, "subject": f"pr:{pr}", "state": state,
                "resolved": resolved, "recorded_merge_sha": recorded}

    if repo and branch:
        head = await resolver.branch_head(repo, branch)
        if head is None:
            return {**base, **head_mode, "subject": f"branch:{branch}", "state": UNVERIFIABLE,
                    "reason": "could not resolve branch head"}
        state = CURRENT if sha_match(repo_sha, head) else STALE
        return {**base, **head_mode, "subject": f"branch:{branch}", "state": state,
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
