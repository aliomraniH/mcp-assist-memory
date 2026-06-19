# Phase 0 Implementation Sub-Spec — Postgres/pgvector backend on Replit

**Status:** awaiting human sign-off. No code is written until this is approved.

This maps the mission prompt onto the *real* repo (`src/assist_memory/` package
layout) and onto mneme's pool/config/migration discipline. Where the prompt's
idealized file tree or schema disagrees with what's actually in the repo, the
conflict is called out under **Deviations requiring sign-off**.

---

## 1. What the repo actually is today

- **Single Python package** `src/assist_memory/`, not a top-level module tree.
- **Fully synchronous.** `SqliteFsBackend` uses `sqlite3` + a `threading.Lock`;
  all 18 `@mcp.tool` functions are plain `def`; the ASGI app is built by
  `server.create_app()` → `mcp.streamable_http_app()` wrapped in two ASGI
  middlewares (`BearerAuthMiddleware`, `AccessLogMiddleware`). Entry point is
  `main.py` → `uvicorn.run(app)`.
- **StorageBackend ABC** (`storage/base.py`) is the only seam. Its methods are
  sync. Tool code never touches storage internals — exactly the seam we swap.
- **Config** (`config.py`) is a frozen dataclass; `load_config()` is already the
  *only* place `os.environ` is read (grep-gate already satisfied today).
- **Models** (`models.py`): `MemoryRevision` carries `value: str | None` +
  `value_is_json: bool` (text + flag, NOT jsonb), `deleted: bool` (NOT
  `tombstone`), `tags: list[str]`, ISO-`Z` string timestamps via `now_iso()`.
  `Artifact` has a random `artifact_id` (`art_<hex>`) distinct from `sha256`,
  plus `storage_path`. Blobs are content-addressed on disk; metadata rows are
  keyed by `artifact_id`, and many `artifact_id`s may share one `sha256`.
- **The 18 tools** (confirmed by reading `server.py`): `memory_save/get/list/
  search/history/revert/delete` (7), `session_start/log/end/list/get` (5),
  `handoff_save/load` (2), `artifact_upload/list/get` (3), `server_status` (1).
- **Tests** drive tools through an in-memory MCP client (`conftest.call`), which
  already `await`s — so async tools work unchanged in the harness.

## 2. mneme patterns being reused verbatim

- One `AsyncConnectionPool`, built `open=False` in the FastAPI lifespan, stored
  on `app.state.pool`, handed to every consumer via a `pool_factory`/injection;
  no other module opens a connection (`db_mcp/server.py`, `server.py`).
- One `pydantic-settings` `Settings` with a cached singleton (`config.py`),
  `SecretStr` for secrets, `as_log_safe()` for startup logging.
- structlog JSON to stdout configured once (`_configure_logging`).
- Frozen, numbered migrations under `migrations/`; `0001_init.sql` header says
  "FROZEN. Do not modify." Neon-tuned pool kwargs (keepalives, `max_idle`).
- `/healthz` does a bounded `SELECT 1` against the pool and returns ok/degraded.

## 3. Target file layout (mapped onto the real package)

| Prompt item | Actual path |
|---|---|
| `app.py` (FastAPI + lifespan + structlog, mounts `/mcp`+`/healthz`) | `src/assist_memory/app.py` (NEW) — replaces the `create_app` wiring in `server.py` and the body of `main.py` |
| `config.py` (pydantic-settings) | `src/assist_memory/config.py` (REWRITE) |
| `server/mcp_server.py` (inject backend) | keep `src/assist_memory/server.py::build_mcp` (already takes `backend`); tool signatures untouched |
| `storage/postgres.py` | `src/assist_memory/storage/postgres.py` (NEW) |
| `storage/sanitize.py` | `src/assist_memory/storage/sanitize.py` (NEW) |
| `migrations/0001_init.sql` | `migrations/0001_init.sql` (NEW, FROZEN) at repo root |
| `tools/backfill_artifacts.py` | `tools/backfill_artifacts.py` (NEW) at repo root |
| `.env.example`, `Makefile` | repo root (NEW) |
| `pyproject.toml` deps | add `psycopg[binary]`, `psycopg_pool`, `pydantic-settings`, `structlog`, `pgvector`, `fastapi` |

Not created (later phases): `server/embeddings.py`, `server/recall.py`,
`coordination/*`, `0002_*`, `0003_*`.

## 4. Async conversion (the central structural decision)

