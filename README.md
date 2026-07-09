# mcp-assist-memory

A **generic, project-agnostic** memory / coordination / artifact server for
multi-agent and multi-surface work. One FastAPI process serves a **23-tool MCP**
over Streamable HTTP, backed by **Postgres (+ pgvector)**, deployed standalone on
a **Replit Reserved VM**.

This is **Tier 1** of the [reusability contract](./REUSABILITY.md): every project
reuses it as-is. It carries **zero domain terms** — project identity lives in
namespace *values*, never in tool names, tables, columns, or code.

### Capabilities at a glance

- **23-tool MCP** over Streamable HTTP (memory, handoff, session, artifact,
  coordination, feedback, admin).
- **Trust-boundary spine (Plan v2)** — actor-scoped exactly-once writes with
  visible dedup, read-back-verified acks (`verified_persisted`), standardized
  error payloads with remedies, write-time screening + quarantine, provenance
  tiers (`origin`, model attribution, `derived_from` lineage), trust decay
  (`needs_reverification`), PHI-safe `tool_events` telemetry, per-namespace
  variant profiles, and an `observation_log` feedback channel.
- **Namespace-scoped multi-tenancy** — every per-project query filters on `namespace`.
- **Resilient to transient DB drops** — the server transparently retries genuine
  connection losses (Neon scale-down / PgBouncer recycle, SQLSTATE `57P01`/`08xxx`)
  on a fresh pooled connection, and validates connections at checkout, so callers
  no longer have to retry. Retries are idempotency-gated, so they never double-write.
- **Prompt-injection resistance layer** — values are sanitized on write (forged
  markers are escaped one-way to `[[UNTRUSTED_DATA]]`, never reconstructed on read),
  instruction-shaped writes are screened and quarantined (visible in the write ack;
  `include_quarantined: true` opts reads back in), and reads come back wrapped in
  `<<<UNTRUSTED_DATA>>>` markers; `storage.sanitize.unwrap_value` recovers the raw
  value when a consumer needs it (e.g. to `json.loads`). Honest framing: these are
  layers, not proofs — deterministic screens and wrappers are bypassable by an
  adaptive attacker; adversarial evaluation is pending (see Phase 10 backlog in
  `DECISION-PROTOCOL.md`).
- **Content-addressed artifacts** (sha256, global dedup), 50 MB cap, ranged reads.
- **Per-surface rotatable tokens** (web vs. desktop-cli) managed from a password-gated `/admin` dashboard.

## The 23 tools

| Group | Tools |
|---|---|
| memory | `memory_save` `memory_get` `memory_list` `memory_history` `memory_delete` `memory_search` |
| handoff | `handoff_save` `handoff_load` `handoff_list` |
| session | `session_create` `session_append_event` `session_get` `session_list` `session_events` |
| artifact | `artifact_put` `artifact_get` `artifact_list` |
| coordination | `coord_health` `coord_drift_scan` `coord_reconcile` `coord_curate` |
| feedback | `observation_log` |
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

The per-surface tokens scope **which client surface** connects, not **which
project** it may touch: any holder of any active token can pass any namespace, so
namespace remains a **soft** boundary — real isolation for honest clients, not
enforced against a misbehaving one.

> **v2 auth roadmap — per-project tokens/roles.** A token scoped to
> `acme-billing` must not be able to read or write `other-project`. Until then,
> treat the namespace boundary as a convention enforced by client configuration,
> not by the server. (See REUSABILITY.md → "namespace is the tenant boundary".)

## Auth & the /admin dashboard

MCP tokens are stored in Postgres (`admin_auth_tokens`) and **rotatable from
`/admin`** without a redeploy. There is **one active token per surface**:

