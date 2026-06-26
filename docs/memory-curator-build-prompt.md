# Replit Agent prompt — implement the Memory Curator

Copy everything below the line into Replit Agent **after** importing this repo
on the curator feature branch. Unlike the deploy prompts in
`attached_assets/`, this one **builds a new feature**, so it edits source, adds a
migration, and adds tests. The curator's behavioral spec is
[`docs/memory-curator.md`](./memory-curator.md); the system it plugs into is
[`docs/coord-spine.md`](./coord-spine.md).

---

You are implementing the **Memory Curator** — the asynchronous *write-side* of the
coordination spine. The *read-side* reconciler (`storage/reconcile.py`) already
keeps existing entries honest; you are adding the layer that decides **what gets
written in the first place**. It runs after a working agent's session ends, off
the hot path, reads the session's execution trace, asks an LLM what is worth
persisting, and a deterministic worker applies the resulting operations. It never
blocks a working agent and never invents facts the trace doesn't support.

Read `docs/memory-curator.md` first — it is the exact LLM contract (inputs,
per-candidate decision procedure, the JSON output schema, the clinical PHI gate,
the worked examples). Your job is to wire that contract into this codebase
**without breaking the "memory-only, degrades-to-disabled" design**.

## Hard rules — do not violate

1. **Mirror the existing optional-dependency pattern exactly.** The curator is to
   *writing* what `Embedder` is to search and `Resolver` is to reconciliation: an
   **optional, injected, best-effort** dependency. With no API key it must return
   a `DisabledCurator` (`enabled = False`) and the server must boot and behave
   **identically** — every existing test still passes, no new required secret.
2. **`config.py` is the ONLY module that reads the environment.** Add new settings
   there; import the `settings` singleton everywhere else. Do not add `os.environ`
   reads anywhere else (there is a grep-gate on this).
3. **The store is append-only — never hard-delete.** A `SUPERSEDE` sets a validity
   boundary on the old entry and writes the new one. History is preserved.
4. **The clinical PHI gate is a fail-closed hard rule.** The apply-worker (not just
   the LLM) must enforce it: any operation whose fields would carry patient
   identifiers is dropped before write. A dropped memory is recoverable; a leaked
   identifier is not. Test this.
5. **Do not weaken auth, expose unauthenticated routes, or make the working agent's
   path depend on the curator.** Curation is pull/async only.
