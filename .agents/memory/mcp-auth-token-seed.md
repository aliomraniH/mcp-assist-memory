---
name: MCP auth token — seed vs live
description: Why the MCP bearer token in Postgres can differ from the MCP_AUTH_TOKEN env var.
---

The `/mcp` tokens are NOT the `MCP_AUTH_TOKEN` env var at runtime. There is one
active token PER SURFACE label (`web`, `desktop-cli`) — see admin-token-dashboard.md.

- On first boot `ensure_tokens(labels, seed={"web": MCP_AUTH_TOKEN})` inserts the
  env value as the active `web` row; `desktop-cli` is auto-generated.
- `ensure_tokens` never overwrites an existing label. So the env var is a
  first-boot seed for `web` only.
- Live tokens are owned/rotatable from the `/admin` dashboard; rotations write new
  rows (plaintext `token` column, one active per label).

**Why:** Across redeploys the env seed may differ from the active DB token (e.g.
after an /admin rotation, or because the running token is `desktop-cli`). The auth
middleware compares against the live DB token SET, not the env var.

**How to apply:** To authenticate against `/mcp` for verification, read an active
token from `admin_auth_tokens WHERE active` (e.g. `AdminStore.list_tokens()`,
never echo it) — using the env var will 401 if `web` was rotated or you need
`desktop-cli`. Durability is inherent: tokens + memory state live in the managed
Postgres (DATABASE_URL is a global secret shared by dev and the deployed VM), so
they survive redeploys.
