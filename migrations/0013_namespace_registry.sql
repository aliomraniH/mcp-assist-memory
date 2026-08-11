-- 0013_namespace_registry.sql — the namespace registry behind `namespace_init`.
-- Additive only: one new table. Nothing existing reads it, so applying this
-- migration changes no current behaviour.
--
-- ============================================================================
-- WHY A REGISTRY
--
-- Until now a namespace came into existence as a SIDE EFFECT: the first write
-- created rows carrying a namespace string, and that was the whole ceremony.
-- Three consequences, all of which have bitten this project:
--
--   1. Nobody decided the namespace's policy. Its intent_gate arming, its
--      clinical flag, and its retrieval floor were whatever the server defaults
--      happened to be that week. At read time a defaulted setting is
--      indistinguishable from a chosen one, so two namespaces created a release
--      apart can differ in ways no response ever mentions.
--
--   2. There is no list of namespaces. `memory_list` needs one to be named;
--      discovering what exists means scanning memory_entry, which conflates
--      "a namespace" with "somewhere a row was once written" — including
--      typos, one-shot probes, and abandoned experiments.
--
--   3. There is no record of WHO created one or WHY. A probe namespace and a
--      production namespace look identical.
--
-- The registry is the record of the decision. `namespace_init` writes it; the
-- authoritative policy still lives in variant_profiles (the gate reads that,
-- and a registry that could drift from the enforced profile would be worse than
-- none). What this table adds is provenance and enumerability: who created it,
-- when, under what intent, and with which profile at creation time.
--
-- created_profile is a SNAPSHOT, deliberately not kept in sync with
-- variant_profiles. It answers "what was chosen at creation", which is a
-- different and permanently useful question from "what is in force now".
CREATE TABLE IF NOT EXISTS namespace_registry (
    namespace       text PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL DEFAULT 'unattributed',
    purpose         text,
    clinical        boolean NOT NULL DEFAULT false,
    created_profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Free-form, low-cardinality label for filtering ('probe', 'production',
    -- 'verification'). Not a closed enum: unlike a telemetry dimension this is
    -- read by humans, and forcing an enum here would just push people to lie.
    lifecycle       text
);

-- PHI: no goal text, no entry content, no free text beyond `purpose`, which is
-- caller-supplied and screened by the same write path as any other value. In a
-- clinical namespace, callers must keep identifiers out of `purpose` exactly as
-- they must keep them out of an intent goal.
CREATE INDEX IF NOT EXISTS namespace_registry_created_at
    ON namespace_registry (created_at DESC);
CREATE INDEX IF NOT EXISTS namespace_registry_clinical
    ON namespace_registry (clinical) WHERE clinical;
