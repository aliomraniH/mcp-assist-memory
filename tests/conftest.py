import json
import re
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from mcp.shared.memory import (
    create_connected_server_and_client_session as connect,
)

from assist_memory.config import Settings
from assist_memory.server import build_mcp
from assist_memory.storage.sqlite_fs import SqliteFsBackend

TEST_TOKEN = "test-token-123"

_MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0001_init.sql"
_PG_TABLES = (
    "session_event",
    "artifact",
    "artifact_blob",
    "memory_entry",
    "session",
)


class ToolFailure(Exception):
    """Parsed {"code", "message"} from an isError tool result."""

    def __init__(self, code: str, message: str, raw: str):
        super().__init__(raw)
        self.code = code
        self.message = message
        self.raw = raw


def make_config(tmp_path, **overrides) -> Settings:
    defaults = dict(
        mcp_auth_token=TEST_TOKEN,
        data_dir=tmp_path / "data",
        max_upload_mb=25,
        max_total_storage_mb=500,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_mcp(config: Settings):
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


# ---------------------------------------------------------------------------
# SQLite-backed fixtures (default; no external services)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Postgres-backed fixtures (skipped unless DATABASE_URL is set)
#
# These run the REAL PostgresBackend against a live Postgres with pgvector. The
# pool uses the same Neon-tuned settings as production (app.create_pool); the
# 0001 migration is applied once and tables are truncated between tests.
# ---------------------------------------------------------------------------
def _database_url() -> str | None:
    import os  # noqa: PLC0415 -- test harness only; not application code

    return os.environ.get("DATABASE_URL")


@pytest_asyncio.fixture(scope="session")
async def pg_pool():  # type: ignore[no-untyped-def]
    dsn = _database_url()
    if not dsn:
        pytest.skip("DATABASE_URL not set — Postgres tests need a live database")

    from assist_memory.app import create_pool

    pool = create_pool(dsn)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(_MIGRATION.read_text())
        await conn.commit()
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def pg_clean(pg_pool) -> AsyncGenerator[None, None]:  # type: ignore[no-untyped-def]
    truncate = f"TRUNCATE {', '.join(_PG_TABLES)} CASCADE"
    async with pg_pool.connection() as conn:
        await conn.execute(truncate)
        await conn.commit()
    yield


@pytest_asyncio.fixture
async def pg_backend(pg_pool, pg_clean):  # type: ignore[no-untyped-def]
    from assist_memory.storage.postgres import PostgresBackend

    return PostgresBackend(
        pg_pool, max_total_storage_bytes=500 * 1024 * 1024, max_artifact_bytes=25 * 1024 * 1024
    )


@pytest_asyncio.fixture
async def pg_call(tmp_path, pg_backend):  # type: ignore[no-untyped-def]
    mcp = build_mcp(make_config(tmp_path), pg_backend)

    async def _call(name, **args):
        return await call_tool(mcp, name, **args)

    return _call