| surface | label | how the client sends it |
| --- | --- | --- |
| claude.ai web connector | `web` | `?token=<token>` in the URL (the web connector can't send headers) |
| Claude Desktop **and** the Claude Code CLI | `desktop-cli` | `Authorization: Bearer <token>` |
| Cursor | `cursor` | `Authorization: Bearer <token>` (via `.cursor/mcp.json`) |

The gate accepts **any** active token, so each surface can be **rotated or
revoked independently** — rotating `web` never disturbs `desktop-cli` or
`cursor`. The
`/admin` page shows one card per surface with a ready-to-paste URL/command and
its own rotate button.

`MCP_AUTH_TOKEN` seeds the **`web`** token on initial boot (so an existing
claude.ai connector keeps working); `desktop-cli` and `cursor` are
auto-generated. After first boot the dashboard is the source of truth.

- `/admin` is password-gated by **`ADMIN_PASSWORD`** (signed, HttpOnly session
  cookie, CSRF-protected). Without it the dashboard refuses logins.
- The only routes not behind the bearer gate are `GET /healthz`, the streamed
  `GET /artifact/{sha256}`, and `/admin` (which self-authenticates).

**Stateless transport.** `/mcp` runs in stateless HTTP mode
(`http_app(stateless_http=True)`): every request is self-contained, with no
in-memory session affinity. Client sessions therefore survive VM
restarts/redeploys, and the three surfaces share no server-side session state.

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
  only when given an `event_id` (exactly-once). The `session_*` writes also retry,
  with an explicit tradeoff: a drop in the narrow commit-ack window means
  `session_create` may orphan an empty, unreferenced session row, and
  `session_append_event` is **at-least-once** (a replay can append one duplicate
  event). For an append-only session log that beats failing the call outright; a
  save with no `event_id` still runs once and surfaces the error.
- Write-path `sanitize` strips forged delimiters/control chars; reads wrap values
  in `<<<UNTRUSTED_DATA>>>` markers (lethal-trifecta defense). **Note:** the
  `value` (and session-event `payload`) fields come back wrapped, so a consumer
  that needs the raw value — e.g. to `json.loads` a value that was a JSON string —
  must strip the markers first. Use `storage.sanitize.unwrap_value` (or
  `strip_untrusted` for a single string); the wrapping stays applied on every read.
- Bounded lifespan readiness (no unbounded `pool.wait()`), 50 MB artifact cap,
  ranged blob reads, idempotent `event_id` writes, idempotent blob backfill.
- **Semantic recall (`memory_search`):** when a `VOYAGE_API_KEY` is set, every
  `memory_save` embeds the entry (Voyage, `voyage-3.5-lite`, 1024-dim) into a
  nullable `embedding vector(1024)` column with an HNSW cosine index, and
  `memory_search` ranks live entries by meaning (`embedding <=> query`), then
  backfills keyword/substring matches up to `limit`. Embedding is **best-effort**:
  it runs before a connection is taken and never blocks (or fails) a write, and
  with no key the column stays NULL and search degrades to pure keyword — the
  pre-Phase-3 behavior. Every leg filters on `namespace` first (no cross-project
  recall). After enabling a key on an existing DB, embed old rows once with
  `python scripts/backfill_embeddings.py` (idempotent, only touches NULL rows).
- **Recall tuning (`hnsw.ef_search`):** the HNSW index is approximate, so a larger
  store can miss relevant hits unless its query-time recall parameter is tuned.
  `memory_search` sets `hnsw.ef_search` per-statement (transaction-local, semantic
  leg only) from `HNSW_EF_SEARCH` (default `100`; pgvector's own default is `40`).
  Higher = better recall, slightly slower search; it must be `>=` the search limit
  to take effect. Small stores return the same rows regardless, so the default is
  safe to leave alone — raise it (e.g. `200`) if a large tenant reports missing
  results, lower it toward `40` to shave latency. Very large tenants can also tune
  the index **build** parameters (`m`, `ef_construction`); see
  `migrations/0002_embeddings.sql` (changing those requires recreating the index).
- **Coordination reconciler (`coord_reconcile`):** when GitHub access is available,
  a `claim` (with `meta.repo` + `meta.pr`/`meta.branch`) is resolved against live
  GitHub — is PR #N merged? what is branch X's head? — and stamped with an
  append-only verdict; without access it stays `unverifiable` (never a wrong
  `current`). Access is sourced in priority order: an explicit `GITHUB_TOKEN`
  (read-only repo + PRs), else — on Replit — the **connected GitHub account** via
  the Replit connector (token fetched fresh per cache-window so it survives OAuth
  refresh), else disabled. Resolution is **best-effort**: a network/API failure
  yields `unverifiable`, never a blocked write. `GITHUB_WEBHOOK_SECRET` enables
  `POST /webhook/github` to reconcile affected claims on push / pull_request.
- **Memory curator (`coord_curate`):** when `ANTHROPIC_API_KEY` is set, a finished
  session can be consolidated write-side: `coord_curate(namespace, session_id)` reads
  the session's execution trace plus similar existing memories, asks the model what is
  worth persisting, and applies the resulting `ADD`/`UPDATE`/`MERGE`/`SUPERSEDE`/`NOOP`
  operations deterministically. Every op passes a fail-closed PHI gate first, claims
  without provenance (`meta.repo` + `meta.pr`/`branch`) are downgraded to notes,
  supersession sets a validity boundary (history is kept, never deleted), and writes are
  idempotent (deterministic `event_id`) so re-running a session never double-writes. It
  is **best-effort**: without the key the curator is disabled and `coord_curate` is a
  clean no-op (`{curator_enabled: false, operations: []}`), and any model/parse failure
  yields zero operations — never a wrong write. `dry_run=True` returns the proposed
  operations without writing. `CURATOR_MODEL` and `CURATOR_MAX_OUTPUT_TOKENS` tune it.
  Curated rows also carry a second `hyde_embedding`, so `memory_search` can match a
  future *question* (HyDE leg) as well as the stored statement.

## Run locally

```bash
cp .env.example .env          # DATABASE_URL + MCP_AUTH_TOKEN + ADMIN_PASSWORD
make install                  # pip install -c constraints.txt -e ".[test]"
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

## Smoke test (the connector handshake)

`scripts/smoke_mcp.py` performs the exact handshake a Claude connector does —
`initialize` + `tools/list` over `/mcp` with a valid token — and asserts HTTP 200
with the full **23-tool** surface, plus the guard rails (no/bad token ⇒ 401,
`/healthz` db ok). It exists so a transport/auth/host regression (like the
fastmcp 3.4.3 421) can never ship silently again.

- **Blocks a bad build:** the in-process half runs in CI via `pytest`
  (`tests/test_smoke_mcp.py`) against the ephemeral Neon branch — a broken
  handshake, gate, or tool count fails the build before it can deploy.
- **Flags an unhealthy live deploy:** run it against the deployed URL after a
  deploy. It exits non-zero on any failed check:

  ```bash
  SMOKE_BASE_URL=https://<your-vm> SMOKE_TOKEN=<active token> make smoke
  ```

  `.github/workflows/smoke.yml` runs this against a live URL on manual dispatch
  (or a 6-hour schedule); set repo secrets `SMOKE_BASE_URL` and `SMOKE_TOKEN` to
  enable it (it no-ops cleanly when unset).

## Deploy on Replit (Reserved VM)

1. In **Secrets**, set `DATABASE_URL` (pooled endpoint), `MCP_AUTH_TOKEN`, and
   `ADMIN_PASSWORD` (plus optional Phase-3 keys).
2. Deploy as a **Reserved VM** (`deploymentTarget = "vm"`) — *not* Autoscale;
   the durability gate needs the process to persist.
3. The deploy `run` step runs `python scripts/migrate.py` then starts uvicorn.
4. Open `https://<your-vm>/admin`, sign in, and copy/rotate the token. Point each
   client at `https://<your-vm>/mcp`.

### Pinned dependencies (deterministic builds)

`pyproject.toml` declares loose ranges, but the deploy build, `post-merge.sh`, and
`make install` all pass `-c constraints.txt`, so **prod installs the exact versions
verified in dev**. This is the guardrail against the class of failure that caused the
prod `/mcp` 421 outage (an unpinned build silently resolving a newer `fastmcp`).

To **intentionally upgrade** a dependency (so pins don't rot):

```bash
pip install -U <pkg>          # or `pip install -e .` to re-resolve a widened range
make test                     # AND exercise /mcp locally
make lock                     # regenerate constraints.txt from the verified env
# then redeploy — prod now installs the newly verified set
```

Never hand-edit versions in `constraints.txt`; always regenerate with `make lock`
(`scripts/lock-deps.sh`). The file header documents the same procedure.

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
