"""Blobs stored as bytea must come back byte-identical, and dedup by sha256."""
from __future__ import annotations

import hashlib
import os


async def test_bytea_round_trip_byte_identical(backend):
    data = os.urandom(200_000)
    put = await backend.artifact_put(data, content_type="application/octet-stream")
    assert put["sha256"] == hashlib.sha256(data).hexdigest()
    read = await backend.artifact_read_range(put["sha256"], 0, len(data))
    assert read == data


async def test_ranged_read_windows(backend):
    data = bytes(range(256)) * 100  # 25_600 bytes
    put = await backend.artifact_put(data)
    sha = put["sha256"]
    first = await backend.artifact_read_range(sha, 0, 1000)
    middle = await backend.artifact_read_range(sha, 1000, 1000)
    assert first == data[0:1000]
    assert middle == data[1000:2000]


async def test_content_addressed_dedup(backend):
    data = b"dedupe-me" * 10
    a = await backend.artifact_put(data)
    b = await backend.artifact_put(data)
    assert a["sha256"] == b["sha256"]
    assert b["deduped"] is True
