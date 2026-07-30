# Phase −1 — Codebase orientation (recorded 2026-07-30)

Branch base: main @ 4bd1fc1e666ffe9fa337b075b2986d665832fd57 (full 40-char).
Designated work branch (session harness): claude/mcp-assist-intent-gate-1pwuit
  DELTA vs charter: charter says feat/intent-gate-p1; the Claude Code session's
  designated branch is claude/mcp-assist-intent-gate-1pwuit and pushing elsewhere
  is forbidden by the harness. Recorded; PR will cite both.

## a. Server entry + tool registration
- app.py:111-118 — FastAPI + mcp.http_app (stateless HTTP, json_response).
- server/mcp_server.py:145-154 — FastMCP instance; 23 tools; tool descriptions are
  the docstrings of @mcp.tool functions (e.g. memory_save at :190-288).
- instrument() wrapper server/mcp_server.py:72-142 — telemetry + version stamp +
  AppError → ToolError(json payload) with isError semantics.
- _TOOL_PARAMS registry :649-659 (must add new tools); ArgStrictnessMiddleware :662.
- registered_tool_names() :786-799 (smoke source of truth; docs say "23 tools").

## b. storage/postgres.py
- _append: postgres.py:574-740 — the single write path (memory_save, delete
  tombstones, handoff_save, curation ops). Screening at :619-625, quarantine
  persist; boundary meta/sha canonicalization via _boundary_meta :498-572;
  fingerprint at :603-609 (JCS at boundary, never post-jsonb); read-back verify
  :713-728; _finalize_ack :414-442 (status/summary layering).
- _split_meta: postgres.py:160-171 (projection of _META_COLS :114).
- coord_health: postgres.py:1096-1303 (stale via sha_equivalent :1141-1145;
  needs_reverification :1216-1251 — claim_staleness_hours from profile or
  settings.default_claim_staleness_hours).
- apply_curation: postgres.py:1538-1620 (PHI gate per-op :1563-1565, provenance
  downgrade :1573-1577, deterministic event_id _curate_event_id :176-180).
- Error taxonomy: errors/catalog.py — AppError {code, message, remedy, retryable}
  (RFC-9457-shaped in-band payload); catalog CATALOG :23-109; FEEDBACK_NUDGE :115.
- Ack envelope: _row_to_entry :219-274 (34 fields), compact ack
  server/mcp_server.py:162-186 (_COMPACT_ACK_CORE 11 + escalations; profile
  compact_acks on/off; verbose:true expands). Compact-acks baseline = 14 fields.

## c. storage/reconcile.py
- sha_match / MIN_ABBREV_LEN re-exported :28-29 from storage/sha_equiv.
- reconcile_claim :167-222 (evidence gate, temporal forks via _subject_verdict
  :225-286). Resolver protocol :37-46; GitHubResolver :64-144 (branch_head,
  merged_state, commit_sha); build_resolver :147-164 (PAT → connector → disabled);
  GitHub token path storage/github_token.py (connector provider).

## d. R5 hook + variant_profiles
- _stale_pin_advisory: postgres.py:742-775 — asyncio.timeout(2) budget,
  advisory_status computed|ok|skipped_timeout|skipped_unresolved, never blocks.
  Wired in memory_save :789-804 gated by profile advisory_mode. THIS is the
  template for GitHub awakening.
- variant_profiles: storage/profiles.py (DEFAULT_PROFILE :31-37, resolve_profile
  :48-60; pass-through keys claim_staleness_hours + clinical). Table
  variant_profiles (0006). Cache TTL 60s (postgres.py:475-491).

## e. Curator + SDK + PHI gate
- storage/curator.py — AnthropicCurator.curate :133-159 (lazy SDK import, direct
  API messages.create, fail-closed _empty(status,error)); build_curator :162-173
  (anthropic_api_key gate). THIS is the Tier-2 call path to copy.
- storage/phi.py — assert_no_phi :99-110 deterministic fail-closed gate.
- Clinical profile: profiles pass-through `clinical`; origin_detail suppressed
  (postgres.py:633-637); observation_log disabled (:1923-1927); attestation raw
  fields rejected (:546-554).

## f. Test suite
- tests/conftest.py — REAL Postgres via DATABASE_URL; inline SCHEMA mirror
  (currently mirrors through 0007 — 0008's tool_events.source_surface is NOT
  mirrored: fresh-schema runs swallow telemetry inserts; migrated DB is the
  correct baseline). FakeEmbedder :142-166, FakeResolver :183-207,
  FakeCurator :225-237, ns fixture proj-test-<rand>.
- Run: `pytest -q` with DATABASE_URL, MCP_AUTH_TOKEN=seed-token-xyz,
  ADMIN_PASSWORD=test-admin-pw (dashboard/surface tests import-order artifact:
  without ADMIN_PASSWORD exported up front they fail; with it, green).
- BASELINE (main @ 4bd1fc1, migrated local PG16+pgvector): 332 passed, 0 failed.

## g. Migrations
- migrations/000N_*.sql applied by scripts/migrate.py in filename order against
  empty DB; pattern = additive ALTER TABLE ... ADD COLUMN IF NOT EXISTS +
  nullable; comment headers explain semantics (see 0007). Next: 0009.
- NOTE: migrate.py 0001 is not idempotent against a conftest-seeded dirty DB
  (memory_entry_event_id_uq collision) — migrate from empty, as prod did.

## Deltas from the package docs
1. Seed writes NOT run: dev/mcp-assist-memory has no project/meta,
   charter/intent-gate-v1, or baton/intent-gate-p1 (memory_list verified
   2026-07-30). Proceeding from the uploaded package (zip). Operator should run
   SEED_WRITES_intent-gate.md; MD-1 dogfooding vs dev/mcp-assist-memory will
   return project:null until then (correct per MD-1: never fabricate).
2. claude/intent-gate/ not in repo — package arrived as uploaded zip; will be
   committed as docs-only housekeeping first commit.
3. Branch name (see header).
4. Session (episodic, surface claude_code): 9a89df80-3d3e-45c1-98fe-a8105a591602.
