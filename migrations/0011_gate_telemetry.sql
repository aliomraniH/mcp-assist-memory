-- 0011_gate_telemetry.sql — Intent Gate v1 remediation, Phase 2. Additive only:
-- nullable columns on tool_events, one new append-only table, one marker row.
--
-- ============================================================================
-- 2a. WHY BLOCKS WERE INVISIBLE
--
-- The validation run established by controlled experiment that four blocks
-- occurred, the gate/efficacy rollup captured two, and tool_events captured
-- zero. The diagnosis "a blocked call raises before its row is written" is only
-- half right, and the other half is what makes this a schema change:
--
--   The tool layer ALREADY emits from a finally-path, so a row IS written for a
--   blocked call. But the gate verdict is read off the tool's RESULT, and on an
--   error path the result is None — so gate_tier and gate_decision land NULL.
--   The row exists and is invisible to exactly the query anyone would write:
--       SELECT gate_decision, count(*) FROM tool_events GROUP BY 1
--   Every v_* analytics view built on that column therefore concludes the gate
--   never blocks anything.
--
-- The fix is to carry the verdict on the EXCEPTION as well as the result, and
-- to record WHY the block happened in a fixed low-cardinality dimension.
--
-- error_type is a closed enum, not free text. Raw exception messages must never
-- become a dimension: they are unbounded, they drift with every reword, and
-- they are the most likely place for user content to leak into telemetry.
ALTER TABLE tool_events ADD COLUMN IF NOT EXISTS error_type text;
DO $$
BEGIN
    ALTER TABLE tool_events ADD CONSTRAINT tool_events_error_type_chk
        CHECK (error_type IS NULL OR error_type IN (
            'confirm_mismatch', 'unresolved_conflict_destructive',
            'idempotency_conflict', 'intent_mismatch', 'quarantined', 'internal'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- The gate rule that fired, as named by the gate itself (never inferred prose).
ALTER TABLE tool_events ADD COLUMN IF NOT EXISTS gate_rule text;

-- Marks rows at/after the telemetry migration boundary. See the marker row at
-- the bottom of this file for why this exists instead of a backfill.
ALTER TABLE tool_events ADD COLUMN IF NOT EXISTS discontinuity boolean;

-- Idempotent emission. The emitter derives a deterministic id per (call, phase)
-- so a retried or double-invoked emission collapses instead of double-counting
-- a block — the failure mode that would make the new numbers as untrustworthy
-- as the old ones.
ALTER TABLE tool_events ADD COLUMN IF NOT EXISTS emit_event_id text;
CREATE UNIQUE INDEX IF NOT EXISTS tool_events_emit_event_id_uq
    ON tool_events (emit_event_id) WHERE emit_event_id IS NOT NULL;

-- ============================================================================
-- 2b. EVENT-SOURCED SKILL EFFICACY
--
-- The v1 counters were mutable integers on the skill's own meta, incremented by
-- actor 'gate' on every match. skill/no-sorted-fold-replay ran applied 0 -> 5,
-- every increment written by the gate, INCLUDING one caused by the catering
-- false positive. The instrument shared identity with the subject, so the
-- metric meant to decide whether a skill earns its keep was inflated by that
-- skill's own false positives — and could not be used to tune the threshold
-- that produced them.
--
-- Counters become PROJECTIONS over this append-only log. Four monotonic stages:
--
--   matched        the candidate cleared the retrieval guard. DIAGNOSTIC ONLY —
--                  never tunes a threshold, because it is produced by the very
--                  mechanism under measurement.
--   surfaced       the candidate was returned to the caller.
--   acted_upon     session linkage fired (see the bias note below).
--   outcome_closed a human/agent recorded what actually happened. The ONLY
--                  stage thresholds may tune on.
--
-- Each stage has a DISTINCT writer actor (gate-eval / gate-linkage /
-- gate-closure) and the wrong actor for a stage is rejected. That is not
-- ceremony: event dedup on this server is scoped to (namespace, actor), so
-- sharing an actor between stages would let one stage's dedup silently swallow
-- another's events — the same class of bug as the instrument sharing identity
-- with its subject.
--
-- UNIQUE(namespace, skill_key, intent_hash, stage) makes double-counting
-- structurally impossible rather than conventionally avoided.
CREATE TABLE IF NOT EXISTS skill_efficacy_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    namespace text NOT NULL,
    skill_key text NOT NULL,
    -- PHI: the intent is identified by hash. No goal column, by construction.
    intent_hash char(64) NOT NULL,
    stage text NOT NULL CHECK (stage IN
        ('matched', 'surfaced', 'acted_upon', 'outcome_closed')),
    outcome text CHECK (outcome IS NULL OR outcome IN
        ('followed', 'overridden', 'abandoned')),
    writer_actor text NOT NULL,
    event_id text,
    session_id text,
    UNIQUE (namespace, skill_key, intent_hash, stage)
);
CREATE INDEX IF NOT EXISTS skill_efficacy_events_ns_skill
    ON skill_efficacy_events (namespace, skill_key, stage);
CREATE INDEX IF NOT EXISTS skill_efficacy_events_ns_intent
    ON skill_efficacy_events (namespace, intent_hash);

-- ============================================================================
-- THE DISCONTINUITY MARKER — why there is no backfill.
--
-- Both telemetry surfaces undercounted blocks, in different amounts, in the
-- same direction, and neither undercount is quantified. Any reconstruction of
-- the missing rows would be a guess dressed as history, and durable memory is
-- an amplifier: a fabricated historical count would be read as measurement by
-- every future session and would never be re-derived.
--
-- So the pre-migration data is not repaired. It is FENCED. This row is the
-- fence, and it is a first-class record rather than a comment in a file nobody
-- reads: analytics can find it, and the false-positive rate published before it
-- must be treated as unusable rather than merely old.
INSERT INTO tool_events (tool, outcome, discontinuity, error_type, emit_event_id)
SELECT '_telemetry_discontinuity', 'ok', true, NULL,
       'discontinuity:0011_gate_telemetry'
WHERE NOT EXISTS (
    SELECT 1 FROM tool_events WHERE emit_event_id = 'discontinuity:0011_gate_telemetry');
