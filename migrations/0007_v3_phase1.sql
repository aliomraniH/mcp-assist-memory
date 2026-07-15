-- 0007_v3_phase1.sql — Enhancement Plan v3, Phases 0-1. Additive only:
-- nullable columns, no backfill, old acks unchanged under the default
-- variant_profile.

-- Item 4: idempotency fingerprint. sha256 over the JCS (RFC 8785)
-- canonicalization of (tool, namespace, key, kind, payload, meta), computed at
-- the API boundary (never post-jsonb). Stored with event_id writes so a
-- replayed key with a DIFFERENT payload is answerable with
-- idempotency_conflict instead of a phantom ack.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS idem_fingerprint text;
