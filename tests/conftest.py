import json
import re

import pytest
from mcp.shared.memory import (
    create_connected_server_and_client_session as connect,
)

from assist_memory.config import Config
from assist_memory.server import build_mcp
from assist_memory.storage.sqlite_fs import SqliteFsBackend

TEST_TOKEN = "test-token-123"


class ToolFailure(Exception):
    """Parsed {"code", "message"} from an isError tool result."""

    def __init__(self, code: str, message: str, raw: str):
        super().__init__(raw)
        self.code = code
        self.message = message
        self.raw = raw


def make_config(tmp_path, **overrides) -> Config:
    defaults = dict(
        auth_token=TEST_TOKEN,
        data_dir=tmp_path / "data",
        max_upload_mb=25,
        max_total_storage_mb=500,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_mcp(config: Config):
    backend = SqliteFsBackend(config.data_dir, config.max_total_storage_bytes)
    return build_mcp(config, backend)


async def call_tool(mcp, name: str, **args):
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.isError:
        match = re.search(r"\{.*\}", text, re.S)
        payload = json.loads(match.group(0)) if match else {}
        raise ToolFailure(payload.get("code", "UNKNOWN"), payload.get("message", text), text)
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(text)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def mcp(config):
    return make_mcp(config)


@pytest.fixture
def call(mcp):
    async def _call(name, **args):
        return await call_tool(mcp, name, **args)

    return _call
