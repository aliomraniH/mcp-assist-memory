"""Test fixtures. These run against a REAL Postgres (the Phase 0 gate is live,
not mock). Set DATABASE_URL to a throwaway Neon branch or local PG; tests skip
cleanly if it is absent.

The neutral test project is ``proj-test`` (never a real project name) — each
test gets a unique ``proj-test-<rand>`` namespace so cases don't collide on a
shared Postgres.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from storage.postgres import PostgresBackend

DATABASE_URL = os.environ.get("DATABASE_URL")

# Mirrors migrations/0001_init.sql (kept inline so the suite is self-contained).
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS memory_entry (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace text NOT NULL, key text NOT NULL, revision integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('note','decision','todo','handoff','config')),
    value jsonb NOT NULL, source_surface text, tags text[] NOT NULL DEFAULT '{}',
    event_id uuid, tombstone boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (namespace, key, revision));
CREATE UNIQUE INDEX IF NOT EXISTS memory_entry_event_id_uq
    ON memory_entry (event_id) WHERE event_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS session (
    session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), namespace text NOT NULL,
    surface text, metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS session_event (
    session_id uuid NOT NULL REFERENCES session(session_id) ON DELETE CASCADE,
    namespace text NOT NULL, seq integer NOT NULL, kind text NOT NULL, payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (session_id, seq));
CREATE TABLE IF NOT EXISTS artifact (
    sha256 char(64) PRIMARY KEY, bytes bytea NOT NULL, size integer NOT NULL,
    content_type text, created_at timestamptz NOT NULL DEFAULT now());
"""

@pytest_asyncio.fixture
async def backend():
    # A module-level `pytestmark` in conftest.py does NOT propagate to other test
    # modules, so gate at the fixture: any test needing Postgres skips cleanly
    # when DATABASE_URL is unset. (Pure tests like test_sanitize still run.)
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=4)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    yield PostgresBackend(pool)
    await pool.close()


@pytest_asyncio.fixture
def ns():
    """A unique neutral-project namespace per test (project == namespace)."""
    return f"proj-test-{uuid.uuid4().hex[:12]}"
