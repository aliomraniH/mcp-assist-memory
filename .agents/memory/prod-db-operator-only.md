---
name: Prod DB is read-only to agents
description: Production Postgres for the deployed app is separate from dev and agent SQL access is SELECT-only.
---
- The deployed app has its own production database (writes via the deployed MCP server do NOT appear in dev `DATABASE_URL`). Agent-side prod SQL (`environment: "production"`) is a read-only replica — no INSERT/UPDATE/DDL.
- **Why:** probe work needing a prod-side table write (e.g. `variant_profiles` upsert for compact_acks) cannot be done by the agent; it's operator-only.
- **How to apply:** for probes needing prod DB writes, either verify the behavior on the identical dev build and record the deviation, or ask the user/operator. `admin_auth_tokens.token` is stored plaintext in each environment's own DB — dev tokens can be read from dev DB for local MCP calls.
