"""Backfill embeddings for memory_entry rows that don't have one yet.

Embeddings are written on save going forward, so this is only needed once after
enabling a provider key (or after importing rows). It is idempotent: it only
touches live rows where ``embedding IS NULL`` and stops cleanly when there are
none. Tombstones are skipped (they are never embedded).

Usage: ``python scripts/backfill_embeddings.py`` (reads config via config.py).
Requires VOYAGE_API_KEY; without it the embedder is disabled and the script exits.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import psycopg

# Allow running as a script from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402
from storage.embeddings import build_embedder, embed_text, to_vector_literal  # noqa: E402

BATCH = 64
# This is a bulk, off-critical-path job, so it can afford to wait out rate limits.
# Free Voyage tiers cap requests-per-minute aggressively; retry each batch and
# pace between batches so the whole backfill rides through 429s instead of dying.
MAX_RETRIES = 8
INTER_BATCH_SLEEP = 1.0


async def main() -> None:
    embedder = build_embedder(settings)
    if not embedder.enabled:
        print("no embedding provider configured (set VOYAGE_API_KEY) — nothing to do", flush=True)
        return

    total = 0
    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        while True:
            cur = await conn.execute(
                "SELECT id, key, value FROM memory_entry "
                "WHERE embedding IS NULL AND NOT tombstone "
                "ORDER BY id LIMIT %s",
                (BATCH,),
            )
            rows = await cur.fetchall()
            if not rows:
                break

            # value is jsonb -> already a python object (str/dict/list) via psycopg.
            texts = [embed_text(key, value) for _id, key, value in rows]

            vectors = await embedder.embed(texts, input_type="document", max_retries=MAX_RETRIES)
            if not vectors:
                print("embedder returned no vectors — aborting", flush=True)
                return

            async with conn.transaction():
                for (row_id, _key, _value), vec in zip(rows, vectors):
                    await conn.execute(
                        "UPDATE memory_entry SET embedding = %s::vector WHERE id = %s",
                        (to_vector_literal(vec), row_id),
                    )
            total += len(rows)
            print(f"embedded {len(rows)} rows (total {total})", flush=True)
            await asyncio.sleep(INTER_BATCH_SLEEP)

    print(f"backfill complete — {total} rows embedded", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
