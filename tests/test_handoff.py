import pytest

from .conftest import ToolFailure


async def test_handoff_round_trip_across_surfaces(call):
    """Surface 1 (cli) saves a handoff; surface 2 (web) loads it, continues,
    and hands back. The receiving surface can backtrack via revision history."""
    sid = (await call("session_start", surface="cli", label="charts"))["session_id"]
    saved = await call(
        "handoff_save",
        from_surface="cli",
        content="Fixed render guard; next: add age=0 API fixture",
        session_id=sid,
    )
    assert saved["key"] == "handoff/latest"
    assert saved["revision"] == 1

    # web surface picks it up
    loaded = await call("handoff_load")
    assert loaded["content"] == "Fixed render guard; next: add age=0 API fixture"
    assert loaded["from_surface"] == "cli"
    assert loaded["session_id"] == sid
    assert loaded["revision"] == 1

    # web hands back after more work
    await call("handoff_save", from_surface="web", content="age=0 fixture added; deploy next")
    second = await call("handoff_load")
    assert second["revision"] == 2
    assert second["from_surface"] == "web"
    assert [h["revision"] for h in second["history"]] == [1, 2]

    # backtrack using the history pointer
    original = await call("memory_get", key="handoff/latest", revision=1)
    assert "render guard" in original["value"]["content"]


async def test_handoff_load_empty_namespace(call):
    with pytest.raises(ToolFailure) as exc:
        await call("handoff_load", namespace="fresh-ns")
    assert exc.value.code == "NOT_FOUND"


async def test_handoff_is_a_memory_entry(call):
    await call("handoff_save", from_surface="desktop", content="state notes")
    listed = await call("memory_list", kind="handoff")
    assert [e["key"] for e in listed["entries"]] == ["handoff/latest"]
