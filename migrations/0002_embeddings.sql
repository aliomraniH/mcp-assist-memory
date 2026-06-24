-- ---------------------------------------------------------------------------
-- Phase 3 — semantic recall.
-- Add an embedding vector to memory_entry plus an approximate-nearest-neighbor
-- (HNSW, cosine) index so memory_search can rank by meaning, not just substring.
--
-- The column is NULLABLE on purpose: rows without an embedding (no provider key,
-- or not yet backfilled) still store and read normally, and memory_search falls
-- back to the existing trigram/keyword path. The trigram index from 0001 stays
-- for that fallback and for the keyword leg of hybrid search.
--
-- Dimension 1024 matches settings.embedding_dim (voyage-3.5-lite default). If you
-- change the model/dimension, this column must be recreated to match.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;       -- enabled in 0001; harmless if already present

ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- Cosine HNSW: matches the `<=>` operator used by memory_search.
CREATE INDEX IF NOT EXISTS memory_entry_embedding_hnsw
    ON memory_entry USING hnsw (embedding vector_cosine_ops);
