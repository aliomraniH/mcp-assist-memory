-- 0007_v3_phase1.sql — Enhancement Plan v3, Phases 0-1. Additive only:
-- nullable columns, no backfill, old acks unchanged under the default
-- variant_profile.

-- Item 4: idempotency fingerprint. sha256 over the JCS (RFC 8785)
-- canonicalization of (tool, namespace, key, kind, payload, meta), computed at
-- the API boundary (never post-jsonb). Stored with event_id writes so a
-- replayed key with a DIFFERENT payload is answerable with
-- idempotency_conflict instead of a phantom ack.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS idem_fingerprint text;

-- Item 5: temporal mode. What kind of time-binding a claim has:
--   head_tracking       — asserts about the CURRENT state of a moving ref;
--                         goes stale when the head moves (the classic claim).
--   historical_snapshot — asserts about a specific commit as of a moment;
--                         reconciles by sha-exists + evidence, NEVER compared
--                         to the live head (terminal non-stale once verified).
--   interval            — bounded validity window (reconciliation of this mode
--                         is not mechanized in Phases 0-1; verdicts stay
--                         unverifiable rather than guessed).
--   timeless            — no external mutable subject at all.
-- Nullable: absent means the reconciler INFERS a mode and marks the verdict
-- temporal_mode_origin:"inferred" (advisory, never authoritative).
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS temporal_mode text;
