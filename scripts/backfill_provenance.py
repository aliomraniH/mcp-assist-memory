"""Backfill coordination provenance columns (0003) from envelopes that were
written INSIDE the JSON value before the columns existed.

Conservative on purpose: it only lifts a structured ``meta`` / ``_meta`` object
already present in a row's value into the indexed columns. It does NOT guess a
repo_sha by scraping SHAs out of free prose — a wrong provenance stamp is worse
than none (it would read as authoritative). Legacy prose entries are left with
NULL provenance for the reconciler / a human re-verify to handle.

Idempotent: only touches live rows where ``meta IS NULL`` and stops cleanly when
there are none. Usage: ``python scripts/backfill_provenance.py`` (config via config.py).
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import psycopg
from psycopg.types.json import Jsonb

# Allow running as a script from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402

BATCH = 200
_COLS = ("repo_sha", "base_sha", "branch", "dirty", "session_id")


def _envelope(value) -> dict | None:
    """Return an embedded coordination envelope if the value carries one.

    Accepts a top-level ``meta`` or ``_meta`` object only — anything else is
    treated as "no structured provenance" and skipped."""
    if not isinstance(value, dict):
        return None
    env = value.get("_meta") or value.get("meta")
    return env if isinstance(env, dict) and env else None


async def main() -> None:
    total = lifted = 0
    last_id = 0
    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        while True:
            cur = await conn.execute(
                "SELECT id, value FROM memory_entry "
                "WHERE meta IS NULL AND NOT tombstone AND id > %s "
                "ORDER BY id LIMIT %s",
                (last_id, BATCH),
            )
            rows = await cur.fetchall()
            if not rows:
                break
            last_id = rows[-1][0]
            total += len(rows)

            async with conn.transaction():
                for row_id, value in rows:
                    env = _envelope(value)
                    if env is None:
                        continue
                    vals = [env.get(c) for c in _COLS]
                    await conn.execute(
                        "UPDATE memory_entry SET "
                        "repo_sha=%s, base_sha=%s, branch=%s, dirty=%s, session_id=%s, meta=%s "
                        "WHERE id=%s",
                        (*vals, Jsonb(env), row_id),
                    )
                    lifted += 1
            print(f"scanned {total} rows, lifted {lifted} envelopes")

    print(f"backfill complete — {lifted} of {total} scanned rows had an embedded envelope")


if __name__ == "__main__":
    asyncio.run(main())
