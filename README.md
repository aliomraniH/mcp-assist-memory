# mcp-assist-memory

A remote MCP server (Streamable HTTP) that gives Claude a shared memory layer
across surfaces — claude.ai web, Claude Code CLI, and Claude Code Desktop —
so work state survives surface switches. It stores append-only revisioned
memory entries, work-session timelines, cross-surface handoffs, and uploaded
artifacts (with automatic ingestion of debug-capture session ZIPs), all in
**PostgreSQL (Neon) with the `vector` extension** — relational rows, JSONB, and
`bytea` blobs — behind a single `StorageBackend` interface. A SQLite +
filesystem backend remains for local development and the test suite. It is
memory-only by design: no third-party credentials, no outbound API calls.

Full contract: see [SPEC.md](SPEC.md).

## Environment variables

All environment/secrets are read in one place (`src/assist_memory/config.py`,
pydantic-settings).

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL` | **yes** | — | Postgres **pooled** connection string; holds memory **and** the admin token table (Replit's managed Postgres sets this automatically) |
| `ADMIN_PASSWORD` | for `/admin` | — | Password gate for the token dashboard; without it the dashboard refuses logins |
| `MCP_AUTH_TOKEN` | no | — | Seeds the **first** token on initial boot; afterward the live token is owned by `/admin` |
| `SESSION_SECRET` | no | `ADMIN_PASSWORD`→random | Signs `/admin` session cookies |
| `MAX_ARTIFACT_BYTES` | no | `26214400` | Per-blob `bytea` write cap (25 MB) |
| `MAX_UPLOAD_MB` | no | `25` | Per-upload size cap |
| `MAX_TOTAL_STORAGE_MB` | no | `500` | Global storage cap |
| `PORT` | no | `8000` | HTTP port (Replit sets this automatically) |
| `LOG_LEVEL` | no | `INFO` | Log verbosity (structlog JSON to stdout) |
| `DATA_DIR` | no | `./data` | SQLite/blob location — **dev/test backend only** |
| `VOYAGE_/OPENAI_/ANTHROPIC_/LANGSMITH_API_KEY` | no | — | Declared for Phase 3; unused today |

## Run locally

The production entrypoint (`main.py` → FastAPI app) is Postgres-backed and needs
`DATABASE_URL` (a Postgres with the `vector` extension). It self-migrates on
startup and seeds the admin token; set `ADMIN_PASSWORD` to use the `/admin`
dashboard (and optionally `MCP_AUTH_TOKEN` to seed a known first token):

```bash
pip install -e ".[dev]"
export DATABASE_URL=postgresql://USER:PASS@HOST/db?sslmode=require
export ADMIN_PASSWORD=dev-admin
export MCP_AUTH_TOKEN=dev-token        # optional: seeds the first MCP token
python main.py
# health: curl http://localhost:8000/healthz  → {"status":"ok","db":"ok"}
# token:  open http://localhost:8000/admin and sign in with ADMIN_PASSWORD
```

The test suite needs no database — the SQLite backend covers the unit tests, and
the Postgres tests skip unless `DATABASE_URL` is set:

```bash
pytest                  # 55 passed, 12 skipped (no DATABASE_URL)
```

> Do **not** run `pytest` with `DATABASE_URL` pointed at a database you care
> about: the Postgres tests `TRUNCATE` tables between tests. Use a scratch DB.

## Deploy on Replit

Use the step-by-step agent prompt in
[docs/replit-agent-prompt.md](docs/replit-agent-prompt.md). In short:

1. Import this repo into Replit. The included `.replit` makes the **Run** button
   work (`python main.py`).
2. Provision a Postgres database with `pgvector` (Replit's managed Postgres
   sets `DATABASE_URL`/`PG*` for you) and set the `ADMIN_PASSWORD` Secret.
3. Deploy as a **Reserved VM** (not Autoscale): the process holds one long-lived
   connection pool and Phase 0's durability gate needs a persistent process. The
   app **self-migrates** on startup (applies `migrations/0001_init.sql`
   idempotently) and creates/seeds the admin token table; `make migrate` is the
   explicit alternative.
4. Open `https://<your-repl-url>/admin`, sign in with `ADMIN_PASSWORD`, and copy
   the live MCP token (or rotate it). Registration snippets are pre-filled there.
5. Because all state is in Postgres, **data survives redeploys** — there is no
   runtime filesystem to preserve. Your endpoint is
   `https://<your-repl-url>/mcp`; liveness is `GET /healthz`.

## Register the server on each client

Authentication works two ways with the same token: the
`Authorization: Bearer <token>` header (preferred), or `?token=<token>` in
the URL for clients that can't send custom headers. The query-string token
is never written to this server's logs, but treat such URLs as secrets.

**Claude Code CLI / Desktop:**

```bash
claude mcp add -s user --transport http assist-memory \
  https://<repl-url>/mcp \
  -H "Authorization: Bearer <token>"
```

**claude.ai web:** Settings → Connectors → Add custom connector. The web
connector UI doesn't let you attach a custom `Authorization` header, so use
the query-parameter form as the connector URL:

```
https://<repl-url>/mcp?token=<token>
```

**Cursor:** Settings → MCP → Add new MCP server (or edit `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "assist-memory": {
      "url": "https://<repl-url>/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

**Other agent tools** (Windsurf, Cline, custom agents, anything
MCP-compatible): point the client at `https://<repl-url>/mcp` with transport
`streamable-http`. If the client supports custom headers, send
`Authorization: Bearer <token>`; if not, append `?token=<token>` to the URL.

## Tool overview

| Group | Tools |
|---|---|
| Memory | `memory_save`, `memory_get`, `memory_list`, `memory_search`, `memory_history`, `memory_revert`, `memory_delete` |
| Sessions | `session_start`, `session_log`, `session_end`, `session_list`, `session_get` |
| Handoff | `handoff_save`, `handoff_load` |
| Artifacts | `artifact_upload`, `artifact_list`, `artifact_get` (ranged, 1 MB/page) |
| Meta | `server_status` |

Memory is append-only: every write is a new revision, deletes are
tombstones, and `memory_revert` restores by copying — history is never lost.
Uploading a debug-capture ZIP (a `session.json` export with
`schema_version "1.0"`) auto-creates the session record and stores its
`agent-handoff/brief.md` as a queryable memory entry
(`debug/<session_id>/brief`).

## Security

- Every request to `/mcp` requires the live bearer token (constant-time
  compare), which is managed in the `/admin` dashboard and rotatable without a
  redeploy. The only routes exempt from bearer auth are `GET /healthz`, `GET /`,
  and `/admin` (which enforces its own `ADMIN_PASSWORD` session, CSRF-protected,
  signed HttpOnly cookie).
- Stored free-text is sanitized on the write path (control characters stripped,
  untrusted-data delimiters defanged).
- ZIP uploads are checked for zip-slip, absolute paths, symlinks, entry
  count (≤ 2000), and decompression bombs (≤ 4 × `MAX_UPLOAD_MB`).
- Values matching common credential patterns are stored but tagged
  `possible-secret` with a warning in the response.
- Logs record request/tool metadata only (names, codes, durations,
  user-agents) — never tokens, query strings, or stored values.
