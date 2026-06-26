-- ---------------------------------------------------------------------------
-- Memory Curator (write-side consolidation) — make a curated entry's salience,
-- confidence, supersession boundary, and second embedding leg first-class columns
-- so the apply-worker can rank recall and retire stale facts without rewriting
-- history.
--
-- Additive and online-safe, exactly like 0003: every column is NULLABLE, so a
-- write that supplies none of them (every existing surface) behaves exactly as
-- before, and existing rows keep working unchanged. No CHECK change: the curator's
-- kinds (claim/knowledge/decision/todo/note) are already allowed by 0001/0003.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;   -- enabled in 0001/0002; harmless if present

-- Curator scores, surfaced on reads to rank what should steer future sessions.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS salience   int;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS confidence real;
-- Supersession boundary. NULL = live; a non-NULL timestamp in the PAST means this
-- revision is no longer the live one, the same way `tombstone` excludes a row from
-- "latest live" reads. SUPERSEDE sets this on the old key (history is preserved —
-- nothing is hard-deleted) and writes the new entry alongside it.
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS valid_until timestamptz;
-- The second embedding leg: the curator's `hyde` string (the question a future
-- agent would ask) embedded separately, so memory_search can match a future
-- *question* against `hyde` as well as the statement against `summary` (embedding).
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS hyde_embedding vector(1024);

-- "Rank a namespace's entries by how much they should steer future sessions."
CREATE INDEX IF NOT EXISTS memory_entry_ns_salience
    ON memory_entry (namespace, salience);
-- Partial index for the hot "latest live" path (not tombstoned, not superseded).
CREATE INDEX IF NOT EXISTS memory_entry_live
    ON memory_entry (namespace, key)
    WHERE NOT tombstone AND valid_until IS NULL;
-- Cosine HNSW over the hyde leg, matching the `<=>` operator memory_search uses.
CREATE INDEX IF NOT EXISTS memory_entry_hyde_hnsw
    ON memory_entry USING hnsw (hyde_embedding vector_cosine_ops);
