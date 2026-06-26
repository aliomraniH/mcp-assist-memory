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
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS memory_entry (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace text NOT NULL, key text NOT NULL, revision integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('note','decision','todo','handoff','config','claim','knowledge')),
    value jsonb NOT NULL, source_surface text, tags text[] NOT NULL DEFAULT '{}',
    event_id uuid, tombstone boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (namespace, key, revision));
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS embedding vector(1024);
-- 0003_provenance.sql columns (mirrored inline so the suite is self-contained).
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS repo_sha   text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS base_sha   text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS branch     text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS dirty      boolean;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS session_id text;
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS meta       jsonb;
-- 0004_content_hash.sql
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS content_hash text;
CREATE UNIQUE INDEX IF NOT EXISTS memory_entry_event_id_uq
    ON memory_entry (event_id) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_entry_embedding_hnsw
    ON memory_entry USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS memory_entry_ns_repo_sha ON memory_entry (namespace, repo_sha);
CREATE INDEX IF NOT EXISTS memory_entry_session_id  ON memory_entry (session_id);
CREATE INDEX IF NOT EXISTS memory_entry_meta_gin    ON memory_entry USING gin (meta jsonb_path_ops);
CREATE INDEX IF NOT EXISTS memory_entry_content_hash ON memory_entry (content_hash);
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


class FakeEmbedder:
    """Deterministic offline embedder for tests. Hashes word tokens into a
    1024-dim bag-of-words vector and L2-normalizes it, so entries that share
    vocabulary with a query get a higher cosine similarity (lower distance).
    This exercises the real pgvector ranking path without a network call."""

    enabled = True

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    async def embed(self, texts, *, input_type: str = "document"):
        import hashlib
        import math
        import re

        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in re.findall(r"\w+", text.lower()):
                idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim
                vec[idx] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


@pytest_asyncio.fixture
async def semantic_backend():
    """Like ``backend`` but with the deterministic FakeEmbedder wired in, so
    memory_search exercises the embedding write + pgvector ranking path."""
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=4)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    yield PostgresBackend(pool, embedder=FakeEmbedder())
    await pool.close()


class FakeResolver:
    """Offline GitHub resolver for tests. Tests populate ``pulls`` / ``heads`` to
    stand in for live PR/branch state without any network call."""

    enabled = True

    def __init__(self) -> None:
        self.pulls: dict = {}   # (repo, pr) -> {"merged": bool, "merge_sha": str}
        self.heads: dict = {}   # (repo, branch) -> sha

    async def merged_state(self, repo, pr):
        return self.pulls.get((repo, int(pr)))

    async def branch_head(self, repo, branch):
        return self.heads.get((repo, branch))


@pytest_asyncio.fixture
async def reconcile_backend():
    """Like ``backend`` but with a FakeResolver wired in, so coord_reconcile
    exercises the real verdict + append-only write path. Tests set
    ``reconcile_backend.resolver.pulls/heads`` to control resolved truth."""
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=4)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    yield PostgresBackend(pool, resolver=FakeResolver())
    await pool.close()


@pytest_asyncio.fixture
def ns():
    """A unique neutral-project namespace per test (project == namespace)."""
    return f"proj-test-{uuid.uuid4().hex[:12]}"
