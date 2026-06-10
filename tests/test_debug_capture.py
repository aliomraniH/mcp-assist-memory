import base64
import io
import json
import zipfile
from pathlib import Path

from .fixtures.build_fixture_zip import BRIEF_MD, FIXTURE_SESSION_ID, SESSION_JSON

FIXTURE = Path(__file__).parent / "fixtures" / "debug_capture_session.zip"


def fixture_b64() -> str:
    return base64.b64encode(FIXTURE.read_bytes()).decode()


async def upload(call, content_b64: str):
    return await call(
        "artifact_upload",
        filename="debug_capture_session.zip",
        content=content_b64,
        encoding="base64",
        source_surface="cli",
    )


async def test_fixture_zip_creates_session_and_brief(call):
    result = await upload(call, fixture_b64())

    dc = result["debug_capture"]
    assert dc["recognized"] is True
    assert dc["session_id"] == FIXTURE_SESSION_ID
    assert dc["session_created"] is True
    assert dc["brief_memory_key"] == f"debug/{FIXTURE_SESSION_ID}/brief"

    session = await call("session_get", session_id=FIXTURE_SESSION_ID)
    assert session["status"] == "closed"
    assert session["summary"] == SESSION_JSON["results"]["summary"]
    assert session["created_at"] == "2026-06-09T14:30:00Z"
    assert session["ended_at"] == "2026-06-09T15:05:42Z"
    # the zip is linked as ONE artifact, not exploded
    assert len(session["artifacts"]) == 1
    assert session["artifacts"][0]["artifact_id"] == result["artifact_id"]

    brief = await call("memory_get", key=f"debug/{FIXTURE_SESSION_ID}/brief")
    assert brief["value"] == BRIEF_MD
    assert brief["kind"] == "handoff"
    assert "debug-capture" in brief["tags"]

    listed = await call("artifact_list")
    assert listed["artifacts"][0]["is_debug_capture"] is True


async def test_reimport_is_idempotent(call):
    await upload(call, fixture_b64())
    again = await upload(call, fixture_b64())
    assert again["debug_capture"]["session_created"] is False

    sessions = await call("session_list", status="closed")
    assert sessions["count"] == 1
    # brief got a second revision, not a duplicate key
    history = await call("memory_history", key=f"debug/{FIXTURE_SESSION_ID}/brief")
    assert history["count"] == 2


async def test_session_json_one_level_deep_recognized(call):
    buf = io.BytesIO()
    payload = dict(SESSION_JSON, session_id="2026-06-10T01-00-00Z_nested")
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export/session.json", json.dumps(payload))
        zf.writestr("export/agent-handoff/brief.md", "nested brief")
    result = await call(
        "artifact_upload",
        filename="nested.zip",
        content=base64.b64encode(buf.getvalue()).decode(),
        encoding="base64",
    )
    assert result["debug_capture"]["session_id"] == "2026-06-10T01-00-00Z_nested"
    brief = await call("memory_get", key="debug/2026-06-10T01-00-00Z_nested/brief")
    assert brief["value"] == "nested brief"


async def test_unrecognized_session_json_stores_plain_artifact(call):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("session.json", json.dumps({"schema_version": "9.9"}))
    result = await call(
        "artifact_upload",
        filename="other.zip",
        content=base64.b64encode(buf.getvalue()).decode(),
        encoding="base64",
    )
    assert result["debug_capture"] is None
    assert result["warnings"]  # explains why it wasn't recognized
    assert (await call("session_list", status="closed"))["count"] == 0
