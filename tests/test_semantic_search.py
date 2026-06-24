"""Phase 3 — semantic recall for memory_search.

These tests use the deterministic FakeEmbedder (conftest) so they exercise the
real embedding-on-write path and pgvector cosine ranking without a network call.
The `backend` fixture (no embedder) covers the keyword-only fallback.
"""
from __future__ import annotations


async def test_save_writes_embedding_when_enabled(semantic_backend, ns):
    await semantic_backend.memory_save(ns, "k", {"note": "hello world"})
    async with semantic_backend.pool.connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT embedding IS NOT NULL AS has_vec FROM memory_entry "
            "WHERE namespace = %s AND key = %s ORDER BY revision DESC LIMIT 1",
            (ns, "k"),
        )
        row = await cur.fetchone()
    assert row["has_vec"] is True


async def test_search_ranks_by_meaning(semantic_backend, ns):
    # Two unrelated entries; the query shares vocabulary with the database one.
    await semantic_backend.memory_save(ns, "db", {"note": "postgres database connection pool"})
    await semantic_backend.memory_save(ns, "ui", {"note": "frontend button color styling"})

    results = await semantic_backend.memory_search(ns, "database connection pooling", limit=10)
    keys = [r["key"] for r in results]
    assert "db" in keys and "ui" in keys
    # The semantically closest entry ranks first.
    assert keys[0] == "db"


async def test_search_excludes_tombstoned_latest(semantic_backend, ns):
    await semantic_backend.memory_save(ns, "gone", {"note": "ephemeral database record"})
    await semantic_backend.memory_delete(ns, "gone")
    results = await semantic_backend.memory_search(ns, "database record", limit=10)
    assert all(r["key"] != "gone" for r in results)


async def test_search_is_namespace_isolated_semantic(semantic_backend, ns):
    other = ns + "-other"
    await semantic_backend.memory_save(ns, "k", {"note": "shared vocabulary token apple"})
    # A semantic query from another namespace must not see it.
    assert await semantic_backend.memory_search(other, "apple vocabulary token") == []


async def test_keyword_backfill_when_not_semantically_top(semantic_backend, ns):
    # An entry with no embedding (simulated NULL) still surfaces via the keyword leg.
    await semantic_backend.memory_save(ns, "noembed", {"note": "unique-needle-xyz"})
    async with semantic_backend.pool.connection() as conn:
        await conn.execute(
            "UPDATE memory_entry SET embedding = NULL WHERE namespace = %s AND key = %s",
            (ns, "noembed"),
        )
    results = await semantic_backend.memory_search(ns, "unique-needle-xyz", limit=10)
    assert any(r["key"] == "noembed" for r in results)


class _FailingEmbedder:
    """Enabled, but raises on every call — simulates a provider outage/timeout."""

    enabled = True
    dim = 1024

    async def embed(self, texts, *, input_type: str = "document"):
        raise RuntimeError("provider down")


async def test_write_and_search_survive_embedder_failure(semantic_backend, ns):
    from psycopg.rows import dict_row

    semantic_backend.embedder = _FailingEmbedder()
    # The write must still succeed (best-effort embedding), with a NULL embedding.
    saved = await semantic_backend.memory_save(ns, "k", {"note": "still saved database entry"})
    assert saved["revision"] == 1
    async with semantic_backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT embedding IS NULL AS no_vec FROM memory_entry "
            "WHERE namespace = %s AND key = %s ORDER BY revision DESC LIMIT 1",
            (ns, "k"),
        )
        assert (await cur.fetchone())["no_vec"] is True
    # And search degrades to keyword (query embed also fails -> qvec is None).
    results = await semantic_backend.memory_search(ns, "still saved database entry")
    assert any(r["key"] == "k" for r in results)


async def test_search_falls_back_to_keyword_without_embedder(backend, ns):
    # The plain `backend` fixture has a DisabledEmbedder: pure substring search.
    await backend.memory_save(ns, "k", {"note": "needle-abc in here"})
    results = await backend.memory_search(ns, "needle-abc")
    assert any(r["key"] == "k" for r in results)
    # A query with no substring match returns nothing (no semantic recall).
    assert await backend.memory_search(ns, "completely-unrelated-qqq") == []
