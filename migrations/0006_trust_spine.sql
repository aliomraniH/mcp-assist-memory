-- 0006_trust_spine.sql — shared infrastructure for the v2 trust-boundary +
-- ergonomics program (MCP_ASSIST_IMPROVEMENT_PLAN v2).
--
-- Per global ground rule 6, all new columns of Phases 1–6 are bundled into
-- THIS one migration + one schema_version bump, so historical rows need one
-- backfill annotation pass, not five. Everything here is additive with safe
-- defaults; historical revisions are never rewritten (annotate forward only).
-- schema_version after this migration: 6.

-- ---------------------------------------------------------------------------
-- Phase 1 — tool_events: append-only, PHI-safe telemetry.
-- arg_value_meta holds names + sha256/length via storage/redact.py only —
-- never raw values (tests/test_redact.py + tests/test_telemetry.py pin this).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_events (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                  timestamptz NOT NULL DEFAULT now(),
    namespace           text,
    tool                text NOT NULL,
    actor               text,
    session_id          text,
    arg_names           text[] NOT NULL DEFAULT '{}',
    arg_value_meta      jsonb,          -- names + sha256/length via redact()
    variant_profile     jsonb,
    server_version      text,
    schema_version      int,
    outcome             text NOT NULL,  -- ok | dedup_replay | error | quarantined
                                        -- | unknown_arg_accepted | unknown_arg_rejected
    error_code          text,
    remedy_emitted      boolean NOT NULL DEFAULT false,
    advisories          text[],
    advisory_status     text,
    screening_patterns  text[],         -- pattern NAMES only, never matched content
    dedup               boolean,
    verified_persisted  boolean,
    latency_ms          int,
    readback_latency_ms int,
    result_bytes        int,
    truncated           boolean
);
CREATE INDEX IF NOT EXISTS tool_events_ts     ON tool_events (ts);
CREATE INDEX IF NOT EXISTS tool_events_ns_ts  ON tool_events (namespace, ts);

-- ---------------------------------------------------------------------------
-- Phase 7 — variant_profiles (built now: shared spine, used by Workstream E).
-- profile keys: convention_stmt (R1) | advisory_mode (R5) | arg_strictness (R6)
-- | remedy_errors (R9) | claim_staleness_hours (Phase 6) | clinical (PHI gate).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS variant_profiles (
    namespace  text PRIMARY KEY,
    profile    jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now(),
    note       text
);

-- ---------------------------------------------------------------------------
-- Phases 2–6 — memory_entry columns (bundled; all optional / safe defaults).
-- ---------------------------------------------------------------------------
-- Phase 2 (T2.1): actor scopes idempotency; 'unattributed' keeps legacy callers
-- working (dedup still functions within that bucket).
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS actor text NOT NULL DEFAULT 'unattributed';
-- Rule 3: version-stamp every persisted revision.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS server_version text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS schema_version int;
-- Phase 3 (T3.2): quarantine, don't reject. screening = pattern NAMES only.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS quarantined boolean NOT NULL DEFAULT false;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS screening text[];
-- Phase 5 (T5.1/T5.2): provenance tiers + structured model attribution.
-- Backfill default 'unknown' — annotate forward only.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'unknown'
    CHECK (origin IN ('tool','retrieval','synthesized','human','unknown'));
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS origin_detail text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS origin_model_id text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS origin_model_family text;
-- Phase 5 (T5.3): lineage refs, each "key@revision_id".
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS derived_from text[];

-- session_event: actor + optional event_id idempotency (Phase 2, T2.1).
ALTER TABLE session_event ADD COLUMN IF NOT EXISTS actor text NOT NULL DEFAULT 'unattributed';
ALTER TABLE session_event ADD COLUMN IF NOT EXISTS event_id uuid;

-- ---------------------------------------------------------------------------
-- Phase 2 (T2.1): global event_id unique → composite (namespace, actor, event_id).
-- Loosening a constraint: no existing row can violate the new one. No ORM
-- references the old constraint by name (verified in docs/CODEMAP.md).
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS memory_entry_event_id_uq;
CREATE UNIQUE INDEX IF NOT EXISTS memory_entry_ns_actor_event_uq
    ON memory_entry (namespace, actor, event_id) WHERE event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS session_event_ns_actor_event_uq
    ON session_event (namespace, actor, event_id) WHERE event_id IS NOT NULL;

-- Phase 3: quarantine reads exclude by default — keep the filter cheap.
CREATE INDEX IF NOT EXISTS memory_entry_ns_quarantined
    ON memory_entry (namespace) WHERE quarantined;
-- Phase 4 (T4.1): prefix compiles to index-friendly LIKE 'prefix%'.
CREATE INDEX IF NOT EXISTS memory_entry_ns_key_pattern
    ON memory_entry (namespace, key text_pattern_ops);

-- ---------------------------------------------------------------------------
-- Phase 1 — pre-registered metric views, before any data exists. These are
-- deliberately simple approximations: the Phase 10 analysis session joins them
-- with _meta/observations; tool_events is always the denominator.
-- ---------------------------------------------------------------------------

