# mcp-assist-memory

A **generic, project-agnostic** memory / coordination / artifact server for
multi-agent and multi-surface work. One FastAPI process serves an **18-tool MCP**
over Streamable HTTP, backed by **Postgres (+ pgvector)**, deployed standalone on
a **Replit Reserved VM**.

This is **Tier 1** of the [reusability contract](./REUSABILITY.md): every project
reuses it as-is. It carries **zero domain terms** — project identity lives in
namespace *values*, never in tool names, tables, columns, or code.

## The 18 tools

| Group | Tools |
|---|---|
| memory | `memory_save` `memory_get` `memory_list` `memory_history` `memory_delete` `memory_search` |
| handoff | `handoff_save` `handoff_load` `handoff_list` |
| session | `session_create` `session_append_event` `session_get` `session_list` `session_events` |
| artifact | `artifact_put` `artifact_get` `artifact_list` |
| admin | `stats` |

`/healthz` (liveness) and the `/admin` token dashboard are served separately (not
MCP tools).

## Tenancy — namespace is the project boundary

**`namespace` == project == tenant.** One namespace per project (e.g.
`acme-billing`), with conventional sub-scopes by key prefix (`coord/…`,
`knowledge/…`). Every per-project tool takes a required `namespace` and **every
query filters on it** — there are no implicit cross-project reads. The `session`
and `session_event` tables carry `namespace` too, so episodic memory is scoped
like everything else.

**Artifacts are the deliberate exception:** they are content-addressed (sha256)
and dedup globally, so they are not tenant-scoped — the hash is the capability.

### Honest limit (and the v2 fix)

Under a **single shared `MCP_AUTH_TOKEN`**, namespace is a **soft** boundary: any
client holding the token can pass any namespace. It is real isolation for honest
clients, not enforced against a misbehaving one.

> **v2 auth roadmap — per-project tokens/roles.** A token scoped to
> `acme-billing` must not be able to read or write `other-project`. Until then,
> treat the namespace boundary as a convention enforced by client configuration,
> not by the server. (See REUSABILITY.md → "namespace is the tenant boundary".)

## Auth & the /admin dashboard

The live MCP bearer token is stored in Postgres (`admin_auth_tokens`) and
**rotatable from `/admin`** without a redeploy. `MCP_AUTH_TOKEN` seeds the first
token on initial boot; after that the dashboard is the source of truth.

- `/admin` is password-gated by **`ADMIN_PASSWORD`** (signed, HttpOnly session
  cookie, CSRF-protected). Without it the dashboard refuses logins.
- Present the token on `/mcp` as `Authorization: Bearer <token>` or, for
  headerless clients, `?token=<token>`.
- The only routes not behind the bearer gate are `GET /healthz`, the streamed
  `GET /artifact/{sha256}`, and `/admin` (which self-authenticates).

## Architecture

- One `AsyncConnectionPool` created in the FastAPI `lifespan` (`app.py`), injected
  via `deps`. Nothing else opens a connection. The pool is built with
  `check=AsyncConnectionPool.check_connection`, so a connection terminated
  server-side while idle is validated and discarded on checkout, never handed to a
  caller.
- One `config.py` (`pydantic-settings`) — the **only** place secrets are read.
- `StorageBackend` ABC (`storage/base.py`) implemented by `PostgresBackend`; the
  18 tools map 1:1 onto it.
- **Transparent reconnect:** reads and idempotent writes retry on a connection
  drop (`_retry_on_disconnect`), so e.g. `OperationalError: terminating connection
  due to administrator command` (SQLSTATE 57P01, Neon scale-down / PgBouncer) is
  retried on a fresh pooled connection instead of surfacing to the caller. Only
  genuine disconnects (`08xxx` / `57P0x` / an already-closed connection) are
  retried — other operational errors (lock timeout, too-many-connections) surface
  unchanged. Writes retry only when a replay is safe: `artifact_put`
  (content-addressed) always, and `memory_save`/`handoff_save`/`memory_delete`
  only when given an `event_id` (exactly-once). Non-idempotent writes (`session_*`,
  or a save with no `event_id`) run once and surface the error, so a transparent
  retry can never cause a silent double-write.
- Write-path `sanitize` strips forged delimiters/control chars; reads wrap values
  in `<<<UNTRUSTED_DATA>>>` markers (lethal-trifecta defense). **Note:** the
  `value` (and session-event `payload`) fields come back wrapped, so a consumer
  that needs the raw value — e.g. to `json.loads` a value that was a JSON string —
  must strip the markers first. Use `storage.sanitize.unwrap_value` (or
  `strip_untrusted` for a single string); the wrapping stays applied on every read.
- Bounded lifespan readiness (no unbounded `pool.wait()`), 50 MB artifact cap,
  ranged blob reads, idempotent `event_id` writes, idempotent blob backfill.

## Run locally

```bash
cp .env.example .env          # DATABASE_URL + MCP_AUTH_TOKEN + ADMIN_PASSWORD
make install                  # pip install -e ".[test]"
make migrate                  # apply migrations/0001_init.sql
make run                      # uvicorn app:app
curl localhost:8000/healthz   # {"status":"ok","db":"ok"}
# token: open http://localhost:8000/admin and sign in with ADMIN_PASSWORD
```

## Tests (real Postgres)

The suite runs against a **real** Postgres and skips cleanly if `DATABASE_URL` is
unset. The neutral test project is **`proj-test`** (never a real project name).

```bash
DATABASE_URL=... make test
```

CI (`.github/workflows/test.yml`) spins an **ephemeral Neon branch** per run,
migrates it, runs `pytest`, and deletes the branch. Set repo secrets
`NEON_API_KEY` and `NEON_PROJECT_ID` to enable it.

## Deploy on Replit (Reserved VM)

1. In **Secrets**, set `DATABASE_URL` (pooled endpoint), `MCP_AUTH_TOKEN`, and
   `ADMIN_PASSWORD` (plus optional Phase-3 keys).
2. Deploy as a **Reserved VM** (`deploymentTarget = "vm"`) — *not* Autoscale;
   the durability gate needs the process to persist.
3. The deploy `run` step runs `python scripts/migrate.py` then starts uvicorn.
4. Open `https://<your-vm>/admin`, sign in, and copy/rotate the token. Point each
   client at `https://<your-vm>/mcp`.

### Postgres / Neon

Use the **pooled** connection string for the running service; psycopg is
configured with `prepare_threshold=None` for PgBouncer transaction pooling. Tests
and `scripts/migrate.py` use a **direct** endpoint (the test pool keeps prepared
statements on).

## Boundary

This repo is **Tier 1 only**. Canvas-specific MCP tools, FHIR logic, and SDK
knowledge live in the separate `canvas-sdk-tools` repo — never here.

## Blob migration (filesystem → bytea)

```bash
python scripts/backfill_artifacts.py /path/to/old/blobstore
```
Idempotent (dedup by sha256), streams each file, skips/reports anything over the
50 MB cap, and verifies a random sample by checksum readback.
