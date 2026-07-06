---
name: MCP SSE responses 421 on Reserved VM edge
description: Why the deployed /mcp endpoint returned 421 and the json_response fix
---

# Streamed MCP (SSE) responses get 421 Misdirected Request behind the Reserved VM edge

On the Reserved VM deployment, every *authenticated* request that reached the
mounted FastMCP app (`app.mount("/mcp", mcp.http_app(...))`) returned
`421 Misdirected Request` — `initialize`, `tools/list`, `DELETE`, even an
`Accept: application/json`-only request. Unauthenticated `/mcp` (short JSON 401
from our own middleware) and all plain FastAPI routes (`/`, `/healthz`, `/admin`)
worked fine. The identical authenticated request returned 200 locally.

**Root cause:** default FastMCP StreamableHTTP replies as **SSE**
(`text/event-stream`, streamed/chunked). The Replit/Google edge (GFE) rejects
those streamed responses with its own `421` page (body literally
`"Misdirected Request"` — note our app code has *no* 421 path; the mcp SDK's only
421 is DNS-rebinding host validation, which is disabled by default and emits
`"Invalid Host header"`, so it's not us). It is NOT a v2 regression — the transport
config was byte-identical to the prior version.

**Fix:** build the app with plain JSON responses:
`mcp.http_app(path="/", stateless_http=True, json_response=True)`.
Stateless MCP has no server-initiated messages, so nothing needs an open SSE
stream. Both framings are MCP-spec compliant and Claude clients accept
`application/json`.

**Why:** the edge does not proxy the streamed SSE response for POST /mcp;
json_response makes it a normal Content-Length JSON body the edge passes through.

**How to apply:** if a deployed FastMCP/StreamableHTTP endpoint 421s only on the
authenticated/streaming path while health routes are fine, switch to
`json_response=True` before suspecting host/auth/DB. Verifying locally will NOT
reproduce the 421 (no edge in front) — confirm the fix by redeploying.

**Debugging note:** the deployment DB is separate from the dev workspace DB, so
dev tokens 401 on prod; get a valid prod token from the prod `/admin` dashboard
(login with ADMIN_PASSWORD, scrape the token from the dashboard HTML) — never the
dev `admin_auth_tokens` row.
