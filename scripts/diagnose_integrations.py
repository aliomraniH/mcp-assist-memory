"""Diagnose the two best-effort external integrations: GitHub + Anthropic.

Motivating incident (2026-07-16, v3 web-session capability test): the deployed
server ran a whole test window with `resolver_enabled:true` while every
branch-head resolution failed — and nothing could say WHY, because both
integrations are deliberately best-effort and collapse every failure
(missing token, expired token, connector-proxy error, rate limit, blocked
egress) into the same silent ``None`` → ``unverifiable``. That silence is
correct for the write/reconcile path (never block, never guess) but useless
for an operator. This script is the missing debugger: it walks each hop
separately and names the first one that breaks.

Hops, in dependency order:

  config            which token path is active (pat | connector | none) and
                    whether an Anthropic key is present — presence only,
                    never values
  github_egress     can this box reach api.github.com at all (any HTTP
                    status = yes; only a transport error = no)
  connector_token   the Replit connector-proxy fetch, with the real reason
                    on failure (not_configured | http_<code> |
                    network_unreachable | no_github_connection |
                    no_access_token | ok)
  github_api        an authenticated GET /rate_limit (distinguishes
                    auth_failed from rate_limited from ok, and reports the
                    remaining core quota) plus the actual branch-head call
                    the reconciler makes
  anthropic_api     a minimal real messages.create round-trip (classified
                    by exception type: AuthenticationError, NotFoundError
                    for a bad model id, RateLimitError, ...)

Run on the deployed box (Replit shell) or locally:

    python scripts/diagnose_integrations.py [repo] [branch]

Repo/branch default to $DIAG_REPO / $DIAG_BRANCH, else
aliomraniH/mcp-assist-memory main. Exits 0 when every ACTIVE hop passes
(hops whose path isn't configured report ``skip`` and don't fail the run),
1 otherwise. No secret value is ever printed.

The check functions are imported by tests/test_diagnose_integrations.py,
which runs the same classifications against stub transports — so the
diagnosis logic is pinned by CI even though the script itself needs live
credentials to say anything interesting.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

GITHUB_API = "https://api.github.com"

OK = "ok"
SKIP = "skip"
FAIL = "fail"


def _result(check: str, status: str, reason: str, **extra: Any) -> dict:
    return {"check": check, "status": status, "reason": reason, **extra}


def diagnose_config(env: dict) -> dict:
    """Which credential paths are active — presence only, never values."""
    if env.get("GITHUB_TOKEN"):
        github_path = "pat"
    elif env.get("REPLIT_CONNECTORS_HOSTNAME") and (
        env.get("REPL_IDENTITY") or env.get("WEB_REPL_RENEWAL")
    ):
        github_path = "connector"
    else:
        github_path = "none"
    return _result(
        "config",
        OK if github_path != "none" or env.get("ANTHROPIC_API_KEY") else FAIL,
        f"github token path: {github_path}; anthropic key: "
        f"{'present' if env.get('ANTHROPIC_API_KEY') else 'absent'}",
        github_path=github_path,
        anthropic_configured=bool(env.get("ANTHROPIC_API_KEY")),
        connector_identity="repl" if env.get("REPL_IDENTITY")
        else "depl" if env.get("WEB_REPL_RENEWAL") else None,
    )


async def check_github_egress(client: Any, api_url: str = GITHUB_API) -> dict:
    """Any HTTP response at all proves egress + DNS; only a transport error fails.
    (An unauthenticated /rate_limit is a valid 200 on api.github.com.)"""
    try:
        resp = await client.get(f"{api_url}/rate_limit")
    except Exception as exc:  # noqa: BLE001 - the exception IS the diagnosis
        return _result("github_egress", FAIL, f"network_unreachable: {type(exc).__name__}")
    return _result("github_egress", OK, f"reachable (HTTP {resp.status_code})")


async def check_connector_token(env: dict, client: Any) -> dict:
    """The Replit connector-proxy fetch, mirroring storage/github_token.py but
    with the failure reason surfaced instead of swallowed."""
    hostname = env.get("REPLIT_CONNECTORS_HOSTNAME")
    identity, renewal = env.get("REPL_IDENTITY"), env.get("WEB_REPL_RENEWAL")
    x_replit_token = f"repl {identity}" if identity else f"depl {renewal}" if renewal else None
    if not hostname or not x_replit_token:
        return _result("connector_token", SKIP, "not_configured (no connector env vars)")
    try:
        resp = await client.get(
            f"https://{hostname}/api/v2/connection",
            params={"include_secrets": "true", "connector_names": "github"},
            headers={"Accept": "application/json", "X_REPLIT_TOKEN": x_replit_token},
        )
    except Exception as exc:  # noqa: BLE001
        return _result("connector_token", FAIL, f"network_unreachable: {type(exc).__name__}")
    if resp.status_code != 200:
        return _result("connector_token", FAIL, f"http_{resp.status_code} from connector proxy")
    items = resp.json().get("items", [])
    if not items:
        return _result("connector_token", FAIL,
                       "no_github_connection (proxy answered; no connected GitHub account)")
    settings = items[0].get("settings", {}) or {}
    creds = (settings.get("oauth", {}) or {}).get("credentials", {}) or {}
    token = settings.get("access_token") or creds.get("access_token")
    if not token:
        return _result("connector_token", FAIL,
                       "no_access_token (connection exists; token field empty)")
    return _result("connector_token", OK, "token minted",
                   expires_at=creds.get("expires_at"), token=token)


async def check_github_api(token: str, client: Any, repo: str, branch: str,
                           api_url: str = GITHUB_API) -> dict:
    """The authenticated calls the reconciler actually makes, classified."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        rl = await client.get(f"{api_url}/rate_limit", headers=headers)
    except Exception as exc:  # noqa: BLE001
        return _result("github_api", FAIL, f"network_unreachable: {type(exc).__name__}")
    if rl.status_code == 401:
        return _result("github_api", FAIL, "auth_failed (401 — token invalid or expired)")
    core = (rl.json().get("resources", {}) or {}).get("core", {}) if rl.status_code == 200 else {}
    remaining = core.get("remaining")
    if remaining == 0:
        return _result("github_api", FAIL,
                       f"rate_limited (0 remaining, resets at {core.get('reset')})")
    try:
        head = await client.get(f"{api_url}/repos/{repo}/branches/{branch}", headers=headers)
    except Exception as exc:  # noqa: BLE001
        return _result("github_api", FAIL, f"network_unreachable: {type(exc).__name__}")
    if head.status_code == 404:
        return _result("github_api", FAIL,
                       f"repo_not_visible (404 on {repo} — private without access, or gone)",
                       core_remaining=remaining)
    if head.status_code == 403:
        return _result("github_api", FAIL, "forbidden_or_secondary_rate_limit (403)",
                       core_remaining=remaining)
    if head.status_code != 200:
        return _result("github_api", FAIL, f"http_{head.status_code} on branch-head",
                       core_remaining=remaining)
    sha = ((head.json().get("commit") or {}).get("sha"))
    return _result("github_api", OK, f"branch head resolved: {repo}@{branch} = {sha}",
                   core_remaining=remaining, head=sha)


