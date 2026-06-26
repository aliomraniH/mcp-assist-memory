"""Backfill content_hash (0004) for rows written before the column existed.

content_hash is computed on save going forward; this populates it for older
live rows so the indexed store-wide drift scan (coord_drift_scan) can group on
the column instead of recomputing. The detectors work without this — they hash
on the fly when the column is null — so this is an optimization, not a
prerequisite. Idempotent: only touches live rows where content_hash IS NULL.

Usage: ``python scripts/backfill_content_hash.py`` (reads config via config.py).
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import psycopg

# Allow running as a script from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402
from storage.postgres import _content_hash  # noqa: E402

BATCH = 500


async def main() -> None:
    total = 0
    last_id = 0
    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        while True:
            cur = await conn.execute(
                "SELECT id, value FROM memory_entry "
                "WHERE content_hash IS NULL AND NOT tombstone AND id > %s "
                "ORDER BY id LIMIT %s",
                (last_id, BATCH),
            )
            rows = await cur.fetchall()
            if not rows:
                break
            last_id = rows[-1][0]
            async with conn.transaction():
                for row_id, value in rows:
                    await conn.execute(
                        "UPDATE memory_entry SET content_hash = %s WHERE id = %s",
                        (_content_hash(value), row_id),
                    )
            total += len(rows)
            print(f"hashed {len(rows)} rows (total {total})")

    print(f"backfill complete — {total} rows hashed")


if __name__ == "__main__":
    asyncio.run(main())
