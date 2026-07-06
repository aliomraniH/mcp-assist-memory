---
name: Deploy healthcheck probes "/" + main drops two operational shims
description: Why the app needs GET / for Replit deploy healthchecks and a /mcp path-normalization shim, and why every deploy-from-main must re-apply both.
---

# Replit deploy healthcheck hits `GET /`

The Replit deployment healthcheck probes the root path `GET /`, not `/healthz`.
A service whose root has no route returns 404 (and 500/connection-refused during
the cold-boot window before uvicorn binds), so the deploy can be marked unhealthy
even though the app is actually serving.

**Why:** the platform's default healthcheck path is `/`. `/healthz` is a DB-aware
readiness probe (503 when the pool is down) and is NOT what the platform hits.

**How to apply:** keep a lightweight, DB-independent `GET /` that returns 200 as
soon as the process is serving (liveness). Keep `/healthz` separate for DB-aware
readiness. Never make `/` depend on the DB, or a slow/cold DB fails liveness at boot.

## GitHub `main` DROPS two operational shims — re-apply on every deploy-from-main

GitHub `main` (the v2 "Trust Boundary" program) does NOT contain two shims that
live only in this Repl's working tree. Any "reset working tree to origin/main then
deploy" MUST re-apply both to `app.py` or the deploy regresses:

1. The `GET /` liveness root above (else the platform healthcheck 404s on cold boot).
2. A `/mcp` path-normalization shim in `MCPAuthMiddleware`: rewrite
   `request.scope["path"]` from exactly `"/mcp"` → `"/mcp/"` before dispatch.
   Without it, a bare `POST /mcp` (no trailing slash) gets a Starlette Mount **307**
   redirect to `/mcp/`; `curl` without `-L` and some MCP clients mishandle a 307 on
   POST — this breaks the deploy-prompt's own check #3 (`initialize` on bare `/mcp`).

**Why:** both were added in-Repl after v2 branched and were never merged upstream,
so resetting to `main` silently discards them (they sit in the pre-deploy backup
branch, not on `main`).

**How to apply:** after resetting the tree to `origin/main`, re-add both before
publishing; verify locally that `GET /`→200 and that an authenticated bare
`POST /mcp` `initialize`→200 (not 307).

## Unrelated but adjacent: "Session terminated" on the claude.ai connector

That symptom is an HTTP 401 from `POST /mcp?token=...`, i.e. the connector is using
a token that is no longer `active=TRUE` in `admin_auth_tokens` (rotated out via
/admin). The MCP transport surfaces a 401 as "Session terminated". It is NOT a
cold-start/crash — the gate accepts ANY active token; fix is to point the connector
at a currently-active token, not to add wake/replay logic.
