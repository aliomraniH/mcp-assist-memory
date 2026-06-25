---
name: pgvector semantic memory_search
description: Pitfalls when ranking the append-only revisioned memory_entry table by embedding/keyword
---

# Searching the append-only revisioned memory store

`memory_entry` is append-only: a key has many revisions; a delete appends a
tombstone revision (value `{"deleted": true}`). Any "search live memory" query
must reduce to the **latest revision per key first**, then filter.

## Rule: latest-revision-per-key BEFORE any content/embedding filter
Put `SELECT DISTINCT ON (key) ... ORDER BY key, revision DESC` in a subquery,
then apply `WHERE NOT tombstone AND <match>` in the outer query.

**Why:** if you filter inside (e.g. `WHERE value ILIKE %s` or `embedding IS NOT
NULL` next to the `DISTINCT ON`), the filter runs before the dedup, so a deleted
key whose *earlier* revision matched (or was embedded) resurfaces — leaking
deleted/stale content. This was a latent bug in the original keyword-only search
too, not just the semantic leg.

**How to apply:** every leg of `memory_search` (semantic cosine leg AND keyword
ILIKE leg) uses the subquery-then-filter shape. Tombstones embed to NULL, so the
semantic leg also needs `NOT tombstone` in the outer WHERE, not just
`embedding IS NOT NULL`.

## pgvector without the python package
Pass vectors as a text literal `'[0.1,0.2,...]'` and cast `::vector` in SQL
(`embedding <=> %s::vector`). Avoids `pgvector` package / per-connection type
registration. `_row_to_entry` reads specific keys, so `SELECT *` returning the
embedding column is harmless.

## Ranking: RRF fusion, not concatenation
`memory_search` runs the meaning (cosine) leg and keyword (ILIKE substring) leg
independently, then fuses them with Reciprocal Rank Fusion (`_rrf_fuse`, k=60):
each leg adds `1/(k+rank)`, so a key in BOTH legs outranks a single-leg top hit.
Ties break on best individual rank, then key (deterministic).

**Why:** plain "cosine results then keyword backfill" can never let a dual-signal
match beat a cosine-#1 single-signal one. RRF can.

**How to apply:** only the embedded path fuses. When `qvec is None` (embeddings
disabled / provider failed) it stays pure-keyword, original order, no fusion —
do NOT route the fallback through `_rrf_fuse`. Each leg still fetches only
`limit` rows before fusion (candidate-pool overfetch is a known follow-up).

## Pooled connection row_factory persists
`PostgresBackend` sets `conn.row_factory = dict_row` per use, but that setting
**sticks on the pooled connection** after checkin. A test/script that grabs
`pool.connection()` directly may get dict rows (so `row[0]` raises `KeyError: 0`).
Set `conn.row_factory` explicitly when you need a known shape.