The prompt mandates an **AsyncConnectionPool** + `await pool.open()` +
`async with asyncio.timeout(15)` lifespan, *and* says "keep the StorageBackend
ABC unchanged." Those conflict: the ABC is sync today. Resolution proposed:

- Convert the **ABC methods, `PostgresBackend`, and the 18 tool functions to
  `async def`.** The MCP **wire contracts** — tool name, parameters, and
  returned JSON shape — stay byte-identical; only `def`→`async def` changes.
  "18 contracts unchanged" is read as the wire contract, which is the actual
  deliverable and what Canvas depends on.
- `SqliteFsBackend` is kept but also made `async` (thin `async def` wrappers
  over the existing sync body) so the existing 40 tests keep passing without a
  live Postgres, OR is left in place behind an async shim. (Recommendation:
  make it async to keep one ABC shape.)
- `server.create_app()` is removed; `build_mcp(settings, backend)` stays and is
  mounted by `app.py`. `main.py` shrinks to `import uvicorn; uvicorn.run("assist_memory.app:app", ...)`.

Alternative (if async is rejected): use psycopg's **sync** `ConnectionPool`,
keep everything sync, and approximate the bounded-wait lifespan with a sync
`SELECT 1` probe under a thread timeout. This keeps the ABC literally
byte-identical but departs from the prompt's async lifespan spec and from
mneme. **Recommended: async.**

## 5. `migrations/0001_init.sql` (FROZEN once merged)

```sql
-- assist-memory initial schema. FROZEN. New columns/tables => new migration.
CREATE EXTENSION IF NOT EXISTS vector;   -- extension only; knowledge table is Phase 3
-- gen_random_uuid() is built-in on PG13+ (Neon is PG15/16) => no pgcrypto needed.

CREATE TABLE IF NOT EXISTS memory_entry (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace      TEXT NOT NULL,
    key            TEXT NOT NULL,
    revision       INT  NOT NULL,                       -- server-computed max+1 in-txn
    kind           TEXT NOT NULL CHECK (kind IN ('note','decision','todo','handoff','config')),
    value          TEXT,                                -- raw stored text (see §Deviation A)
    value_is_json  BOOLEAN NOT NULL DEFAULT FALSE,
    tags           TEXT[] NOT NULL DEFAULT '{}',
    source_surface TEXT NOT NULL DEFAULT 'other',
    event_id       UUID,                                -- nullable; idempotency key
    tombstone      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, key, revision)
);
CREATE INDEX IF NOT EXISTS idx_memory_ns_key ON memory_entry (namespace, key, revision DESC);
CREATE INDEX IF NOT EXISTS idx_memory_tags   ON memory_entry USING gin (tags);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_event_id
    ON memory_entry (event_id) WHERE event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS session (
    session_id  TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    surface     TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_session_ns ON session (namespace, created_at DESC);

CREATE TABLE IF NOT EXISTS session_event (
    session_id  TEXT NOT NULL REFERENCES session(session_id),
    seq         INT  NOT NULL,                          -- per-session monotonic
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    type        TEXT NOT NULL,
    message     TEXT NOT NULL,
    data        JSONB,
    PRIMARY KEY (session_id, seq)
);

-- Content-addressed immutable blob store (bytea). This is the prompt's "artifact".
CREATE TABLE IF NOT EXISTS artifact_blob (
    sha256       CHAR(64) PRIMARY KEY,
    bytes        BYTEA NOT NULL,
    size         INT NOT NULL,
    content_type TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Artifact metadata (keyed by the app's art_<hex> id; many ids -> one sha256).
CREATE TABLE IF NOT EXISTS artifact (
    artifact_id      TEXT PRIMARY KEY,
    namespace        TEXT NOT NULL,
    filename         TEXT NOT NULL,
    mime             TEXT NOT NULL,
    size_bytes       INT NOT NULL,
    sha256           CHAR(64) NOT NULL REFERENCES artifact_blob(sha256),
    uploaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_surface   TEXT NOT NULL,
    session_id       TEXT,
    memory_key       TEXT,
    tags             TEXT[] NOT NULL DEFAULT '{}',
    is_debug_capture BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_artifact_ns      ON artifact (namespace);
CREATE INDEX IF NOT EXISTS idx_artifact_session ON artifact (session_id);
```

Revision is computed in-txn (`SELECT COALESCE(MAX(revision),0)+1 ... FOR UPDATE`
guarded by the `UNIQUE (namespace,key,revision)` constraint), never client-supplied.

