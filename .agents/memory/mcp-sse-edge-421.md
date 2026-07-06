---
name: Deployed /mcp 421 = fastmcp 3.4.3 HostOriginGuard (unpinned-dep drift)
description: Why the deployed /mcp returned 421 Misdirected Request in prod but 200 locally, and the real fix
---

# Deployed /mcp returns 421 "Misdirected Request" — it's a fastmcp version-drift bug, NOT the edge / NOT SSE

On the Reserved VM deployment, EVERY request routed into the mounted FastMCP app
(`app.mount("/mcp", mcp.http_app(...))`) returned `421 Misdirected Request`
(text/plain, 19 bytes) — for ALL methods including `OPTIONS`/`PATCH`/`PUT`
(which Starlette answers itself, never reaching the MCP transport). uvicorn's own
access log recorded the 421, with NO accompanying warning/traceback. Plain FastAPI
routes (`/`, `/healthz`, `/admin`, a 404) passed through fine. The identical
request returned 200 locally.

**Root cause: unpinned dependency drift.** `pyproject.toml` had `fastmcp>=2.3`
with NO lockfile. The dev workspace had fastmcp **3.4.2**; the deploy's fresh
`pip install -e .` resolved the latest, fastmcp **3.4.3** (+ mcp 1.28.1). fastmcp
**3.4.3 added `HostOriginGuardMiddleware`** (`fastmcp/server/http.py`) that, when
`host_origin_protection=True` (the default), rejects any request whose `Host`
header isn't in `DEFAULT_HOSTS` + configured `allowed_hosts` + the ASGI
`server` host — returning literally `Response("Misdirected Request", 421)` with
**no log line**. Behind the Replit edge the external deployment domain is not in
that set, so prod 421s uniformly while 3.4.2 (which has no such middleware) is 200.

**Why the earlier SSE / json_response theory was WRONG:** the 421 fires for
OPTIONS/PATCH that never reach the streamable transport, and for plain-JSON error
responses too — it's request-side host validation, not response framing. Setting
`json_response=True` did nothing (harmless to keep, but not the fix).

**Fix applied:** pin the verified-good versions so deploy == dev:
`fastmcp==3.4.2` and `mcp==1.27.2` in pyproject.toml. 3.4.2 has no host guard, so
/mcp is reachable; our own `MCPAuthMiddleware` bearer-token gate still enforces auth.

**Alternative fix (if you WANT to move to fastmcp >=3.4.3):** pass
`host_origin_protection=False` (or `allowed_hosts=["*"], allowed_origins=["*"]`)
to `mcp.http_app(...)`. Do NOT add that kwarg while pinned to 3.4.2 — it doesn't
accept it and the app won't boot. `_host_matches` supports `"*"` and fnmatch
patterns; the claude.ai web connector sends `Origin: https://claude.ai`, so if you
keep host protection you must also allow that origin or it 403s "Forbidden Origin".

**How to apply / general lesson:** if a deployed server 421s (or otherwise differs)
in prod but works locally, SUSPECT DEPENDENCY DRIFT FIRST — compare installed
versions (`pip index versions <pkg>` shows LATEST vs INSTALLED) and read the newer
version's code in a temp `pip install --target` dir. Unpinned deps + a fresh deploy
build = dev/prod skew. Verifying locally will NOT reproduce it.

**Debugging note:** the deployment DB is separate from the dev workspace DB, so dev
tokens 401 on prod; get a valid prod token from the prod `/admin` dashboard (login
with ADMIN_PASSWORD, scrape the token from the dashboard HTML) — never the dev
`admin_auth_tokens` row.
