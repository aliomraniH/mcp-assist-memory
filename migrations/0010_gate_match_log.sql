-- 0010_gate_match_log.sql — Intent Gate v1 remediation, Phase 1 (A2 floor +
-- relative guard, and the calibration dataset that a future adaptive cutoff
-- will need). Additive only: one new table, no column changes, no backfill.
--
-- WHY THIS TABLE EXISTS
--   The v1 gate had SIMILARITY_FLOOR = 0.25 and escalated an anti-pattern skill
--   to gate_conflict on embedding proximity alone. The independent validation
--   run measured true matches at cosine 0.504-0.609 against a noise ceiling of
--   0.392, and demonstrated twice that proximity cannot decide violation:
--   "schedule the quarterly workshop catering" conflicted against an event-log
--   fold-order skill at 0.288, and an intent that OBEYED that skill conflicted
--   against it too. Compliance and violation are adjacent in embedding space,
--   so no cosine threshold separates them.
--
--   The remedy splits the two jobs. The floor + relative guard controls what is
--   RETRIEVED (a display/precision concern). A structured predicate controls
--   what ESCALATES. This table records the retrieval side of every decision so
--   the floor stops being a guess.
--
-- EVERY match is logged, escalated or not. That is the point: a table holding
-- only escalations cannot tell you what the floor rejected, so it can never
-- calibrate the floor that produced it. This is the dataset an adaptive or
-- conformal cutoff needs, and that upgrade stays research-grade until roughly
-- 1,000 matched-outcome observations exist. Do not ship an adaptive floor
-- before then.
--
-- PHI HARD GATE (charter §7). intent_hash ONLY — never raw goal text. The
-- existing gate_intent table already enforces `goal IS NULL` in clinical
-- namespaces; this table meets the same standard by construction, having no
-- goal column at all to leak into. Feature labels are extracted lemmas, not
-- free text, and are omitted entirely for clinical namespaces.
CREATE TABLE IF NOT EXISTS gate_match_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    namespace text NOT NULL,
    -- sha256 of the declared goal. The only intent identifier stored here.
    intent_hash char(64) NOT NULL,
    skill_key text NOT NULL,
    -- Retrieval signals. cosine is NULL for a candidate that arrived via the
    -- deterministic trigger-overlap leg rather than the embedding leg.
    cosine real,
    top_score real,
    absolute_floor real,
    alpha real,
    passed_guard boolean NOT NULL,
    -- Escalation signals. predicate_match is the ONLY one of these that can
    -- cause a gate_conflict. nli_contradiction stays NULL on this branch:
    -- Phase 4 is descoped, and the column exists so enabling it later is
    -- additive rather than another migration on the hot path.
    predicate_match boolean,
    nli_contradiction real,
    -- Backfilled at outcome closure (Phase 2's gate_close_outcome), never at
    -- match time. NULL means "not yet closed", not "not acted upon".
    acted_upon boolean,
    -- Freshness discipline: a stored signal that cannot go stale becomes a
    -- stale authority. calibration_ts records when the floor/alpha in this row
    -- were last calibrated; a read past the calibration window is surfaced as
    -- unverified rather than silently trusted.
    temporal_mode text,
    calibration_ts timestamptz
);
CREATE INDEX IF NOT EXISTS gate_match_log_ns_ts
    ON gate_match_log (namespace, ts);
CREATE INDEX IF NOT EXISTS gate_match_log_ns_skill
    ON gate_match_log (namespace, skill_key, ts);
-- Closure backfill looks up by (namespace, intent_hash).
CREATE INDEX IF NOT EXISTS gate_match_log_ns_intent
    ON gate_match_log (namespace, intent_hash);
