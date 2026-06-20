"""Apply numbered SQL migrations in order, idempotently.

Tracks applied files in a ``schema_migrations`` table so reruns are no-ops.
Usage: ``python scripts/migrate.py`` (reads DATABASE_URL via config.py).
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import psycopg

# Allow running as a script from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


async def main() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("no migrations found")
        return

    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        await conn.commit()

        cur = await conn.execute("SELECT filename FROM schema_migrations")
        applied = {r[0] for r in await cur.fetchall()}

        for path in files:
            if path.name in applied:
                print(f"skip   {path.name} (already applied)")
                continue
            print(f"apply  {path.name}")
            sql = path.read_text()
            await conn.execute(sql)  # type: ignore[arg-type]
            await conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
            await conn.commit()
    print("migrations complete")


if __name__ == "__main__":
    asyncio.run(main())
