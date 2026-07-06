"""Post-deploy / CI smoke test for the MCP surface.

Exercises the exact handshake a Claude connector performs, so a transport /
auth / host regression (like the fastmcp 3.4.3 421 that was only caught by a
manual curl after users reported the connector wouldn't connect) can never ship
silently again. The checks:

  * GET  /healthz               -> 200 and {"db": "ok"}
  * POST /mcp  (no token)       -> 401 (bearer gate holds)
  * POST /mcp  (bad token)      -> 401
  * POST /mcp  initialize       -> 200 (valid token)  [bare /mcp, no redirect]
  * POST /mcp  tools/list       -> 200 with EXACTLY EXPECTED_TOOL_COUNT tools

The /mcp requests are sent to the BARE path (no trailing slash) with redirects
disabled, so both the path-normalization shim and the stateless-JSON transport
are exercised end to end — a 307 or a 421 becomes a failed check instead of a
silent success.

Run against a live deployment:

    SMOKE_BASE_URL=https://your-deploy.example \\
    SMOKE_TOKEN=<an active token from /admin> \\
    python scripts/smoke_mcp.py

Base URL resolution (first that is set): argv[1], $SMOKE_BASE_URL, then
https://$REPLIT_DEV_DOMAIN, else http://localhost:5000. Token resolution:
$SMOKE_TOKEN then $MCP_AUTH_TOKEN. Exits 0 when every check passes and 1 on the
first failing check (so it can gate a deploy or a CI job).

The check functions here are imported by tests/test_smoke_mcp.py, which runs the
same assertions in-process against an ephemeral Postgres in CI — so the CI gate
and the post-deploy probe never drift apart.
"""
from __future__ import annotations

import json
import os
import sys

# The current tool surface. Bump this in the SAME commit that adds/removes a tool
# in server/mcp_server.py — a mismatch is exactly the "connector half-broke"
# signal this smoke test exists to catch.
EXPECTED_TOOL_COUNT = 23

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


class SmokeError(AssertionError):
    """A smoke check failed — the deploy is unhealthy."""


def _rpc_body(resp) -> dict:
    """Parse a JSON-RPC body from either a plain-JSON response (the configured
    ``json_response=True`` transport) or an SSE ``text/event-stream`` framing, so
    a transport flip is surfaced by a later assertion rather than a parse crash."""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise SmokeError(f"SSE response had no data frame: {text[:200]!r}")
    return json.loads(text)


def check_healthz(client) -> str:
    resp = client.get("/healthz")
    if resp.status_code != 200:
        raise SmokeError(f"/healthz status {resp.status_code}, expected 200")
    body = resp.json()
    if body.get("db") != "ok":
        raise SmokeError(f"/healthz db not ok: {body!r}")
    return "healthz: 200 db=ok"


def check_unauthorized(client) -> list[str]:
    """The bearer gate must reject a missing AND a bad token with 401 — the guard
    rail that keeps the memory store from being world-writable."""
    out = []
    no_tok = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False)
    if no_tok.status_code != 401:
        raise SmokeError(f"/mcp without token returned {no_tok.status_code}, expected 401")
    out.append("no-token: 401")
    bad = client.post(
        "/mcp",
        json=INITIALIZE,
        headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"},
        follow_redirects=False,
    )
    if bad.status_code != 401:
        raise SmokeError(f"/mcp with bad token returned {bad.status_code}, expected 401")
    out.append("bad-token: 401")
    return out


def check_handshake(client, token: str, expected_tools: int = EXPECTED_TOOL_COUNT) -> list[str]:
    """A real MCP initialize + tools/list with a valid token. Sends the BARE
    /mcp path with redirects disabled so a 307 (missing path shim) or a 421
    (host/origin guard regression) fails loudly instead of being followed."""
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    out = []

    init = client.post("/mcp", json=INITIALIZE, headers=auth, follow_redirects=False)
    if init.status_code != 200:
        raise SmokeError(
            f"/mcp initialize returned {init.status_code} (expected 200) — "
            f"body: {init.text[:200]!r}"
        )
    body = _rpc_body(init)
    if "result" not in body:
        raise SmokeError(f"initialize had no result: {body!r}")
    out.append("initialize: 200")

    listing = client.post("/mcp", json=TOOLS_LIST, headers=auth, follow_redirects=False)
    if listing.status_code != 200:
        raise SmokeError(
            f"/mcp tools/list returned {listing.status_code} (expected 200) — "
            f"body: {listing.text[:200]!r}"
        )
    tools = _rpc_body(listing).get("result", {}).get("tools")
    if tools is None:
        raise SmokeError(f"tools/list had no tools array: {listing.text[:200]!r}")
    if len(tools) != expected_tools:
        names = sorted(t.get("name") for t in tools)
        raise SmokeError(
            f"tools/list returned {len(tools)} tools, expected {expected_tools}: {names}"
        )
    out.append(f"tools/list: 200 ({len(tools)} tools)")
    return out


def run_smoke(client, token: str, expected_tools: int = EXPECTED_TOOL_COUNT) -> list[str]:
    """Run every check against ``client`` (an httpx.Client or a Starlette
    TestClient — both share .get/.post). Returns the pass lines; raises
    SmokeError on the first failing check."""
    if not token:
        raise SmokeError("no token supplied (set SMOKE_TOKEN or MCP_AUTH_TOKEN)")
    results = [check_healthz(client)]
    results += check_unauthorized(client)
    results += check_handshake(client, token, expected_tools)
    return results


def _resolve_base_url() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].rstrip("/")
    env = os.environ.get("SMOKE_BASE_URL")
    if env:
        return env.rstrip("/")
    dev = os.environ.get("REPLIT_DEV_DOMAIN")
    if dev:
        return f"https://{dev}".rstrip("/")
    return "http://localhost:5000"


def main() -> int:
    import httpx

    base_url = _resolve_base_url()
    token = os.environ.get("SMOKE_TOKEN") or os.environ.get("MCP_AUTH_TOKEN") or ""
    print(f"smoke: target {base_url}")
    try:
        with httpx.Client(base_url=base_url, timeout=30) as client:
            for line in run_smoke(client, token):
                print(f"  ok  {line}")
    except SmokeError as exc:
        print(f"  FAIL {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # network/DNS/etc — the deploy is unreachable
        print(f"  FAIL unreachable: {exc}", file=sys.stderr)
        return 1
    print("smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
