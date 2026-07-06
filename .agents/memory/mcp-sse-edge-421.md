---
name: Deployed /mcp 421 = fastmcp HostOriginGuard — now CONFIGURED (not disabled)
description: Why the deployed /mcp returned 421 in prod but 200 locally, and how the Host/Origin guard is now run correctly
---

# Deployed /mcp 421 "Misdirected Request" — it's the fastmcp HostOriginGuard, NOT the edge / NOT SSE

On the Reserved VM deployment, EVERY request routed into the mounted FastMCP app
returned `421 Misdirected Request` (text/plain, 19 bytes) — for ALL methods,
including `OPTIONS`/`PATCH`/`PUT` that Starlette answers itself. Plain FastAPI
routes (`/`, `/healthz`, `/admin`, a 404) passed fine. The identical request was
200 locally.

**Root cause: fastmcp's `HostOriginGuardMiddleware` (added in 3.4.3).** When
`host_origin_protection=True` (the default), it rejects any request whose `Host`
isn't in `DEFAULT_HOSTS` (`127.0.0.1`, `localhost`, `::1`) + configured
`allowed_hosts` + the ASGI `server` host — returning `Response("Misdirected
Request", 421)` with **no log line**. It also 403s "Forbidden Origin" for a
browser `Origin` not in `allowed_origins`. Behind the Replit edge the external
deployment domain is not in that set, so prod 421s uniformly. The FIRST time this
bit us it arrived via **unpinned dependency drift**: `fastmcp>=2.3` + no lockfile
let a fresh deploy build pull 3.4.3 while dev had 3.4.2 (which has no guard).

**Why the earlier SSE / json_response theory was WRONG:** the 421 fires for
OPTIONS/PATCH that never reach the streamable transport, and for plain-JSON errors
too — it's request-side host validation, not response framing. `json_response=True`
did nothing for it (harmless to keep, but not the fix).

**Current state (the guard is now RUN, correctly configured — not disabled):**
- Pinned `fastmcp==3.4.3`, `mcp==1.28.1` so deploy == dev and guard behavior is stable.
- `app.py` `mcp.http_app(...)` passes `host_origin_protection=settings.mcp_host_origin_protection`
  (default True), `allowed_hosts=settings.mcp_allowed_hosts_list`,
  `allowed_origins=settings.mcp_allowed_origins_list`.
- Defaults live in `config.py`: hosts = `mcp-assist-memory.replit.app,*.replit.app,*.replit.dev`,
  origins = `https://claude.ai`. Both comma-separated, overridable via
  `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`; entries support fnmatch (`*.replit.app`);
  `"*"` disables a dimension. The guard is defense-in-depth ON TOP of our bearer gate.
- Regression coverage: `tests/test_host_origin_guard.py` asserts deploy host + `*.replit.*`
  + `https://claude.ai` are 200, an unlisted host is 421, a bad origin is 403, and the
  guard stays enabled/configured.

**Matching rules to remember (from fastmcp/server/http.py):**
- Host: `fnmatchcase` against DEFAULT_HOSTS + allowed_hosts + the (non-unspecified)
  ASGI server host. So `0.0.0.0` bind adds nothing; the external domain MUST be listed.
- Origin: only checked when an `Origin` header is present; loopback-to-loopback and
  Origin==request-origin are auto-allowed, otherwise it must match `allowed_origins`.
  The claude.ai web connector sends `Origin: https://claude.ai`.

**If /mcp 421s in prod again:** the deployment domain isn't in `allowed_hosts`
(new custom domain, or someone narrowed the default) — add it to `MCP_ALLOWED_HOSTS`.
If a browser client 403s "Forbidden Origin", add its origin to `MCP_ALLOWED_ORIGINS`.
Do NOT "fix" it by flipping `host_origin_protection` off — that discards the guard.

**General lesson:** if a deployed server 421s (or otherwise differs) in prod but
works locally, SUSPECT the host guard / dependency drift FIRST — compare installed
versions and read the newer version's code. Unpinned deps + a fresh deploy build =
dev/prod skew that local verification will NOT reproduce.

**Debugging note:** the deployment DB is separate from the dev workspace DB, so dev
tokens 401 on prod; get a valid prod token from the prod `/admin` dashboard (login
with ADMIN_PASSWORD) — never the dev `admin_auth_tokens` row.
