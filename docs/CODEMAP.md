# CODEMAP — where the load-bearing code lives (Phase 0, T0.1)

Line numbers are as of the Phase 0 commit; later phases keep this file updated
when they move something structural (exact lines will drift — anchor on the
symbol names, which are stable).

## Tool registration

* `server/mcp_server.py:46` — the single `FastMCP(name="assist-memory")` instance.
* `server/mcp_server.py:50-265` — all `@mcp.tool` registrations (22 tools), thin
  relays into the injected `StorageBackend`.
* `app.py:94` — `mcp.http_app(path="/", stateless_http=True)` builds the ASGI app;
  `app.py:173` mounts it at `/mcp`; `app.py:122` injects the backend on `deps`
  during the lifespan.

## memory_save write path, end to end

1. `server/mcp_server.py:51` `memory_save` tool → `_backend().memory_save(...)`.
2. `storage/postgres.py` `PostgresBackend.memory_save` (decorated
   `@_retry_if_idempotent`, `storage/postgres.py:80`) → `_append(...)`.
3. `_append` (`storage/postgres.py:322`):
   a. embeds BEFORE checkout (`_maybe_embed`, best-effort, never raises);
   b. `_split_meta` projects the coordination envelope into indexed columns;
   c. `sanitize(value)` (`storage/sanitize.py:29`) scrubs control chars and
      forged `<<<UNTRUSTED_DATA>>>`/`<<<END>>>` delimiters;
   d. `_content_hash` over the sanitized value;
   e. takes a pooled connection, **dedup check**: `_seen_event(conn, event_id)`
      (`storage/postgres.py:315`) — a seen event returns the existing row
      unchanged (silent until Phase 2 makes it visible);
   f. INSERT with server-computed `revision = COALESCE(MAX(revision),0)+1`
      inside `conn.transaction()`, retried ≤3 times on `UniqueViolation`
      (revision race), with a duplicate-event_id race check on a second
      connection;
   g. `RETURNING *` row → `_row_to_entry` (`storage/postgres.py:165`) — the
      in-hand object; there is NO public-read-path read-back until Phase 2.

## event_id unique constraint

* `migrations/0001_init.sql:40-41` — `memory_entry_event_id_uq` UNIQUE index on
  `(event_id) WHERE event_id IS NOT NULL` — **global**, not namespace- or
  actor-scoped (changed in Phase 2 / migration 0006).
* Mirrored inline in `tests/conftest.py:48-49` (self-contained test schema).
* No ORM references the constraint by name; the only code that touches the
  collision is the `UniqueViolation` handler in `_append`
  (`storage/postgres.py:378`) and `_seen_event`'s bare `event_id = %s` lookup
  (`storage/postgres.py:317`) — both must change with the constraint.

## memory_list / memory_search query construction

* `memory_list` — `storage/postgres.py:411`: dynamic WHERE from
  `namespace` (+ optional `kind`, `tag = ANY(tags)`), `DISTINCT ON (key) …
  ORDER BY key, revision DESC`, liveness filtered in Python via `_is_live`,
  then `[:limit]`. No prefix filter, no pagination (Phase 4).
* `memory_search` — `storage/postgres.py:464`: three legs (semantic cosine,
  HyDE cosine, keyword ILIKE) each over the `DISTINCT ON (key)` latest-revision
  subquery, fused via `_rrf_fuse` (`storage/postgres.py:198`); pure-keyword
  fallback when embeddings are off.

## artifact_put argument parsing

* `server/mcp_server.py:177-189` — base64 validation (`base64.b64decode(...,
  validate=True)` → `ValueError` with message) and the size-cap check against
  `settings.max_artifact_bytes`, both **in the tool layer**;
  `storage/postgres.py:1003` `artifact_put` then hashes + upserts
  (`ON CONFLICT (sha256) DO NOTHING`, `deduped` flag from RETURNING).

## UNTRUSTED wrapping

* `storage/sanitize.py:16-17` — marker constants.
* Write side: `sanitize`/`scrub_text` (`storage/sanitize.py:25-37`) — strips
  forged delimiters entirely (replaced by a visible one-way escape in Phase 3).
* Read side: `wrap_value` (`storage/sanitize.py:44`), applied in
  `_row_to_entry` (`storage/postgres.py:173`) and `_event_to_dict`
  (`storage/postgres.py:240`).
* Inverse helpers `strip_untrusted`/`unwrap_value` (`storage/sanitize.py:62-82`)
  for consumers that must parse.

## Error-construction sites (pre-Phase-2: no standardized shape)

* `server/mcp_server.py:183,185` — `ValueError` for bad base64 / oversize blob.
* `server/mcp_server.py:42` — `RuntimeError("storage backend not initialized")`.
* `storage/postgres.py:76,389,963` — `raise last_exc` after retry exhaustion
  (raw `psycopg` exceptions surface to FastMCP).
* `storage/postgres.py:948` — `ValueError("session not found in namespace")`.
* Postgres CHECK violations (bad `kind`) surface as raw `pg_errors.CheckViolation`.
* FastMCP converts any raised exception into an `isError: true` tool result with
  the stringified message — Phase 2 (T2.5) standardizes the payload.

## Migration mechanism

* `scripts/migrate.py` — applies `migrations/*.sql` in filename order,
  idempotently, tracked in `schema_migrations(filename)`. Each file runs in one
  transaction (`conn.commit()` per file). Frozen-once-merged convention stated
  in `migrations/0001_init.sql:1`.
* `tests/conftest.py:23-72` mirrors the cumulative schema inline (`SCHEMA`) so
  the suite is self-contained — every migration must be reflected there too.

## Optional injected dependencies (all best-effort, all fail-open by design)

* Embedder — `storage/embeddings.py` (`build_embedder`); disabled ⇒ keyword-only.
* Resolver — `storage/reconcile.py:129` (`build_resolver`); disabled ⇒
  `unverifiable`, never silently `current`.
* Curator — `storage/curator.py:133` (`build_curator`); disabled ⇒ clean no-op.
