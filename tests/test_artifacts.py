import base64
import hashlib
import os

import pytest

from .conftest import ToolFailure, make_config, make_mcp, call_tool


async def test_text_upload_and_get(call):
    result = await call(
        "artifact_upload", filename="notes.txt", content="hello world", encoding="text"
    )
    assert result["size_bytes"] == 11
    assert result["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert result["debug_capture"] is None

    got = await call("artifact_get", artifact_id=result["artifact_id"], mode="text")
    assert got["content"] == "hello world"
    assert got["eof"] is True

    meta = await call("artifact_get", artifact_id=result["artifact_id"])
    assert "content" not in meta
    assert meta["mime"] == "text/plain"


async def test_json_upload_validates(call):
    ok = await call(
        "artifact_upload", filename="data.json", content='{"a": 1}', encoding="json"
    )
    assert ok["mime"] == "application/json"

    with pytest.raises(ToolFailure) as exc:
        await call("artifact_upload", filename="bad.json", content="{nope", encoding="json")
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_binary_refuses_text_mode(call):
    payload = bytes(range(256)) * 4
    result = await call(
        "artifact_upload",
        filename="blob.bin",
        content=base64.b64encode(payload).decode(),
        encoding="base64",
    )
    with pytest.raises(ToolFailure) as exc:
        await call("artifact_get", artifact_id=result["artifact_id"], mode="text")
    assert exc.value.code == "BINARY_NOT_TEXT"

    got = await call("artifact_get", artifact_id=result["artifact_id"], mode="base64")
    assert base64.b64decode(got["content"]) == payload


async def test_large_artifact_paged_by_range(call):
    """Files above the 1 MB per-call cap are fully retrievable via offset/length paging."""
    payload = os.urandom(int(2.5 * 1024 * 1024))
    uploaded = await call(
        "artifact_upload",
        filename="big.bin",
        content=base64.b64encode(payload).decode(),
        encoding="base64",
    )
    aid = uploaded["artifact_id"]

    chunks, offset = [], 0
    while True:
        page = await call("artifact_get", artifact_id=aid, mode="base64", offset=offset)
        chunk = base64.b64decode(page["content"])
        assert len(chunk) <= 1024 * 1024
        chunks.append(chunk)
        offset += page["length"]
        if page["eof"]:
            break
    assert b"".join(chunks) == payload
    assert len(chunks) == 3

    with pytest.raises(ToolFailure) as exc:
        await call("artifact_get", artifact_id=aid, mode="base64", length=2 * 1024 * 1024)
    assert exc.value.code == "INVALID_ARGUMENT"

    with pytest.raises(ToolFailure) as exc:
        await call("artifact_get", artifact_id=aid, mode="base64", offset=len(payload) + 1)
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_upload_too_large(tmp_path):
    mcp = make_mcp(make_config(tmp_path, max_upload_mb=1))
    payload = os.urandom(1024 * 1024 + 1024)
    with pytest.raises(ToolFailure) as exc:
        await call_tool(
            mcp,
            "artifact_upload",
            filename="huge.bin",
            content=base64.b64encode(payload).decode(),
            encoding="base64",
        )
    assert exc.value.code == "UPLOAD_TOO_LARGE"


async def test_artifact_session_link_and_list(call):
    sid = (await call("session_start", surface="cli"))["session_id"]
    uploaded = await call(
        "artifact_upload",
        filename="log.txt",
        content="trace",
        encoding="text",
        session_id=sid,
    )
    listed = await call("artifact_list", session_id=sid)
    assert [a["artifact_id"] for a in listed["artifacts"]] == [uploaded["artifact_id"]]

    full = await call("session_get", session_id=sid)
    assert full["artifacts"][0]["filename"] == "log.txt"

    with pytest.raises(ToolFailure) as exc:
        await call(
            "artifact_upload", filename="x.txt", content="x", encoding="text",
            session_id="missing",
        )
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_server_status_counts(call):
    await call("memory_save", key="k", value="v")
    await call("session_start", surface="cli")
    await call("artifact_upload", filename="a.txt", content="abc", encoding="text")

    status = await call("server_status")
    assert status["counts"]["memory_keys"] == 1
    assert status["counts"]["sessions"] == 1
    assert status["counts"]["artifacts"] == 1
    assert status["limits"]["max_upload_mb"] == 25
    assert status["storage"]["used_mb"] >= 0
