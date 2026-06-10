import pytest

from .conftest import ToolFailure


async def test_three_writes_three_revisions(call):
    for i, value in enumerate(["v1", "v2", "v3"], start=1):
        result = await call("memory_save", key="plan", value=value)
        assert result["revision"] == i

    history = await call("memory_history", key="plan")
    assert history["count"] == 3
    assert [r["revision"] for r in history["revisions"]] == [1, 2, 3]

    first = await call("memory_get", key="plan", revision=1)
    assert first["value"] == "v1"
    latest = await call("memory_get", key="plan")
    assert latest["value"] == "v3"
    assert latest["revision"] == 3


async def test_revert_is_non_destructive(call):
    for value in ["v1", "v2", "v3"]:
        await call("memory_save", key="plan", value=value)

    result = await call("memory_revert", key="plan", to_revision=1)
    assert result["revision"] == 4
    assert result["reverted_to"] == 1

    latest = await call("memory_get", key="plan")
    assert latest["revision"] == 4
    assert latest["value"] == "v1"

    history = await call("memory_history", key="plan")
    assert history["count"] == 4
    assert history["revisions"][1]["value_preview"] == "v2"


async def test_tombstone_delete_preserves_history(call):
    await call("memory_save", key="doomed", value="v1")
    await call("memory_save", key="doomed", value="v2")
    result = await call("memory_delete", key="doomed")
    assert result["deleted"] is True
    assert result["revision"] == 3

    with pytest.raises(ToolFailure) as exc:
        await call("memory_get", key="doomed")
    assert exc.value.code == "NOT_FOUND"

    history = await call("memory_history", key="doomed")
    assert history["count"] == 3
    assert history["revisions"][-1]["deleted"] is True

    old = await call("memory_get", key="doomed", revision=1)
    assert old["value"] == "v1"

    revived = await call("memory_revert", key="doomed", to_revision=2)
    assert revived["revision"] == 4
    assert (await call("memory_get", key="doomed"))["value"] == "v2"


async def test_json_value_roundtrip(call):
    value = {"decision": "use sqlite", "alternatives": ["postgres"], "n": 3}
    await call("memory_save", key="arch", value=value, kind="decision")
    got = await call("memory_get", key="arch")
    assert got["value"] == value
    assert got["kind"] == "decision"


async def test_namespaces_are_isolated(call):
    await call("memory_save", key="k", value="default-ns")
    await call("memory_save", key="k", value="other-ns", namespace="proj-a")
    assert (await call("memory_get", key="k"))["value"] == "default-ns"
    assert (await call("memory_get", key="k", namespace="proj-a"))["value"] == "other-ns"


async def test_list_filters_and_excludes_values(call):
    await call("memory_save", key="todo/1", value="x", kind="todo", tags=["sprint-1"])
    await call("memory_save", key="todo/2", value="y", kind="todo")
    await call("memory_save", key="note/1", value="z", kind="note")
    await call("memory_delete", key="todo/2")

    result = await call("memory_list", kind="todo")
    assert result["count"] == 1
    assert result["entries"][0]["key"] == "todo/1"
    assert "value" not in result["entries"][0]

    by_prefix = await call("memory_list", prefix="note/")
    assert [e["key"] for e in by_prefix["entries"]] == ["note/1"]

    by_tag = await call("memory_list", tag="sprint-1")
    assert by_tag["count"] == 1


async def test_search_matches_keys_tags_values(call):
    await call("memory_save", key="alpha", value="the QUICK fox", tags=["animal"])
    await call("memory_save", key="bravo-quick", value="nothing")
    await call("memory_save", key="charlie", value="slow", tags=["quickest"])

    result = await call("memory_search", query="quick")
    assert {r["key"] for r in result["results"]} == {"alpha", "bravo-quick", "charlie"}
    assert result["results"][0]["value_preview"] is not None


async def test_secret_pattern_is_flagged_not_rejected(call):
    token = "ghp_" + "a" * 36
    result = await call("memory_save", key="ci-log", value=f"auth failed for {token}")
    assert result["warnings"], "expected a possible-secret warning"
    assert token not in result["warnings"][0]

    got = await call("memory_get", key="ci-log")
    assert "possible-secret" in got["tags"]
    assert token in got["value"]  # stored, not redacted


async def test_invalid_arguments(call):
    with pytest.raises(ToolFailure) as exc:
        await call("memory_save", key="k", value="v", kind="bogus")
    assert exc.value.code == "INVALID_ARGUMENT"

    with pytest.raises(ToolFailure) as exc:
        await call("memory_save", key="k", value="v", namespace="Bad NS!")
    assert exc.value.code == "INVALID_ARGUMENT"

    with pytest.raises(ToolFailure) as exc:
        await call("memory_get", key="nope")
    assert exc.value.code == "NOT_FOUND"

    with pytest.raises(ToolFailure) as exc:
        await call("memory_save", key="big", value="x" * (256 * 1024 + 1))
    assert exc.value.code == "INVALID_ARGUMENT"
