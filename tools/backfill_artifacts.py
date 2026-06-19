"""One-time backfill: import filesystem blobs from a SQLite/FS deployment into
the Postgres `artifact_blob` bytea store.

NOT part of the frozen migration. Idempotent and re-runnable: each blob is
inserted with ON CONFLICT (sha256) DO NOTHING, so running twice is a no-op.

Usage:
    DATABASE_URL=... python tools/backfill_artifacts.py /path/to/data/blobs
                                  [--max-bytes 26214400] [--verify]

The old on-disk layout is content-addressed (`<sha256[:2]>/<sha256>`), so the
filename IS the digest; we recompute and verify it while streaming. Blobs larger
than --max-bytes are skipped and reported (the signal to move to object storage
instead of bytea). With --verify, a random sample is read back from Postgres and
re-checksummed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import random
import sys
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

MB = 1024 * 1024
DEFAULT_MAX_BYTES = 25 * MB
BATCH = 50


def _iter_blob_files(blobs_dir: Path):
    for path in sorted(blobs_dir.rglob("*")):
        if path.is_file():
            yield path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(MB), b""):
            h.update(chunk)
    return h.hexdigest()


async def backfill(blobs_dir: Path, dsn: str, max_bytes: int, verify: bool) -> int:
    files = list(_iter_blob_files(blobs_dir))
    if not files:
        print(f"no blobs found under {blobs_dir} — nothing to do")
        return 0

    sizes = [p.stat().st_size for p in files]
    print(f"survey: {len(files)} blobs, max single blob = {max(sizes) / MB:.2f} MB")
    oversized = [p for p, s in zip(files, sizes) if s > max_bytes]
    if oversized:
        print(
            f"WARNING: {len(oversized)} blob(s) exceed the {max_bytes // MB} MB cap "
            "and will be SKIPPED — consider object storage instead of bytea:"
        )
        for p in oversized:
            print(f"  skip (oversized): {p}  ({p.stat().st_size / MB:.2f} MB)")

    inserted = 0
    imported_digests: list[str] = []
    pool = AsyncConnectionPool(dsn, open=False, min_size=1, max_size=4)
    await pool.open()
    try:
        batch: list[tuple[str, bytes, int, str]] = []

        async def flush() -> int:
            if not batch:
                return 0
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO artifact_blob (sha256, bytes, size, content_type) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (sha256) DO NOTHING",
                        batch,
                    )
                await conn.commit()
            n = len(batch)
            batch.clear()
            return n

        for path in files:
            if path.stat().st_size > max_bytes:
                continue
            data = path.read_bytes()
            digest = _sha256(path)
            if path.name != digest:
                print(f"  note: filename {path.name!r} != sha256 {digest!r} (using sha256)")
            batch.append((digest, data, len(data), "application/octet-stream"))
            imported_digests.append(digest)
            if len(batch) >= BATCH:
                inserted += await flush()
        inserted += await flush()

        # Verification: row count present + a re-checksummed random sample.
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM artifact_blob")
            (total_rows,) = await cur.fetchone()  # type: ignore[misc]
        print(f"processed (attempted): {inserted}; artifact_blob total rows: {total_rows}")

        expected = set(imported_digests)
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(DISTINCT sha256) FROM artifact_blob WHERE sha256 = ANY(%s)",
                (list(expected),),
            )
            (present,) = await cur.fetchone()  # type: ignore[misc]
        if present != len(expected):
            print(f"ERROR: expected {len(expected)} distinct digests present, found {present}")
            return 1
        print(f"verify: all {len(expected)} imported digests present in artifact_blob")

        if verify and expected:
            sample = random.sample(sorted(expected), min(5, len(expected)))
            async with pool.connection() as conn, conn.cursor() as cur:
                for digest in sample:
                    await cur.execute(
                        "SELECT bytes FROM artifact_blob WHERE sha256=%s", (digest,)
                    )
                    row = await cur.fetchone()
                    back = bytes(row[0])  # type: ignore[index]
                    assert hashlib.sha256(back).hexdigest() == digest, digest
            print(f"verify: re-checksummed {len(sample)} random blob(s) byte-identically")
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill FS blobs into Postgres bytea")
    ap.add_argument("blobs_dir", type=Path, help="path to the old data/blobs directory")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--verify", action="store_true", help="re-checksum a random sample")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    if not args.blobs_dir.is_dir():
        print(f"not a directory: {args.blobs_dir}", file=sys.stderr)
        return 2
    return asyncio.run(backfill(args.blobs_dir, dsn, args.max_bytes, args.verify))


if __name__ == "__main__":
    raise SystemExit(main())