## 6. `PostgresBackend` (every ABC method over `self.pool.connection()`)

- **Writes** pass values through `sanitize()` before insert. **Reads** wrap the
  returned `value`/`content`/`summary`/`message` strings in
  `<<<UNTRUSTED_DATA>>> … <<<END>>>`.
- **Idempotent save:** `save_revision(..., *, event_id: str | None = None)` —
  keyword-only, defaulting `None`, added to the ABC (backend-internal; **no
  tool signature changes** because no tool passes it in Phase 0). If `event_id`
  is non-null and already present, return that row's revision instead of
  appending. Enforced by `uq_memory_event_id` + `ON CONFLICT (event_id) DO
  NOTHING` then re-select.
- **Timestamps:** stored `TIMESTAMPTZ`, formatted back to the exact
  `YYYY-MM-DDTHH:MM:SSZ` string by the backend so returned JSON is unchanged.
- **bytea reads:** `read_artifact_bytes(id, off, len)` →
  `SELECT substring(bytes FROM %s FOR %s)` (1-based offset = off+1) on
  `artifact_blob`; never selects whole `bytes`.
- **Write cap:** `store_artifact` rejects blobs > `max_artifact_bytes` (25 MB)
  with a clear `ToolFault`; dedupe via `INSERT ... ON CONFLICT (sha256) DO NOTHING`.
- **usage()/ensure_capacity():** there is no data dir. `used_bytes` computed in
  SQL = `sum(size)` over `artifact_blob` + `sum(octet_length(value))` over
  `memory_entry`. Counts via the same aggregate query shape as today.
- **PgBouncer:** Neon pooled endpoint is transaction-pooled, so the pool is
  built with `kwargs={"prepare_threshold": None, ...}` to disable server-side
  prepared statements.

## 7. `config.py` (pydantic-settings — the only env reader)

`Settings(BaseSettings)` with: `database_url: SecretStr`,
`mcp_auth_token: SecretStr`, optional `voyage_api_key/openai_api_key/
anthropic_api_key/langsmith_api_key: SecretStr | None` (declared, unused until
Phase 3), `max_artifact_bytes: int = 25*1024*1024`, plus the fields current code
already relies on (`log_level`, `port`, `max_total_storage_mb`, and `max_upload_*`
preserved as fields/properties so `server.py` is untouched). Cached `settings`
singleton via `@functools.cache`. Grep-gate: `os.environ` only here.

## 8. Lifespan (bounded, fail-fast — `app.py`)

Pool built `open=False, min_size=0, max_size=10, timeout=10.0,
reconnect_timeout=30.0, max_idle=60.0,
kwargs={"connect_timeout":10, "prepare_threshold":None,
"options":"-c statement_timeout=15000 -c idle_in_transaction_session_timeout=15000"}`.
Then `await pool.open()`; readiness probe `async with asyncio.timeout(15): SELECT 1`;
on timeout/`OperationalError` log + raise (terminal crash → Replit restarts VM).
`/healthz`: unauthenticated, bounded `SELECT 1`, returns
`{"status":"ok","db":"ok"}` (200) or degraded (503); no data. Bearer auth stays
on `/mcp` only (existing `BearerAuthMiddleware`, now reading
`settings.mcp_auth_token`).

## 9. `tools/backfill_artifacts.py` (one-time, NOT in the frozen migration)

Survey first (`count`, `max size`). Stream each content-addressed file from the
old `./data/blobs`, compute sha256, `INSERT ... ON CONFLICT (sha256) DO NOTHING`
into `artifact_blob` in batches; idempotent/re-runnable. Verify
row-count == file-count and re-checksum a random sample by reading bytea back.
Skip-and-report anything over the cap. (On a fresh standalone deploy with no
prior `./data`, this is a no-op; it exists for migrating an existing instance.)

## 10. Tests (against a REAL Postgres, like mneme's `unit_pool`)

conftest gains an async `pg_pool` fixture using `DATABASE_URL`, applying
`0001_init.sql`, with `TRUNCATE ... CASCADE` isolation between tests; **skips if
`DATABASE_URL` is unset**. New: `test_round_trip.py` (all 18 tools, asserting
values), `test_sanitize.py`, `test_blob_durability.py`, `test_idempotency.py`,
`test_healthz.py`. Existing SQLite tests remain green via the async shim.

## 11. Replit / Makefile / .env.example

