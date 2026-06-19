# Replit Agent prompt — deploy mcp-assist-memory (Phase 0, Postgres + /admin)

Copy everything below the line into Replit Agent. Deploy from the **default
branch** (latest), which now contains the Postgres memory backend **and** the
admin dashboard.

---

You are deploying an **already-built, already-tested** application. Your job is
configuration, deployment, and verification — **nothing else**.

## What this app is

A remote MCP (Streamable HTTP) server, Python 3.11+, FastAPI + FastMCP. It gives
AI coding agents shared memory, sessions, handoffs, and artifacts, consumed by
Claude Code CLI/Desktop, Cursor (bearer header) and claude.ai web (`?token=`).

- **Storage is PostgreSQL with `pgvector`** — memory revisions, sessions,
  handoffs, and artifact blobs (`bytea`) all live in Postgres, so data is
  durable across redeploys. There is **no** local data directory to preserve.
  On Replit's managed Postgres, `DATABASE_URL` and `PG*` are injected
  automatically.
- **The MCP auth token is managed at `/admin`** (password-gated dashboard,
  stored in the `admin_auth_tokens` table). Rotating it there takes effect
  immediately, no redeploy. The app **self-migrates** on startup (applies
  `migrations/0001_init.sql` idempotently) and seeds the first token.
- Routes: `POST /mcp` (bearer), `GET /healthz` (liveness, open), `GET /`
  (open), `/admin*` (own password session).

## Hard rules — do not violate

1. **Do not modify source code, tests, `pyproject.toml`, or
   `migrations/0001_init.sql`.** The code is complete and live-validated. If
   something looks broken, STOP and report the exact error. `0001_init.sql` is
   frozen.
2. **Do not add features, dependencies, OAuth, or analytics.** No outbound API
   calls.
3. **Do not weaken auth.** Never expose an unauthenticated endpoint other than
   `GET /healthz`, `GET /`, and the self-authenticating `/admin`. Never print,
   log, or commit `DATABASE_URL`, `ADMIN_PASSWORD`, or any token.
4. **Never run the test suite with `DATABASE_URL` set against the production
   database** — the Postgres tests `TRUNCATE` tables. Run the deploy gate
   **without** `DATABASE_URL` (step 2).
5. **Deploy as a Reserved VM, not Autoscale.**

## Steps

1. **Postgres.** Use the Replit-managed Postgres (it injects `DATABASE_URL`).
   Confirm the database supports the `pgvector` extension — the app runs
   `CREATE EXTENSION IF NOT EXISTS vector` on startup. If that extension is
   unavailable, STOP and report it (do not edit the migration).

2. **Install deps and run the deploy gate (no DATABASE_URL):**
   ```bash
   pip install -e ".[dev]"
   pytest        # run with DATABASE_URL UNSET
   ```
   Expect **55 passed, 12 skipped** (the skipped 12 are Postgres tests, already
   validated upstream). If anything *fails*, STOP and report the output
   verbatim. Do not deploy and do not edit code.

3. **Secrets** (Replit Secrets, not files):
   - `ADMIN_PASSWORD` — **required** to use `/admin`. Generate a strong value
     (`python -c "import secrets; print(secrets.token_urlsafe(24))"`). Tell the
     user where to read it (Secrets pane); never echo it.
   - `DATABASE_URL` — leave to Replit's managed Postgres; do not set by hand.
   - Optional: `MCP_AUTH_TOKEN` (seeds the first token only; the live token is
     owned by `/admin` afterward), `SESSION_SECRET`, `MAX_ARTIFACT_BYTES`,
     `MAX_TOTAL_STORAGE_MB`, `LOG_LEVEL`. `PORT` is provided by Replit.

4. **Deploy** (Reserved VM). Run command is `python main.py`. On startup the app
   opens the pool, runs a bounded `SELECT 1` probe, applies the migration, and
   seeds the admin token. If the DB is unreachable it exits on purpose so the VM
   restarts — that is correct, not a bug to patch. (No manual migration step is
   needed; `make migrate` exists only as an explicit alternative.)

5. **Get the token + verify.** Open `https://<url>/admin`, sign in with
   `ADMIN_PASSWORD`, and copy the live token. Then check (replace `$URL`,
   `$TOKEN`):
   ```bash
   curl -s $URL/healthz                                   # {"status":"ok","db":"ok"}
   curl -s -o /dev/null -w '%{http_code}' -X POST $URL/mcp   # 401 (anonymous)
   curl -s -X POST "$URL/mcp" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'
   # expect a JSON-RPC result with serverInfo.name "assist-memory"
   # then repeat the authed call against "$URL/mcp?token=$TOKEN" (no header) for web clients
   ```
   If any check fails, report the response body and status and stop.

6. **Durability proof (Phase 0 done-gate).** Save a marker (`memory_save`, key
   `deploy/marker`) via an authed `tools/call`, **redeploy the Reserved VM**,
   then confirm `/healthz` is `ok` and `memory_get` for that key still returns
   the value. Because storage is Postgres, it must survive. Report the result.

7. **Report back:** the public base URL and `https://.../mcp`; that all checks
   in step 5 passed and the step-6 marker persisted across redeploy; that the
   token is obtained from `/admin` (and `ADMIN_PASSWORD` lives in Secrets,
   unrevealed); and that the deployment target is **Reserved VM**. Point the
   user at `/admin` for the pre-filled Claude Code / claude.ai / Cursor
   registration snippets.

## Success criteria

Run button works; deploy-gate tests pass (55 passed / 12 skipped, no
`DATABASE_URL`); the app self-migrated; the four endpoint checks pass; data
persists across a Reserved-VM redeploy; the user can manage the token at
`/admin`; no source files were modified.
