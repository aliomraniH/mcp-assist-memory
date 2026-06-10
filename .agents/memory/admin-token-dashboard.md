---
name: Admin token dashboard for the MCP server
description: How the live MCP auth token is managed (separate DB, dashboard, cache TTL) and why.
---

# Dashboard-managed live MCP auth token

The MCP bearer token is NOT a static env var at runtime. It lives in a separate
PostgreSQL DB (`DATABASE_URL`, table `admin_auth_tokens`), managed via a
password-protected dashboard at `/admin`. The MCP memory store stays in SQLite
under `DATA_DIR` — the two stores are deliberately kept separate.

**Why separate DB:** token-management data must never mingle with agent memory.

- `MCP_AUTH_TOKEN` is optional now — only used to *seed* the first token on first
  boot (`AdminStore.ensure_token(seed=...)`). After that the dashboard is the
  source of truth. Rotation takes effect with no redeploy.
- Auth reads the live token via a `token_provider` callable passed into
  `BearerAuthMiddleware`; `/admin*` paths are exempt from bearer auth and gated
  by their own ADMIN_PASSWORD session instead.
- `/admin` session = HMAC-signed cookie (SESSION_SECRET); CSRF token is bound to
  the session cookie and enforced on `/admin/rotate`.

**Cache + multi-worker rule:** `AdminStore.get_active_token()` caches the active
token for `CACHE_TTL_SECONDS` (5s), then re-reads Postgres. A permanent cache
would cause auth split-brain under multiple uvicorn workers (rotation on one
worker never reaches the others). The DB also has a partial unique index
(`uniq_admin_auth_token_active` on `active WHERE active`) so there is always
exactly one active row.
**How to apply:** if you ever add more cache layers or workers, preserve the TTL
re-read (or switch to LISTEN/NOTIFY) — never cache the token indefinitely.

**Deployment:** Reserved VM (`deploymentTarget = "vm"`), single process, port
5000→80. VM is required because of the local SQLite store and always-on MCP
clients; autoscale would lose SQLite data and cold-start.