- `.replit`: keep `deploymentTarget = "vm"` (Reserved VM); `run` →
  `uvicorn assist_memory.app:app`; drop `DATA_DIR`.
- `Makefile`: `migrate` (`psql "$DATABASE_URL" -f migrations/0001_init.sql`),
  `run` (uvicorn), `test` (pytest).
- Secrets (`DATABASE_URL`, `MCP_AUTH_TOKEN`, declared Phase-3 keys) live in
  Replit Secrets, read only through `config.py`. Two DB roles: owner/migrator
  for `make migrate`, read-only for read paths (documented in `.env.example`).

---

## Deviations requiring sign-off

- **A. `value TEXT` + `value_is_json BOOLEAN`, not `value JSONB`.** The app's
  contract round-trips a *string* `"hello"` distinctly from JSON; a single
  `jsonb` column cannot store a bare non-JSON string without wrapping, which
  risks changing returned values. Keeping text+flag preserves exact round-trip
  and matches `models.MemoryRevision`. (Prompt asked for `value jsonb`.)
- **B. Two artifact tables** (`artifact_blob` content-addressed bytea +
  `artifact` metadata), because the app separates `artifact_id` from `sha256`
  and dedupes blobs. The prompt's single "artifact" table describes only the
  blob half.
- **C. Column/table names** follow the prompt (`memory_entry`, `tombstone`,
  `session`, `session_event`, `artifact`) and are mapped to the existing model
  fields (`deleted`, etc.) inside the backend — internal only.
- **D. Async conversion** of the ABC + 18 tool functions (§4). Wire contracts
  unchanged; `def`→`async def` only.
- **E. `event_id`** added as a keyword-only ABC arg on `save_revision`
  (backend-internal); no tool signature changes.
- **F. `server_status`** has no `data_dir` anymore: `used_mb` comes from SQL;
  the `data_dir_free_mb` key is retained but reports the VM filesystem free
  space (`shutil.disk_usage("/")`) to keep the return shape identical.
- **G. No `pgcrypto`** extension — `gen_random_uuid()` is built-in on Neon's
  PG version. (mneme included it; verified unnecessary here.)
- **H. `max_artifact_bytes` (25 MB)** equals the existing `max_upload_mb` cap;
  the per-upload check stays and the backend adds a defense-in-depth cap.
- **I. Sanitize-on-write is the enforced defense; the `<<<UNTRUSTED_DATA>>>`
  wrapper is NOT applied to tool return values.** Wrapping raw memory values
  would corrupt `decoded_value()`, `memory_revert`, and `handoff_load`'s JSON
  and change the 18 contracts; wrapping artifact bytes would corrupt
  byte-identity. So `sanitize()` (control-char strip + sentinel defang) runs on
  every write, and `wrap_untrusted()` is provided + unit-tested as the helper
  the prompt-assembly layer uses when injecting recalled text into a model
  prompt (see `storage/sanitize.py`). This is the only internally consistent
  reading of "wrap returned strings" + "18 contracts unchanged".

---

## Implementation status (as built — APPROVED, implemented on `claude/stoic-gauss-83afam`)

Implemented per the approved decisions (async; `value TEXT`+`value_is_json`;
two artifact tables). Live-validated against a real PostgreSQL 16 + pgvector:

- `pytest`: **62 passed** with `DATABASE_URL` set (44 SQLite + 18 new Postgres
  tests: round-trip of all 18 tools, sanitize, blob durability/dedupe,
  idempotency, healthz); **50 passed / 12 skipped** when `DATABASE_URL` is unset
  (Postgres tests skip cleanly).
- App smoke via the real `app.py` (now a **FastAPI** outer app, deviation D/§3):
  lifespan startup → bounded `SELECT 1` probe → ready; `/healthz` 200
  `{"status":"ok","db":"ok"}`; `/mcp` 401 without bearer, 200 `initialize` with
  bearer; structlog JSON to stdout; clean shutdown.
- `migrations/0001_init.sql` applies cleanly via `psql` (the `make migrate`
  path) on a fresh DB.
- `tools/backfill_artifacts.py` validated: dedupes by sha256, idempotent on
  re-run, `--verify` re-checksums a sample byte-identically.

Remaining (human-only) gate before Phase 1: redeploy the Reserved VM and confirm
rows + blobs persist, and that a `handoff_save` from one surface is read by
`handoff_load` on another (live cross-surface). Not started: Phase 1+.