6. **Never echo, log, or commit any API key.** Don't commit `data/`, `.env`, or
   runtime files (they're gitignored — keep it that way).

## What to look for (existing patterns to copy, not reinvent)

- **`storage/reconcile.py`** — copy its shape for the curator: a `Protocol`
  (`Curator`) with an `enabled` flag, a `DisabledCurator`, a real
  `AnthropicCurator`, and a `build_curator(settings)` factory that returns the real
  one only when the key is present. `httpx`/SDK imported lazily so the module never
  hard-requires it when disabled.
- **`app.py` lifespan (lines ~96–129)** — `build_embedder` / `build_resolver` are
  built there and injected: `deps.backend = PostgresBackend(pool, embedder=…,
  resolver=…)`. Add `curator=build_curator(settings)` the same way, and extend the
  `startup_ok` log line to include `curator=<bool>` alongside `embeddings`/`reconciler`.
- **`storage/postgres.py`** — `memory_save(..., kind=, tags=, meta=, event_id=)` is
  the write primitive (append-only via `_append`). `coord_reconcile` (line ~589) is
  the model for a new namespace-scoped coordination tool: `SELECT DISTINCT ON (key)`
  to reduce to latest live revision, then act. Reuse `event_id` for idempotency so a
  re-run of the same session doesn't double-write.
- **`server/mcp_server.py`** — every tool is a thin `@mcp.tool` that delegates to
  `_backend()`. Add the new tool here in the same style; keep all domain logic in
  the backend.
- **`migrations/`** — additive only (`0003_provenance.sql` is the template:
  `ADD COLUMN IF NOT EXISTS`, nullable, plus indexes; backfill optional). The
  `kind` CHECK **already allows** `claim`/`knowledge`/`decision`/`todo`/`note`
  (see `0001_init.sql` / `conftest.py`), so the curator's kinds need **no CHECK
  change** — do not widen it.
- **`tests/conftest.py` + `tests/test_reconcile.py`** — tests run against a REAL
  Postgres and inject a **fake** resolver (`.pulls` / `.heads` dicts) so no network
  is needed. Do the same: a `FakeCurator` returning canned operations.

## New secrets / tokens to add (in `config.py`)

The curator needs an LLM and (optionally) embeddings; it needs **no new GitHub
token** — reconciliation stays a separate concern.

| Setting | Required? | Purpose |
|---|---|---|
| `anthropic_api_key` | optional (already declared, line 29) | the curator LLM. Absent ⇒ `DisabledCurator`, curation is a no-op, server identical. |
| `curator_model` (new, default `claude-opus-4-8`) | optional | model id for the curator call. |
| `curator_max_output_tokens` (new, default e.g. `4096`) | optional | bound the JSON response. |
| `voyage_api_key` (already declared) | optional | reused to embed the curator's two strings (`summary`, `hyde`). Absent ⇒ keyword-only, embeddings skipped, no error. |

Keep them all optional with safe defaults so the service boots with none of them
set. Document them in `.env.example` (commented, no real values) the way the
existing optional secrets are documented.

## New API surface to add

1. **`storage/curator.py`** (new) — `Curator` Protocol, `DisabledCurator`,
   `AnthropicCurator` (lazy SDK import; builds the input envelope from
   `docs/memory-curator.md`, calls the model, parses the single JSON object —
   **fail closed**: a response that isn't valid JSON yields zero operations, never a
   crash), and `build_curator(settings)`.
2. **A deterministic apply-worker** in `storage/postgres.py`,
   `apply_curation(namespace, result, *, session_id)` that maps each op:
   - `ADD` → `memory_save` with the op's `kind`/`value`/`tags`/`meta`/`subjects`,
     plus `salience`/`confidence` and the dual embeddings.
   - `UPDATE` → new revision of the same key (the store is already revisioned).
   - `MERGE` → write the canonical key; mark folded duplicates superseded.
   - `SUPERSEDE` → set the validity boundary on `op.supersedes` (do **not** delete)
     and write the new entry.
   - `NOOP` → write nothing; surface the `reason` in the return value for audit.
   - **Every op passes the PHI gate first** (see below); failures are dropped and
     counted.
3. **MCP tool `coord_curate(namespace, session_id, dry_run=False)`** in
   `server/mcp_server.py` — loads the session's events (`session_events`) as the
   trace and similar memories (`memory_search`) as `similar_memories`, runs the
   curator, and (unless `dry_run`) applies the operations. Returns the operations,
   the apply counts (added/updated/superseded/noop/phi-dropped), and
   `curator_enabled`. When disabled it returns `{curator_enabled: false,
   operations: []}` — a clear no-op, never a guess. This mirrors how `coord_reconcile`
   is pull-triggered.
4. **`storage/phi.py`** (new) — a deterministic, conservative PHI guard
   (`assert_no_phi(op) -> bool`) the apply-worker calls on every op. Err toward
   treating data as identifying; when unsure, drop. This is the inline/sync part of
   the otherwise-async design.

## Migration to add — `migrations/0005_curation.sql` (additive, online-safe)

Follow the `0003` template (`ADD COLUMN IF NOT EXISTS`, nullable, indexed; no
CHECK change):

- `salience int`, `confidence real` — curator scores, surfaced on reads to rank recall.
- `valid_until timestamptz` — the supersession boundary (NULL = live). The
  "latest live revision" reads must treat `valid_until` in the past as not-live,
  the same way `tombstone` is treated today.
- `hyde_embedding vector(1024)` — the second embedding leg, so `memory_search` can
  match a future *question* against `hyde` as well as the statement against `summary`.
- Indexes: `(namespace, salience)` for ranking; a partial index for live rows.
- Mirror these same `ADD COLUMN`s into `tests/conftest.py`'s inline `SCHEMA` block
  so the suite stays self-contained.

## Tests to add (`tests/test_curator.py`, real Postgres, fake LLM)

Model them on `tests/test_reconcile.py`. Inject a `FakeCurator` that returns canned
operations — **no live Anthropic call in the suite.**

- **ADD** — an `ADD` op creates a live entry with the right `kind`, `salience`,
  `confidence`, `subjects`, and (when Voyage is enabled) both embeddings populated.
- **SUPERSEDE** — supersession sets `valid_until` on the old key and writes the new
  one; `memory_history` still shows the old revision (nothing hard-deleted); a
  latest-live read returns only the new one.
- **NOOP** — a `NOOP` op writes nothing and the `reason` is returned.
- **PHI gate (fail closed)** — an op whose value/meta carries a patient identifier
  is dropped and counted; nothing is written. Assert the gate refuses by default
  on ambiguous input.
- **Claim provenance required** — a `claim` op lacking `meta.repo` + (`pr`|`branch`)
  is rejected/downgraded, matching the reconciler's "no resolvable subject" rule.
- **Disabled path** — with no `anthropic_api_key`, `build_curator` returns the
  disabled curator, `coord_curate` returns `curator_enabled: false` with zero
  operations, and the server boots identically (no new required secret).
- **Idempotency** — running `coord_curate` twice for the same session does not
  double-write (the `event_id` gate holds).
- **`dry_run`** — `dry_run=True` returns the operations without writing.

Run the full suite as the gate (`pytest`); every pre-existing test must still pass.

## Docs to update

- **`README.md`** — bump the tool count and add `coord_curate` to the tool table
  (the count moved 18 → 21 in Phase 3; this adds one more).
- **`docs/coord-spine.md`** — the "The write side — Memory Curator" subsection
  already points at the spec; note there that the curator is now implemented and
  which secrets gate it (`ANTHROPIC_API_KEY`, optional `VOYAGE_API_KEY`).
- **`.env.example`** — add the new optional settings, commented, no values.

## Report back with

- the `startup_ok` log line showing `embeddings=<bool> reconciler=<bool>
  curator=<bool>` for the secrets you set;
- `pytest` output (pre-existing tests still green + the new `test_curator.py`);
- a `coord_curate(..., dry_run=True)` smoke result for one existing session
  (paste the operations JSON — confirm it parses and carries provenance on claims);
- confirmation that with **no** `ANTHROPIC_API_KEY` the server boots and
  `coord_curate` returns a clean disabled no-op.

## Success criteria

New `coord_curate` tool live; `storage/curator.py` + `storage/phi.py` follow the
`reconcile.py` disabled/enabled pattern; migration `0005` applies additively;
`test_curator.py` passes alongside the existing suite; the server boots and behaves
identically with none of the new secrets set; no secret is logged or committed; the
append-only/PHI guarantees hold.
