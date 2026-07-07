# Replit Agent prompt — sync + test the trust-boundary follow-up fixes

Copy everything below the line into the Replit agent. It is self-contained:
context, hard requirements, step-by-step sync, the test gate, an optional
redeploy, and rollback. The fixes are already **merged to `main`** (tip
`8a4a359`), so the agent syncs from `main` — no branch juggling.

---

## Your task

Sync this Repl's `mcp-assist-memory` checkout to the latest **`main`** (tip
`8a4a359`), install the pinned dependencies, and **run the full test suite to a
clean pass**. Then (optional, only if asked to ship it) redeploy the Reserved VM
at `mcp-assist-memory.replit.app` and run the post-deploy smoke check. Do not
mark the task done until `pytest` is green.

## Context — what changed since the last deploy

These are four **code-only** follow-up fixes on top of the trust-boundary v2
program. There is **no new migration and no schema change** — `schema_version`
stays `6`, `server_version` stays `0.2.0`, and the MCP surface stays **23
tools**. Nothing in `migrations/` was touched. The changes:

1. **`memory_get` now honors the default quarantine exclusion.** It gained an
   `include_quarantined` parameter (default `false`); a quarantined latest
   revision now returns `null` unless you opt in — matching `memory_list`,
   `memory_search`, and `handoff_load`. `handoff_load` delegates to `memory_get`
   so the two exact-key reads can't drift apart. *(storage/postgres.py,
   server/mcp_server.py)*
2. **`coord_health` keeps `unverifiable` claims visible.** A claim whose latest
   reconcile verdict is `unverifiable` now stays in `needs_reverification`
   (reason `"unverifiable"`) regardless of the staleness window, instead of
   reading as "handled" for 72h. A fresh `current` verdict is still not flagged.
   *(storage/postgres.py)*
3. **`memory_search` excludes the internal house-band.** `coord/_reconcile/*`
   verdict records and `_meta/*` bookkeeping no longer appear in ranked search
   results (they could outrank a user's own memories); they stay readable via
   `memory_list` (with the `coord/_reconcile` prefix), `memory_get`, and
   `memory_history`. Ordinary `coord/*` keys still search. *(storage/postgres.py,
   server/mcp_server.py)*
4. **`coord_curate` reports an outcome status.** Every result now carries
   `curator_status ∈ ok|error|disabled` so an empty `operations` list is
   unambiguous: `ok` = a deliberate NOOP, `error` = a fail-closed model failure
   (with a structural `curator_error` — `sdk_unavailable`, the exception class
   name, or `unparseable_response`; never model prose or secrets). The write
   path stays fail-closed in every case. *(storage/curator.py,
   storage/postgres.py, server/mcp_server.py)*

All four are additive to response shapes and behavior-preserving for every path
that already worked. Full suite is **259 tests, green** at `8a4a359`.

## Hard requirements — do NOT violate these

1. **Never edit files under `migrations/`.** There is no migration in this
   change; do not add or "regenerate" one. Schema stays at version 6.
2. **Use the pinned dependency closure.** Install with `-c constraints.txt` on
   every path (it is what CI and the deploy build use). Do not bump
   `pyproject.toml` or hand-edit `constraints.txt`; if a resolution error
   appears, stop and report it rather than loosening a pin.
3. **Run the test suite against a THROWAWAY database, never the production
   `DATABASE_URL`.** The suite writes into disposable `proj-test-<rand>`
   namespaces and does not clean up; pointing it at the live Neon endpoint would
   pollute the store. Use a fresh Neon branch (the CI pattern) or a local
   Postgres — either way it must have the `vector` (pgvector) and `pg_trgm`
   extensions, or the schema in `tests/conftest.py` fails to create.
4. **The suite needs three env vars** set for the test process only:
   `DATABASE_URL` (the throwaway DB), `MCP_AUTH_TOKEN` (any non-empty seed
   value), and `ADMIN_PASSWORD` (any non-empty value — several dashboard tests
   go red without it). Do not commit these anywhere.
5. **Do not "optimize away" the read-back verification or the new house-band
   filters** if you touch the code — they are the point of the fixes.

## Steps

1. **Sync to `main`.** From the repo root:
   ```bash
   git fetch origin main
   git checkout main
   git reset --hard origin/main   # discard any local Repl drift; main is source of truth
   git log --oneline -1           # expect: 8a4a359 (curator_status fix)
   ```
   If the working tree has local changes you need to keep, stash them first and
   report the conflict instead of clobbering.

2. **Install the pinned deps** (test extras included):
   ```bash
   pip install -c constraints.txt -e ".[test]"
   ```

3. **Provision a throwaway database with the required extensions.** Prefer a
   fresh Neon branch (has pgvector). If you use a local Postgres, ensure
   `CREATE EXTENSION vector;` and `CREATE EXTENSION pg_trgm;` succeed on it.
   Export its URL as `TEST_DATABASE_URL`.

4. **Run the full suite** (env scoped to this command only):
   ```bash
   DATABASE_URL="$TEST_DATABASE_URL" \
   MCP_AUTH_TOKEN="ci-seed-token" \
   ADMIN_PASSWORD="ci-admin-pw" \
   pytest -q
   ```
   Expect **all tests passing** (259 at `8a4a359`; the one warning about
   `starlette.testclient`/`httpx` is benign). If anything fails, capture the
   failing test names and output and report them — do not edit tests to make
   them pass.

5. **(Optional) Redeploy the Reserved VM** only if the task is to ship, not just
   verify. The `.replit` build/run are already correct and unchanged; because
   there is no migration, this is a plain code redeploy. After it is live, run
   the post-deploy smoke check against the deployed URL:
   ```bash
   SMOKE_BASE_URL="https://mcp-assist-memory.replit.app" \
   SMOKE_TOKEN="<an active token from /admin>" \
   python scripts/smoke_mcp.py
   ```
   Expect `smoke: PASS` with `tools/list: 200 (23 tools)`.

## Post-sync verification (what "done" looks like)

- `git log --oneline -1` shows `8a4a359`.
- `pytest -q` is green against the throwaway DB. In particular these
  fix-specific tests pass:
  - `tests/test_get_quarantine.py` — memory_get quarantine parity (Fix 1)
  - `tests/test_trust_decay.py::test_unverifiable_verdict_stays_flagged_but_current_does_not` (Fix 2)
  - `tests/test_semantic_search.py::test_search_excludes_reconcile_house_band` and the two sibling exclusion tests (Fix 3)
  - `tests/test_curator_status.py` and `tests/test_curator.py::test_curator_status_distinguishes_noop_from_error` (Fix 4)
- If you redeployed: `scripts/smoke_mcp.py` prints `smoke: PASS`, `/healthz` is
  `200 db=ok`, and the bearer gate 401s without a token.

## Rollback

There is no schema change, so rollback is a pure code revert:
```bash
git checkout main
git reset --hard e0c709c   # the previous main tip ("Published your App")
```
Then redeploy if you had shipped. No migration to undo.
