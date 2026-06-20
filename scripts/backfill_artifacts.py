"""One-time, idempotent backfill of filesystem blobs into the artifact bytea store.

Walks a source directory of content-addressed blob files, streams each (never
slurps), computes sha256, and inserts ON CONFLICT DO NOTHING — so it is safe to
re-run after an interruption and dedups by construction. Verifies by reading a
random sample back. Files over the size cap are skipped and reported, not forced
into bytea (that's the object-storage off-ramp).

Usage: ``python scripts/backfill_artifacts.py /path/to/blobstore``
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import random
import sys

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402

_READ_CHUNK = 1 * 1024 * 1024


def _sha256_streaming(path: pathlib.Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            block = fh.read(_READ_CHUNK)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


async def main(source: str) -> None:
    root = pathlib.Path(source)
    if not root.is_dir():
        print(f"error: {source} is not a directory")
        sys.exit(1)

    files = [p for p in root.rglob("*") if p.is_file()]
    print(f"found {len(files)} candidate files under {source}")

    inserted = skipped_dup = skipped_big = 0
    imported_shas: list[str] = []

    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        for path in files:
            sha, size = _sha256_streaming(path)
            if size > settings.max_artifact_bytes:
                print(f"SKIP (too big: {size}B) {path}")
                skipped_big += 1
                continue
            data = path.read_bytes()
            cur = await conn.execute(
                "INSERT INTO artifact (sha256, bytes, size, content_type) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING RETURNING sha256",
                (sha, data, size, None),
            )
            row = await cur.fetchone()
            await conn.commit()
            if row is None:
                skipped_dup += 1
            else:
                inserted += 1
                imported_shas.append(sha)

        # Verify a random sample by re-reading the stored bytes.
        sample = random.sample(imported_shas, min(5, len(imported_shas)))
        for sha in sample:
            cur = await conn.execute("SELECT bytes FROM artifact WHERE sha256 = %s", (sha,))
            row = await cur.fetchone()
            ok = row is not None and hashlib.sha256(bytes(row[0])).hexdigest() == sha
            print(f"verify {sha[:12]}… {'OK' if ok else 'MISMATCH'}")
            if not ok:
                print("ERROR: checksum mismatch on readback; aborting")
                sys.exit(2)

    print(
        f"done: inserted={inserted} dup={skipped_dup} oversized_skipped={skipped_big} "
        f"verified={len(sample)}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/backfill_artifacts.py /path/to/blobstore")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
