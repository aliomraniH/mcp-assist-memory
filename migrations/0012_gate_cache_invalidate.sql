-- 0012_gate_cache_invalidate.sql — Intent Gate remediation, Phase 3a.
-- Additive: one column, one function, one trigger. No data change.
--
-- Tier-0 pre-flight costs approximately one Neon round trip, and the measured
-- p95 (64ms) is statistically identical to the readback latency on the same
-- acks. The gate is not slow — it is paying for a network hop to read values
-- that change perhaps twice a month. Caching them in-process removes the hop.
--
-- A cache over GATE inputs is only acceptable if invalidation is real. The gate
-- exists to stop stale beliefs from being acted on; a gate serving its own
-- stale configuration would be exactly the failure it is meant to prevent. So
-- the version bump and the notification happen in the SAME transaction as the
-- profile change — there is no window in which a committed profile edit has not
-- been announced.
--
-- NEON TOPOLOGY: pg_notify() inside a transaction works fine on the POOLED
-- endpoint (it is just a statement). LISTEN does not — PgBouncer in transaction
-- mode drops session-level features. The subscriber therefore needs one
-- dedicated DIRECT (non `-pooler`) connection, which is why DATABASE_URL_DIRECT
-- exists as a separate secret. See docs/runbooks/neon-credential-rotation.md.
--
-- The listener is an OPTIMISATION, never a correctness dependency: every cache
-- entry also carries a TTL, so a dead listener degrades the system to slow, not
-- to wrong.

-- Monotonic version. Notifications carrying a version <= the cache's current
-- one are ignored, so a replayed or out-of-order NOTIFY cannot move a cache
-- backwards into a generation it has already left.
ALTER TABLE variant_profiles ADD COLUMN IF NOT EXISTS cache_version bigint NOT NULL DEFAULT 0;

CREATE SEQUENCE IF NOT EXISTS gate_cache_version_seq;

CREATE OR REPLACE FUNCTION gate_notify_invalidate() RETURNS trigger AS $$
DECLARE
    v bigint;
BEGIN
    v := nextval('gate_cache_version_seq');
    NEW.cache_version := v;
    -- Payload is "<version>:<namespace>". The namespace lets a subscriber drop
    -- one entry instead of the whole cache; the version is what makes a stale
    -- notification detectable.
    PERFORM pg_notify('gate_invalidate', v::text || ':' || NEW.namespace);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS variant_profiles_gate_invalidate ON variant_profiles;
CREATE TRIGGER variant_profiles_gate_invalidate
    BEFORE INSERT OR UPDATE ON variant_profiles
    FOR EACH ROW EXECUTE FUNCTION gate_notify_invalidate();
