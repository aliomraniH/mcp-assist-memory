# Replit Agent prompt — deploy mcp-assist-memory (Phase 0, Postgres backend)

Copy everything below the line into Replit Agent after importing this
repository on branch **`claude/stoic-gauss-83afam`**.

---

You are deploying an **already-built, already-tested** application. Your job is
configuration, migration, deployment, and verification — **nothing else**.

## What this app is

A remote MCP (Model Context Protocol) server using Streamable HTTP, Python
3.11+, FastAPI + FastMCP. It provides shared memory, session tracking,
handoffs, and artifact storage for AI coding agents, consumed by several MCP
clients:

- Claude Code CLI / Desktop and Cursor/Windsurf (auth via `Authorization: Bearer`)
- claude.ai web custom connectors (auth via `?token=` query param — the web UI
  can't send custom headers)

Both auth paths are already implemented and tested. The MCP endpoint is
`POST /mcp`; the only anonymous routes are `GET /healthz` and `GET /`.

**Storage is PostgreSQL (Neon) with the `vector` extension** — not SQLite. All
state (memory revisions, sessions, handoffs, and artifact blobs as `bytea`)
lives in Postgres, so data is durable across redeploys. There is no local data
directory to preserve.

## Hard rules — do not violate

1. **Do not modify any source code, tests, `pyproject.toml`, or
   `migrations/0001_init.sql`.** The code is complete and live-validated. If
   something looks broken, STOP and report the exact error instead of "fixing"
   it. `migrations/0001_init.sql` is frozen.
2. **Do not add features, dependencies, OAuth, GitHub/LLM integrations, or
   analytics.** No outbound API calls.
3. **Do not weaken auth.** Never expose an unauthenticated endpoint other than
   `GET /healthz` and `GET /`. Never print, log, or commit `MCP_AUTH_TOKEN` or
   `DATABASE_URL`.
4. **Never run the test suite with `DATABASE_URL` set against the production
   database.** The Postgres integration tests `TRUNCATE` tables between tests —
   pointing them at prod would wipe memory. Run the deploy-gate tests
   **without** `DATABASE_URL` (see step 2).
5. **Deploy as a Reserved VM, not Autoscale.** The process holds one long-lived
   connection pool; Phase 0's durability gate requires a persistent process.

## Steps

1. **Provision Postgres.** Use a Neon Postgres database (Replit's Postgres
   integration or an external Neon project) that supports the `pgvector`
   extension. You need its **pooled (PgBouncer)** connection string.

2. **Install dependencies and run the deploy gate (no DATABASE_URL):**
   ```bash
   pip install -e ".[dev]"
   pytest        # run with DATABASE_URL UNSET
   ```
   Expect **50 passed, 12 skipped** (the 12 skipped are Postgres tests, which
   are intentionally skipped without a scratch database — they were already
   validated upstream). If anything *fails*, STOP and report the pytest output
   verbatim. Do not deploy and do not edit code.

3. **Configure Secrets** (Replit Secrets, not files):
   - `DATABASE_URL`: the Neon **pooled** connection string
     (`...-pooler...neon.tech/...?sslmode=require`).
   - `MCP_AUTH_TOKEN`: generate with
     `python -c "import secrets; print(secrets.token_urlsafe(32))"` and store as
     a Secret. Tell the user only where to read it (the Secrets pane) — never
     echo it into chat, logs, or files.
   - Optional overrides: `MAX_ARTIFACT_BYTES` (default 26214400 = 25 MB),
     `MAX_UPLOAD_MB` (25), `MAX_TOTAL_STORAGE_MB` (500), `LOG_LEVEL` (INFO).
     `PORT` is provided by Replit automatically.

4. **Apply the migration once** (creates the `vector` extension + tables). Run
   with the database owner/migrator `DATABASE_URL`:
   ```bash
   make migrate          # == psql "$DATABASE_URL" -f migrations/0001_init.sql
   ```
   It is idempotent (everything is `IF NOT EXISTS`). If `psql` isn't available,
   run the file's SQL through any client connected to the same database. Confirm
   the tables exist: `memory_entry, session, session_event, artifact_blob,
   artifact`.

5. **Deploy** (Reserved VM). The Run command is `python main.py`, which starts
   the FastAPI app (`assist_memory.app:app`) on `$PORT`. On startup it opens the
   pool, runs a bounded `SELECT 1` readiness probe, and logs `ready`. If the DB
   is unreachable the process exits on purpose (so the VM restarts) — that is
   correct, not a bug to patch.

6. **Verify** against the public URL (replace `$URL`; use the real token from
   Secrets):
   ```bash
   # 1. health probe is open and reports DB connectivity:
   curl -s $URL/healthz            # expect {"status":"ok","db":"ok"}

   # 2. MCP endpoint rejects anonymous requests:
   curl -s -o /dev/null -w '%{http_code}' -X POST $URL/mcp   # expect 401

   # 3. authenticated MCP initialize succeeds (JSON-RPC result with
   #    serverInfo.name "assist-memory"):
   curl -s -X POST "$URL/mcp" \
     -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'

   # 4. query-param auth also works (claude.ai web): repeat check 3 against
   #    "$URL/mcp?token=$MCP_AUTH_TOKEN" with no Authorization header.
   ```
   If any check fails, report the response body and status code and stop.

7. **Durability proof (Phase 0 done-gate).** After verification, **redeploy the
   Reserved VM** and confirm data persists:
   - Before redeploy, save a marker via an authenticated `tools/call` to
     `memory_save` (key `deploy/marker`, any value), or note an existing key.
   - After redeploy, `GET $URL/healthz` is `ok` and a `memory_get` for that key
     still returns the value. Because storage is Neon Postgres, it must survive.
   Report whether the marker persisted.

8. **Report back** with exactly:
   - the public base URL and the MCP endpoint URL (`https://.../mcp`)
   - confirmation that all four checks in step 6 passed and the step-7 marker
     persisted across a redeploy
   - where the secrets are stored (Secrets pane), without revealing them
   - confirmation the deployment target is **Reserved VM**
   - this registration cheat-sheet, real URL filled in, `<token>` left as a
     placeholder:

     **Claude Code CLI / Desktop:**
     ```
     claude mcp add -s user --transport http assist-memory https://<url>/mcp -H "Authorization: Bearer <token>"
     ```
     **claude.ai web:** Settings → Connectors → Add custom connector →
     URL: `https://<url>/mcp?token=<token>`

     **Cursor** (`~/.cursor/mcp.json`):
     ```json
     {"mcpServers": {"assist-memory": {"url": "https://<url>/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
     ```
     **Other MCP clients:** streamable-http transport to `https://<url>/mcp`
     with the bearer header, or `?token=<token>` if headers aren't supported.

## Success criteria

Run button works; deploy-gate tests pass (50 passed / 12 skipped, no
`DATABASE_URL`); the migration applied; the four endpoint checks pass on the
deployed URL; data persists across a Reserved-VM redeploy; the user has the
registration cheat-sheet; no source files were modified.