-- Stale-pin rate (R1): saves that drew a stale-pin advisory / all saves, per
-- namespace per day. 'stale_pin' is the advisory NAME emitted by the R5 arm.
CREATE OR REPLACE VIEW v_stale_pin_rate AS
SELECT namespace, date_trunc('day', ts) AS day,
       count(*) FILTER (WHERE tool = 'memory_save') AS saves,
       count(*) FILTER (WHERE tool = 'memory_save'
                          AND advisories IS NOT NULL
                          AND 'stale_pin' = ANY(advisories)) AS stale_pin_saves
FROM tool_events
GROUP BY namespace, date_trunc('day', ts);

-- Advisory heed (R5): for each advisory-carrying save, was there a corrective
-- re-save (another memory_save, same namespace) within the next 3 events from
-- that namespace? Key-level matching happens in the analysis session (args are
-- hashed here); this view gives the coarse numerator/denominator.
CREATE OR REPLACE VIEW v_advisory_heed AS
WITH numbered AS (
    SELECT id, ts, namespace, tool, advisories,
           row_number() OVER (PARTITION BY namespace ORDER BY id) AS rn
    FROM tool_events
)
SELECT a.namespace, a.id AS advisory_event_id, a.ts,
       EXISTS (
           SELECT 1 FROM numbered b
           WHERE b.namespace = a.namespace
             AND b.rn > a.rn AND b.rn <= a.rn + 3
             AND b.tool = 'memory_save'
       ) AS followed_by_resave
FROM numbered a
WHERE a.advisories IS NOT NULL AND array_length(a.advisories, 1) > 0;

-- One-turn recovery (R6): an unknown-arg rejection followed IMMEDIATELY (next
-- event in that namespace) by an ok call of the same tool.
CREATE OR REPLACE VIEW v_one_turn_recovery AS
WITH numbered AS (
    SELECT id, namespace, tool, outcome,
           row_number() OVER (PARTITION BY namespace ORDER BY id) AS rn
    FROM tool_events
)
SELECT r.namespace, r.id AS rejection_event_id, r.tool,
       EXISTS (
           SELECT 1 FROM numbered n
           WHERE n.namespace = r.namespace AND n.rn = r.rn + 1
             AND n.tool = r.tool AND n.outcome IN ('ok','dedup_replay','quarantined')
       ) AS recovered_in_one_turn
FROM numbered r
WHERE r.outcome = 'unknown_arg_rejected';

-- Error recovery (R9): any error followed within 3 events (same namespace) by
-- an ok call of the same tool, split by whether a remedy was emitted.
CREATE OR REPLACE VIEW v_error_recovery AS
WITH numbered AS (
    SELECT id, namespace, tool, outcome, remedy_emitted,
           row_number() OVER (PARTITION BY namespace ORDER BY id) AS rn
    FROM tool_events
)
SELECT e.namespace, e.id AS error_event_id, e.tool, e.remedy_emitted,
       EXISTS (
           SELECT 1 FROM numbered n
           WHERE n.namespace = e.namespace
             AND n.rn > e.rn AND n.rn <= e.rn + 3
             AND n.tool = e.tool AND n.outcome IN ('ok','dedup_replay','quarantined')
       ) AS recovered
FROM numbered e
WHERE e.outcome = 'error';

-- List/read result sizes: context-budget pressure per tool.
CREATE OR REPLACE VIEW v_list_result_sizes AS
SELECT tool, namespace, date_trunc('day', ts) AS day,
       count(*) AS calls,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY result_bytes) AS p50_bytes,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY result_bytes) AS p95_bytes,
       count(*) FILTER (WHERE truncated) AS truncated_calls
FROM tool_events
WHERE tool IN ('memory_list','memory_search','memory_history','handoff_list',
               'session_events','session_list','artifact_list')
GROUP BY tool, namespace, date_trunc('day', ts);

-- Screening hit rate (Phase 3 / Phase 10 FP threshold): quarantines vs saves
-- vs override-clears per namespace per day. FP fraction over a window =
-- override_clears / quarantines.
CREATE OR REPLACE VIEW v_screening_hit_rate AS
SELECT namespace, date_trunc('day', ts) AS day,
       count(*) FILTER (WHERE tool IN ('memory_save','handoff_save')) AS saves,
       count(*) FILTER (WHERE outcome = 'quarantined') AS quarantines,
       count(*) FILTER (WHERE advisories IS NOT NULL
                          AND 'screening_override' = ANY(advisories)) AS override_clears
FROM tool_events
GROUP BY namespace, date_trunc('day', ts);

-- Quarantine review lag: time from a quarantined write to the next
-- screening_override event in the same namespace.
CREATE OR REPLACE VIEW v_quarantine_review_lag AS
SELECT q.namespace, q.id AS quarantine_event_id, q.ts AS quarantined_at,
       (SELECT min(o.ts) FROM tool_events o
        WHERE o.namespace = q.namespace AND o.ts > q.ts
          AND o.advisories IS NOT NULL
          AND 'screening_override' = ANY(o.advisories)) AS cleared_at
FROM tool_events q
WHERE q.outcome = 'quarantined';
