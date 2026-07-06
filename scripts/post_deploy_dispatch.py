"""Fire a GitHub ``repository_dispatch`` the moment a deploy goes live.

The in-process smoke checks (tests/test_smoke_mcp.py) BLOCK a broken build in
CI, and scripts/smoke_mcp.py FLAGS an unhealthy live deployment — but the live
run was only manual or on a 6-hour schedule (.github/workflows/smoke.yml). A
regression that only manifests behind the Replit edge (like the fastmcp 3.4.3
421) could therefore sit unflagged for up to 6 hours after a deploy.

This script closes that window. It is wired into ``.replit`` ``[deployment].run``
and launched in the background alongside uvicorn: it waits for the local server's
``/healthz`` to report ready, then POSTs a ``repository_dispatch`` event to
GitHub, which triggers the smoke workflow against the LIVE URL. The workflow's
job failure / notification is the loud, visible signal — this script itself is
best-effort and NEVER blocks or fails the deploy (it always exits 0; the
scheduled smoke run stays as the backstop if the dispatch can't be sent).

Resolution (no secrets hardcoded):
  * token   — $SMOKE_DISPATCH_TOKEN or $GITHUB_TOKEN (an explicit PAT with
              ``repo`` scope), else the connected GitHub account via the Replit
              connector proxy (OAuth, re-fetched live — the same source the
              reconciler uses in storage/github_token.py).
  * repo    — $SMOKE_DISPATCH_REPO or $GITHUB_REPOSITORY (``owner/name``), else
              parsed from the ``origin`` git remote.
  * base_url— $SMOKE_BASE_URL, forwarded in client_payload so the workflow can
              probe the exact deployed URL; when unset the workflow falls back to
              its own SMOKE_BASE_URL repo secret.

The event type is ``post-deploy-smoke``; .github/workflows/smoke.yml listens for
it under its ``repository_dispatch`` trigger.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time

EVENT_TYPE = "post-deploy-smoke"
GITHUB_API_URL = "https://api.github.com"

# How long to wait for the freshly started local server before giving up and
# dispatching anyway (a slow boot shouldn't silently skip the probe forever).
HEALTH_TIMEOUT_S = 120.0
HEALTH_INTERVAL_S = 2.0


def resolve_repo(env: dict[str, str]) -> str | None:
    """``owner/name`` for the dispatch target, or ``None`` when undiscoverable."""
    for key in ("SMOKE_DISPATCH_REPO", "GITHUB_REPOSITORY"):
        val = (env.get(key) or "").strip()
        if val:
            slug = _parse_repo_slug(val)
            if slug:
                return slug
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - git missing / not a checkout -> no repo
        return None
    return _parse_repo_slug(url) if url else None


def _parse_repo_slug(value: str) -> str | None:
    """Extract ``owner/name`` from a bare slug or any github remote URL form
    (``git@github.com:owner/name.git``, ``https://[token@]github.com/owner/name.git``)."""
    value = value.strip()
    if not value:
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", value)
    if not m:
        # Already a bare "owner/name"?
        m = re.fullmatch(r"([^/\s]+)/([^/\s]+?)(?:\.git)?", value)
        if not m:
            return None
    owner, name = m.group(1), m.group(2)
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def resolve_explicit_token(env: dict[str, str]) -> str | None:
    """An explicit PAT from the environment, or ``None``."""
    for key in ("SMOKE_DISPATCH_TOKEN", "GITHUB_TOKEN"):
        val = (env.get(key) or "").strip()
        if val:
            return val
    return None


async def _connector_token(env: dict[str, str]) -> str | None:
    """A live GitHub token from the Replit connector proxy, or ``None``. Reuses
    the reconciler's provider so OAuth refresh is handled in exactly one place."""
    from types import SimpleNamespace

    from storage.github_token import build_connector_token_provider

    settings = SimpleNamespace(
        replit_connectors_hostname=env.get("REPLIT_CONNECTORS_HOSTNAME"),
        repl_identity=env.get("REPL_IDENTITY"),
        web_repl_renewal=env.get("WEB_REPL_RENEWAL"),
    )
    provider = build_connector_token_provider(settings)
    if provider is None:
        return None
    try:
        return await provider()
    except Exception:  # noqa: BLE001 - best-effort: proxy failure -> no token
        return None


def build_dispatch_request(
    repo: str, base_url: str | None, api_url: str = GITHUB_API_URL
) -> tuple[str, dict]:
    """The (url, json-body) for the repository_dispatch POST. ``base_url`` is
    forwarded to the workflow only when set, so an unset value cleanly defers to
    the workflow's own SMOKE_BASE_URL secret."""
    payload: dict[str, object] = {"source": "post-deploy"}
    if base_url:
        payload["base_url"] = base_url.rstrip("/")
    body = {"event_type": EVENT_TYPE, "client_payload": payload}
    return f"{api_url.rstrip('/')}/repos/{repo}/dispatches", body


def _local_health_url(env: dict[str, str]) -> str:
    port = (env.get("PORT") or "8000").strip() or "8000"
    return f"http://127.0.0.1:{port}/healthz"


def wait_for_health(
    client, url: str, timeout: float = HEALTH_TIMEOUT_S, interval: float = HEALTH_INTERVAL_S
) -> bool:
    """Poll ``url`` until it returns 200 with ``{"db": "ok"}`` or ``timeout``
    elapses. Returns whether the server came up ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get(url)
            if resp.status_code == 200 and resp.json().get("db") == "ok":
                return True
        except Exception:  # noqa: BLE001 - not up yet; keep polling
            pass
        time.sleep(interval)
    return False


def main() -> int:
    import httpx

    env = os.environ

    repo = resolve_repo(env)
    if not repo:
        print(
            "post-deploy-dispatch: no repo (set SMOKE_DISPATCH_REPO or GITHUB_REPOSITORY) "
            "— skipping; the scheduled smoke run remains the backstop.",
            file=sys.stderr,
        )
        return 0

    token = resolve_explicit_token(env)
    if not token:
        token = asyncio.run(_connector_token(env))
    if not token:
        print(
            "post-deploy-dispatch: no GitHub token (set SMOKE_DISPATCH_TOKEN/GITHUB_TOKEN "
            "or connect GitHub) — skipping; the scheduled smoke run remains the backstop.",
            file=sys.stderr,
        )
        return 0

    health_url = _local_health_url(env)
    with httpx.Client(timeout=10) as client:
        if not wait_for_health(client, health_url):
            print(
                f"post-deploy-dispatch: {health_url} not ready in {HEALTH_TIMEOUT_S:.0f}s — "
                "dispatching anyway so the failure is flagged.",
                file=sys.stderr,
            )

    base_url = (env.get("SMOKE_BASE_URL") or "").strip() or None
    url, body = build_dispatch_request(repo, base_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001 - network failure is non-fatal
        print(f"post-deploy-dispatch: dispatch failed (unreachable): {exc}", file=sys.stderr)
        return 0

    if resp.status_code == 204:
        print(f"post-deploy-dispatch: triggered smoke for {repo} (event={EVENT_TYPE}).")
    else:
        print(
            f"post-deploy-dispatch: dispatch to {repo} returned {resp.status_code} "
            f"(expected 204): {resp.text[:200]!r}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
