import base64
import os

import pytest

from .conftest import ToolFailure, make_config, make_mcp, call_tool


async def test_global_storage_cap_enforced(tmp_path):
    mcp = make_mcp(make_config(tmp_path, max_total_storage_mb=1))

    first = await call_tool(
        mcp,
        "artifact_upload",
        filename="a.bin",
        content=base64.b64encode(os.urandom(900 * 1024)).decode(),
        encoding="base64",
    )
    assert first["size_bytes"] == 900 * 1024

    with pytest.raises(ToolFailure) as exc:
        await call_tool(
            mcp,
            "artifact_upload",
            filename="b.bin",
            content=base64.b64encode(os.urandom(900 * 1024)).decode(),
            encoding="base64",
        )
    assert exc.value.code == "STORAGE_FULL"
    # the error reports current usage against the limit
    assert "used_mb" in exc.value.raw and "limit_mb" in exc.value.raw
    assert "1 MB" in exc.value.message or "limit" in exc.value.message

    # memory writes are capped by the same budget
    with pytest.raises(ToolFailure) as exc:
        await call_tool(mcp, "memory_save", key="big", value="x" * (250 * 1024))
    assert exc.value.code == "STORAGE_FULL"

    # small reads still work and status reports usage
    status = await call_tool(mcp, "server_status")
    assert status["storage"]["limit_mb"] == 1
    assert status["storage"]["used_mb"] > 0.5
