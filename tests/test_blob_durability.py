"""Artifact blobs survive a bytea round-trip byte-identically (by sha256)."""

import hashlib
import os

import base64


async def test_bytea_round_trip_is_byte_identical(pg_call, pg_pool):
    payload = os.urandom(300 * 1024) + b"\x00\xff" * 1000
    digest = hashlib.sha256(payload).hexdigest()

    up = await pg_call(
        "artifact_upload",
        filename="random.bin",
        content=base64.b64encode(payload).decode(),
        encoding="base64",
    )
    assert up["sha256"] == digest
    assert up["size_bytes"] == len(payload)

    # Page the whole blob back through artifact_get (1 MB windows) and reassemble.
    out = bytearray()
    offset = 0
    while True:
        chunk = await pg_call(
            "artifact_get", artifact_id=up["artifact_id"], mode="base64",
            offset=offset, length=128 * 1024,
        )
        out.extend(base64.b64decode(chunk["content"]))
        if chunk["eof"]:
            break
        offset += chunk["length"]

    assert bytes(out) == payload
    assert hashlib.sha256(bytes(out)).hexdigest() == digest

    # Blob metadata preserved in the bytea store.
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT size, content_type, octet_length(bytes) AS n "
            "FROM artifact_blob WHERE sha256=%s",
            (digest,),
        )
        row = await cur.fetchone()
    assert row[0] == len(payload)        # size column
    assert row[2] == len(payload)        # actual bytea length


async def test_identical_content_is_deduped(pg_call, pg_pool):
    payload = b"shared bytes" * 100
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode()

    a = await pg_call("artifact_upload", filename="a.bin", content=encoded, encoding="base64")
    b = await pg_call("artifact_upload", filename="b.bin", content=encoded, encoding="base64")

    assert a["artifact_id"] != b["artifact_id"]   # distinct metadata rows
    assert a["sha256"] == b["sha256"] == digest    # same content address

    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM artifact_blob WHERE sha256=%s", (digest,)
        )
        (blob_count,) = await cur.fetchone()
    assert blob_count == 1  # one physical blob, deduped
