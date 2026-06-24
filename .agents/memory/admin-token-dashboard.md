---
name: Admin token dashboard for the MCP server
description: How the live MCP auth token is managed (separate DB, dashboard, cache TTL) and why.
---

# Dashboard-managed live MCP auth token

The MCP bearer token is NOT a static env var at runtime. It lives in a separate
PostgreSQL DB (`DATABASE_URL`, table `admin_auth_tokens`), managed via a
password-protected dashboard at `/admin`. Agent memory lives in the same managed
Postgres but in separate tables (`admin_auth_tokens` vs the memory schema) — the
two concerns are deliberately kept apart.

**Why separate table:** token-management data must never mingle with agent memory.

**One token PER SURFACE (label), not one global token.** Each row has a `label`;
there is one active token per label. Surfaces: `web` (claude.ai connector — sends
the token as `?token=` because it can't set headers) and `desktop-cli` (Claude
Desktop + Claude Code CLI — both send `Authorization: Bearer`). The gate accepts
ANY active token, so each surface rotates/revokes independently. `/admin` shows
one card per surface (URL/command + its own rotate button, hidden `label` field;
`rotate_post` validates `label` against `SURFACE_LABELS` to block injection).

- `MCP_AUTH_TOKEN` only *seeds* the `web` token on first boot (so an existing
  claude.ai connector keeps working); `desktop-cli` is auto-generated.
  `AdminStore.ensure_tokens(labels, seed={"web": ...})` — never overwrites an
  existing label. After first boot the dashboard is the source of truth; rotation
  takes effect with no redeploy.
- Auth middleware accepts any value in `AdminStore.get_active_tokens()` (a set)
  via constant-time `hmac.compare_digest`, Bearer header OR `?token=`. `/admin*`
  is exempt from bearer auth and gated by its own ADMIN_PASSWORD session.
- `/admin` session = HMAC-signed cookie (SESSION_SECRET); CSRF token is bound to
  the session cookie and enforced on `/admin/rotate`.

**Cache + multi-worker rule:** `AdminStore.get_active_tokens()` caches the active
token set for `CACHE_TTL_SECONDS` (5s), then re-reads Postgres. A permanent cache
would cause auth split-brain under multiple uvicorn workers (rotation on one
worker never reaches the others). The DB has a partial unique index
(`uniq_admin_auth_token_active_label` on `(label) WHERE active`) so there is
exactly one active row PER LABEL. `init()` upgrades an old single-token table by
adding `label` (default `web`, preserving the live token) and dropping the old
global `uniq_admin_auth_token_active` index.
**How to apply:** if you ever add more cache layers or workers, preserve the TTL
re-read (or switch to LISTEN/NOTIFY) — never cache the token indefinitely.

**Deployment:** Reserved VM (`deploymentTarget = "vm"`), single process, port
5000→80. State is in managed Postgres (durable across redeploys); the VM target
is for always-on MCP clients without autoscale cold-starts.
