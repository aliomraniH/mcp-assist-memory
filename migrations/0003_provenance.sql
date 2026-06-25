-- ---------------------------------------------------------------------------
-- Coordination provenance — make "what was this true at?" a first-class,
-- queryable property of a memory entry instead of free text buried in the JSON.
--
-- Today SHAs / session ids live as prose inside `value`, so no reader can
-- mechanically ask "is this still current?" — which is the root of the manual
-- sync tax (stale verifies, status contradictions, namespace drift). These
-- columns are the version vector's optional, git-derived dimensions; the
-- always-present dimensions (server revision, created_at) already exist.
--
-- Every column is NULLABLE on purpose. A write that supplies no `meta` behaves
-- exactly as before, so existing rows and surfaces that can't compute a SHA
-- (web / Cursor / other IDEs) keep working unchanged. This mirrors the
-- best-effort, degrades-cleanly contract the embedding column established.
-- ---------------------------------------------------------------------------

-- Git/session provenance, projected out of the `meta` envelope for indexed reads.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS repo_sha   text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS base_sha   text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS branch     text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS dirty      boolean;
-- session_id is text, not uuid: surfaces other than Claude (Cursor, web, other
-- IDEs) may use non-UUID session identifiers, and a bad cast must never fail a write.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS session_id text;
-- Full coordination envelope as written by the caller/hook. The columns above
-- are the queryable projection; this keeps anything extra (surface, pr,
-- merge_sha, …) losslessly.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS meta       jsonb;

-- Widen the kind enum: `claim` (an assertion about external mutable state that
-- expires and is verifiable) vs `knowledge` (a durable fact Git can't express).
-- Only ADD values, so every existing row still satisfies the constraint.
ALTER TABLE memory_entry DROP CONSTRAINT IF EXISTS memory_entry_kind_check;
ALTER TABLE memory_entry ADD  CONSTRAINT memory_entry_kind_check
    CHECK (kind IN ('note','decision','todo','handoff','config','claim','knowledge'));

-- "Is this entry behind current code?" → filter by (namespace, repo_sha).
CREATE INDEX IF NOT EXISTS memory_entry_ns_repo_sha ON memory_entry (namespace, repo_sha);
-- "What did this session ship?" → filter by session_id.
CREATE INDEX IF NOT EXISTS memory_entry_session_id  ON memory_entry (session_id);
-- Ad-hoc containment queries over the envelope (e.g. meta @> '{"pr": 11}').
CREATE INDEX IF NOT EXISTS memory_entry_meta_gin     ON memory_entry USING gin (meta jsonb_path_ops);
