import logging

import pytest

from .conftest import ToolFailure


async def test_tool_calls_are_logged_without_values(call, caplog):
    with caplog.at_level(logging.INFO, logger="assist_memory.tools"):
        await call("memory_save", key="secret-key-name", value="super-private-value")
    records = [r for r in caplog.records if r.name == "assist_memory.tools"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "tool=memory_save" in message
    assert "outcome=ok" in message
    assert "duration_ms=" in message
    # hygiene: neither argument values nor stored content appear in logs
    assert "super-private-value" not in message
    assert "secret-key-name" not in message


async def test_tool_errors_log_the_code(call, caplog):
    with caplog.at_level(logging.WARNING, logger="assist_memory.tools"):
        with pytest.raises(ToolFailure):
            await call("memory_get", key="missing")
    messages = [r.getMessage() for r in caplog.records if r.name == "assist_memory.tools"]
    assert any("tool=memory_get" in m and "code=NOT_FOUND" in m for m in messages)
