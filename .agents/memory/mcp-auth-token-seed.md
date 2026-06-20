---
name: MCP auth token — seed vs live
description: Why the MCP bearer token in Postgres can differ from the MCP_AUTH_TOKEN env var.
---

The `/mcp` bearer token is NOT the `MCP_AUTH_TOKEN` env var at runtime.

- On first boot `ensure_token(seed=MCP_AUTH_TOKEN)` inserts that value as the active row in `admin_auth_tokens`.
- `ensure_token` returns any existing active token unchanged — it never overwrites once a row exists. So the env var is a first-boot seed only.
- The live token is owned/rotatable from the `/admin` dashboard; rotations write new rows (plaintext `token` column, one active).

**Why:** Across redeploys the env seed may differ from the active DB token (e.g. after an /admin rotation). The auth middleware compares against the live DB token, not the env var.

**How to apply:** To authenticate against `/mcp` for verification, read the active token from `admin_auth_tokens WHERE active` (never echo it) — using the env var will 401 if the token was ever rotated. Durability is inherent: token + memory state live in the managed Postgres (DATABASE_URL is a global secret shared by dev and the deployed VM), so they survive redeploys.
