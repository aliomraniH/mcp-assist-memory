"""All 18 MCP tools round-trip against a REAL Postgres backend (pg_call fixture).

Asserts documented effects and values, not just types. Skipped automatically
when DATABASE_URL is unset (see conftest.pg_pool).
"""

import base64


async def test_memory_tools_round_trip(pg_call):
    # memory_save
    saved = await pg_call("memory_save", key="proj/goal", value="ship phase 0", kind="decision")
    assert saved["revision"] == 1
    assert saved["key"] == "proj/goal"

    # memory_get returns the exact value
    got = await pg_call("memory_get", key="proj/goal")
    assert got["value"] == "ship phase 0"
    assert got["kind"] == "decision"
    assert got["revision"] == 1

    # a structured value round-trips through JSON
    await pg_call("memory_save", key="cfg", value={"a": 1, "b": [2, 3]}, kind="config")
    cfg = await pg_call("memory_get", key="cfg")
    assert cfg["value"] == {"a": 1, "b": [2, 3]}

    # memory_list (metadata only)
    listing = await pg_call("memory_list")
    keys = {e["key"] for e in listing["entries"]}
    assert {"proj/goal", "cfg"} <= keys

    # memory_search finds by substring of the value
    found = await pg_call("memory_search", query="phase 0")
    assert any(r["key"] == "proj/goal" for r in found["results"])

    # second revision + history
    await pg_call("memory_save", key="proj/goal", value="ship phase 0 then 1")
    history = await pg_call("memory_history", key="proj/goal")
    assert history["count"] == 2

    # memory_revert creates a NEW revision carrying the old value
    reverted = await pg_call("memory_revert", key="proj/goal", to_revision=1)
    assert reverted["revision"] == 3
    assert reverted["reverted_to"] == 1
    assert (await pg_call("memory_get", key="proj/goal"))["value"] == "ship phase 0"

    # memory_delete tombstones; subsequent get is NOT_FOUND
    deleted = await pg_call("memory_delete", key="cfg")
    assert deleted["deleted"] is True
    import pytest

    from .conftest import ToolFailure

    with pytest.raises(ToolFailure) as exc:
        await pg_call("memory_get", key="cfg")
    assert exc.value.code == "NOT_FOUND"


async def test_session_tools_round_trip(pg_call):
    started = await pg_call("session_start", surface="cli", label="demo")
    sid = started["session_id"]
    assert started["status"] == "open"

    logged = await pg_call("session_log", session_id=sid, type="note", message="step one",
                           data={"k": "v"})
    assert logged["seq"] == 1

    listing = await pg_call("session_list")
    assert any(s["session_id"] == sid for s in listing["sessions"])

    ended = await pg_call("session_end", session_id=sid, summary="all done")
    assert ended["status"] == "closed"
    assert ended["event_count"] == 1

    full = await pg_call("session_get", session_id=sid)
    assert full["summary"] == "all done"
    assert full["events"][0]["message"] == "step one"
    assert full["events"][0]["data"] == {"k": "v"}


async def test_handoff_round_trip(pg_call):
    saved = await pg_call("handoff_save", from_surface="web", content="continue the refactor")
    assert saved["revision"] >= 1

    loaded = await pg_call("handoff_load")
    assert loaded["content"] == "continue the refactor"
    assert loaded["from_surface"] == "web"


async def test_artifact_tools_round_trip(pg_call):
    payload = b"hello artifact bytes \x01\x02\x03"
    up = await pg_call(
        "artifact_upload",
        filename="blob.bin",
        content=base64.b64encode(payload).decode(),
        encoding="base64",
    )
    aid = up["artifact_id"]
    assert up["size_bytes"] == len(payload)
    assert len(up["sha256"]) == 64

    listing = await pg_call("artifact_list")
    assert any(a["artifact_id"] == aid for a in listing["artifacts"])

    meta = await pg_call("artifact_get", artifact_id=aid, mode="metadata")
    assert meta["size_bytes"] == len(payload)

    chunk = await pg_call("artifact_get", artifact_id=aid, mode="base64")
    assert base64.b64decode(chunk["content"]) == payload
    assert chunk["eof"] is True

    text_up = await pg_call(
        "artifact_upload", filename="note.txt", content="plain text", encoding="text"
    )
    text_get = await pg_call("artifact_get", artifact_id=text_up["artifact_id"], mode="text")
    assert text_get["content"] == "plain text"


async def test_server_status_round_trip(pg_call):
    await pg_call("memory_save", key="k1", value="v1")
    status = await pg_call("server_status")
    assert status["counts"]["memory_revisions"] >= 1
    assert status["storage"]["limit_mb"] == 500
    assert status["limits"]["max_upload_mb"] == 25
