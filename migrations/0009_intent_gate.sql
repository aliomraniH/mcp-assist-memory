-- 0009_intent_gate.sql — Intent Gate v1 (charter: claude/intent-gate/
-- INTENT_GATE_CHARTER.md). Additive only: new tables + nullable columns, no
-- backfill; old acks are byte-identical under the default variant_profile
-- (intent_gate defaults to "off"; the operator flips namespaces on).

-- Two-phase preview/confirm for supersession + delete (Tier 0, G0-1/G0-5).
-- A preview stores the exact args fingerprint (JCS at the boundary, control
-- keys stripped) so the confirm call must round-trip the identical operation;
-- tokens are single-use (consumed_at) and expire.
CREATE TABLE IF NOT EXISTS gate_pending (
    token uuid PRIMARY KEY,
    namespace text NOT NULL,
    tool text NOT NULL,
    key text NOT NULL,
    args_fingerprint text NOT NULL,
    preview jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz
);
CREATE INDEX IF NOT EXISTS gate_pending_ns_created
    ON gate_pending (namespace, created_at);

-- Per-session intent registry (Tier 1). PHI hard gate (charter §7): in
-- clinical-profile namespaces `goal` is ALWAYS NULL — only intent_hash +
-- screened labels persist; verbatim intent text is permitted in dev profiles
-- only. `decision`/`conflict`/`matched` snapshot the Tier-1 verdict so later
-- mutating calls in the session can consult it without re-deriving.
CREATE TABLE IF NOT EXISTS gate_intent (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace text NOT NULL,
    session_id text NOT NULL,
    intent_hash char(64) NOT NULL,
    goal text,
    scope text[] NOT NULL DEFAULT '{}',
    labels text[] NOT NULL DEFAULT '{}',
    screening text[],
    decision text,
    conflict jsonb,
    matched jsonb,
    embedding vector(1024),
    actor text NOT NULL DEFAULT 'unattributed',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gate_intent_ns_session
    ON gate_intent (namespace, session_id, id);

-- Outcome-closure ledger (charter §4b): every gate_blocked/gate_conflict on a
-- write records its fingerprint; a later identical attempt that succeeds
-- (operator override) closes as false_positive, one that genuinely fails
-- closes as confirmed_correct — the measured false-positive tax that governs
-- Tier-2 thresholds. Rollups live in memory under gate/efficacy/<yyyymm>.
CREATE TABLE IF NOT EXISTS gate_block_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace text NOT NULL,
    fingerprint text NOT NULL,
    rule text NOT NULL,
    skill_key text,
    closed text,
    created_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz
);
CREATE INDEX IF NOT EXISTS gate_block_ns_fp
    ON gate_block_log (namespace, fingerprint) WHERE closed IS NULL;

-- Telemetry: every tool_events row can carry the gate verdict (nullable —
-- ungated calls stay NULL). GH-1/GH-4 assert resolver awakening on the
-- tool='gate_awaken' rows; G2-1 asserts trigger discipline on gate_tier.
ALTER TABLE tool_events ADD COLUMN IF NOT EXISTS gate_tier int;
ALTER TABLE tool_events ADD COLUMN IF NOT EXISTS gate_decision text;