async def check_anthropic(api_key: str | None, model: str,
                          create: Any = None) -> dict:
    """One minimal real messages.create — the exception class is the diagnosis
    (AuthenticationError = bad key, NotFoundError = bad model id,
    RateLimitError = quota). ``create`` is injectable for tests."""
    if not api_key:
        return _result("anthropic_api", SKIP, "not_configured (no ANTHROPIC_API_KEY)")
    if create is None:
        try:
            import anthropic
        except Exception:  # noqa: BLE001
            return _result("anthropic_api", FAIL,
                           "sdk_unavailable (anthropic package not importable)")
        create = anthropic.AsyncAnthropic(api_key=api_key).messages.create
    try:
        resp = await create(model=model, max_tokens=8,
                            messages=[{"role": "user", "content": "ping"}])
    except Exception as exc:  # noqa: BLE001 - the exception IS the diagnosis
        return _result("anthropic_api", FAIL, f"{type(exc).__name__}", model=model)
    return _result("anthropic_api", OK,
                   f"model {getattr(resp, 'model', model)} answered", model=model)


async def run_all(env: dict, repo: str, branch: str) -> list[dict]:
    import httpx

    results = [diagnose_config(env)]
    github_path = results[0]["github_path"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        results.append(await check_github_egress(client))
        token: str | None = None
        if github_path == "pat":
            token = env.get("GITHUB_TOKEN")
            results.append(_result("connector_token", SKIP,
                                   "not used (explicit GITHUB_TOKEN wins)"))
        elif github_path == "connector":
            conn = await check_connector_token(env, client)
            token = conn.pop("token", None)
            results.append(conn)
        else:
            results.append(_result("connector_token", SKIP, "not_configured"))
        if token:
            results.append(await check_github_api(token, client, repo, branch))
        else:
            results.append(_result(
                "github_api", SKIP if github_path == "none" else FAIL,
                "no token to test with — resolver would return unverifiable"))
    results.append(await check_anthropic(
        env.get("ANTHROPIC_API_KEY"),
        env.get("CURATOR_MODEL", "claude-opus-4-1")))
    return results


def main(argv: list[str]) -> int:
    repo = argv[1] if len(argv) > 1 else os.environ.get(
        "DIAG_REPO", "aliomraniH/mcp-assist-memory")
    branch = argv[2] if len(argv) > 2 else os.environ.get("DIAG_BRANCH", "main")
    results = asyncio.run(run_all(dict(os.environ), repo, branch))
    failed = False
    for r in results:
        mark = {"ok": "PASS", "skip": "SKIP", "fail": "FAIL"}[r["status"]]
        extras = {k: v for k, v in r.items() if k not in ("check", "status", "reason")}
        print(f"[{mark}] {r['check']:16} {r['reason']}"
              + (f"  {json.dumps(extras, default=str)}" if extras else ""))
        failed = failed or r["status"] == FAIL
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
