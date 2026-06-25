"""PostgresBackend: the StorageBackend ABC implemented over the single pool.

Every method runs on ``self.pool.connection()``; nothing here opens its own
connection. Writes pass through ``sanitize``; revisions/seqs are computed
server-side in the same statement and protected by unique constraints (with a
small retry on the rare concurrent collision). ``event_id`` gives exactly-once
writes: a duplicate is a no-op that returns the already-applied revision.

Tenancy: ``namespace`` is the project tenant. Memory, handoff, and session reads
and writes all filter on it; there are no implicit cross-project reads. Handoffs
are stored as ``kind='handoff'`` rows inside the caller's own namespace (no
shared global handoff space). Artifacts are content-addressed and global.
"""
from __future__ import annotations

import functools
import hashlib
import uuid
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import settings
from storage.base import StorageBackend
from storage.embeddings import DisabledEmbedder, Embedder, embed_text, to_vector_literal
from storage.sanitize import sanitize, wrap_value

_MAX_RETRIES = 3

# How many times a transient connection drop is retried (on a fresh connection).
_CONN_RETRIES = 3
# psycopg raises one of these when a connection is lost or already closed.
_CONN_EXC = (psycopg.OperationalError, psycopg.InterfaceError)


def _is_disconnect(exc: BaseException) -> bool:
    """True only for genuine connection-loss errors (not every OperationalError).

    Covers client-side "connection is closed" (no SQLSTATE), the 08xxx connection
    class, and operator-intervention shutdowns 57P01/57P02/57P03 — e.g. Neon
    scale-down's "terminating connection due to administrator command" (57P01).
    A non-connection OperationalError (lock timeout, too-many-connections, …) is
    left to surface unchanged.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        return isinstance(exc, _CONN_EXC)
    return sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03"}


def _retry_on_disconnect(fn):
    """Retry a backend op on a dropped connection. Each call re-enters
    ``self.pool.connection()``, so the retry runs on a fresh (pool-validated)
    connection. Apply only to reads and idempotent writes: a terminated backend
    rolls an in-flight transaction back, but a drop in the narrow
    commit-but-before-ack window would otherwise replay a non-idempotent write.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        last: Exception | None = None
        for _ in range(_CONN_RETRIES):
            try:
                return await fn(*args, **kwargs)
            except _CONN_EXC as exc:
                if not _is_disconnect(exc):
                    raise
                last = exc
        raise last  # exhausted reconnect attempts
    return wrapper


def _retry_if_idempotent(fn):
    """Disconnect-retry a write only when it carries an ``event_id`` (exactly-once):
    a replay then collapses to a no-op. Without one, the op runs once and a
    disconnect surfaces to the caller, so there is never a silent double-write."""
    retrying = _retry_on_disconnect(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        target = retrying if kwargs.get("event_id") else fn
        return await target(*args, **kwargs)
    return wrapper


def _row_to_entry(row: dict, *, wrap: bool = True) -> dict:
    value = row["value"]
    return {
        "namespace": row["namespace"],
        "key": row["key"],
        "revision": row["revision"],
        "kind": row["kind"],
        "value": wrap_value(value) if wrap else value,
        "tags": list(row.get("tags") or []),
        "source_surface": row.get("source_surface"),
        "tombstone": row.get("tombstone", False),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


_RRF_K = 60


def _rrf_fuse(semantic_rows, keyword_rows, limit, *, k: int = _RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion of the meaning and keyword legs into one ranked list.

    Each leg contributes ``1 / (k + rank)`` (rank is 1-based) to a key's score, so
    a key present in BOTH legs sums two contributions and floats above one that
    only tops a single leg — a true blended ranking rather than concatenating the
    cosine list with keyword backfill. Ties break on the better (smaller)
    individual rank, then key, for a deterministic order. The row payload comes
    from whichever leg saw the key first (semantic, then keyword)."""
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    rows_by_key: dict[str, dict] = {}
    for leg in (semantic_rows, keyword_rows):
        for rank, row in enumerate(leg, start=1):
            key = row["key"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
            rows_by_key.setdefault(key, row)
    ordered = sorted(scores, key=lambda key: (-scores[key], best_rank[key], key))
    return [_row_to_entry(rows_by_key[key]) for key in ordered[:limit]]


def _session_to_dict(row: dict) -> dict:
    return {
        "session_id": str(row["session_id"]),
        "namespace": row["namespace"],
        "surface": row["surface"],
        "metadata": row["metadata"],
        "created_at": row["created_at"].isoformat(),
    }


def _event_to_dict(row: dict) -> dict:
    return {
        "session_id": str(row["session_id"]),
        "namespace": row["namespace"],
        "seq": row["seq"],
        "kind": row["kind"],
        "payload": wrap_value(row["payload"]),
        "created_at": row["created_at"].isoformat(),
    }


class PostgresBackend(StorageBackend):
    def __init__(self, pool, embedder: Embedder | None = None) -> None:
        self.pool = pool
        # Best-effort semantic recall. Defaults to disabled so the backend works
        # with no provider key (keyword-only search, no embeddings written).
        self.embedder: Embedder = embedder or DisabledEmbedder()

    async def _maybe_embed(self, key, value, tombstone) -> str | None:
        """Embed an entry's text for storage. Best-effort: returns None when
        embeddings are disabled, the row is a tombstone, or the provider fails —
        never raises, so a write is never blocked by the embedding path."""
        if not self.embedder.enabled or tombstone:
            return None
        try:
            vecs = await self.embedder.embed([embed_text(key, value)], input_type="document")
        except Exception:  # noqa: BLE001 - embedding is best-effort, fall back to None
            return None
        return self._safe_literal(vecs)

    async def _maybe_embed_query(self, query: str) -> str | None:
        """Embed a search query. Best-effort: None falls search back to keyword."""
        if not self.embedder.enabled:
            return None
        try:
            vecs = await self.embedder.embed([query], input_type="query")
        except Exception:  # noqa: BLE001 - fall back to keyword search
            return None
        return self._safe_literal(vecs)

    def _safe_literal(self, vecs) -> str | None:
        """Turn an embedder result into a pgvector literal, or None on anything
        unexpected. Guards the best-effort contract: a wrong-length / malformed
        vector returns None (keyword-only) instead of failing the ::vector cast
        inside the write transaction and blocking the write."""
        if not vecs:
            return None
        vec = vecs[0]
        expected = getattr(self.embedder, "dim", None)
        if not vec or (expected is not None and len(vec) != expected):
            return None
        try:
            return to_vector_literal(vec)
        except (TypeError, ValueError):  # non-numeric entries
            return None

    # ----------------------------------------------------------------- memory
    async def _seen_event(self, conn, event_id: str) -> dict | None:
        cur = await conn.execute(
            "SELECT * FROM memory_entry WHERE event_id = %s ORDER BY revision DESC LIMIT 1",
            (event_id,),
        )
        return await cur.fetchone()

    async def _append(
        self, namespace, key, value, kind, tags, source_surface, event_id, tombstone
    ) -> dict:
        # Embed BEFORE taking a pooled connection so the (network) embedding call
        # never holds a connection, and compute it once so retries don't re-embed.
        embedding = await self._maybe_embed(key, value, tombstone)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            # Exactly-once: if this event already landed, return it unchanged.
            if event_id:
                existing = await self._seen_event(conn, event_id)
                if existing is not None:
                    return _row_to_entry(existing)

            payload = Jsonb(sanitize(value))
            tags = tags or []
            last_exc: Exception | None = None
            for _ in range(_MAX_RETRIES):
                try:
                    async with conn.transaction():
                        cur = await conn.execute(
                            """
                            INSERT INTO memory_entry
                                (namespace, key, revision, kind, value, source_surface, tags, event_id, tombstone, embedding)
                            SELECT %s, %s,
                                   COALESCE(MAX(revision), 0) + 1,
                                   %s, %s, %s, %s, %s, %s, %s::vector
                            FROM memory_entry WHERE namespace = %s AND key = %s
                            RETURNING *
                            """,
                            (namespace, key, kind, payload, source_surface, tags, event_id,
                             tombstone, embedding, namespace, key),
                        )
                        row = await cur.fetchone()
                        return _row_to_entry(row)
                except pg_errors.UniqueViolation as exc:
                    last_exc = exc
                    # Either a concurrent revision collision (retry) or a racing
                    # duplicate event_id (return the winner).
                    if event_id:
                        async with self.pool.connection() as c2:
                            c2.row_factory = dict_row
                            existing = await self._seen_event(c2, event_id)
                            if existing is not None:
                                return _row_to_entry(existing)
                    continue
            raise last_exc  # exhausted retries

    @_retry_if_idempotent
    async def memory_save(
        self, namespace, key, value, *, kind="note", tags=None, source_surface=None, event_id=None
    ) -> dict:
        return await self._append(namespace, key, value, kind, tags, source_surface, event_id, False)

    @_retry_on_disconnect
    async def memory_get(self, namespace, key) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM memory_entry WHERE namespace = %s AND key = %s ORDER BY revision DESC LIMIT 1",
                (namespace, key),
            )
            row = await cur.fetchone()
        if row is None or row["tombstone"]:
            return None
        return _row_to_entry(row)

    @_retry_on_disconnect
    async def memory_list(self, namespace, *, kind=None, tag=None, limit=100) -> list[dict]:
        clauses = ["namespace = %s"]
        params: list[Any] = [namespace]
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        if tag:
            clauses.append("%s = ANY(tags)")
            params.append(tag)
        where = " AND ".join(clauses)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                f"""
                SELECT DISTINCT ON (key) *
                FROM memory_entry
                WHERE {where}
                ORDER BY key, revision DESC
                """,
                params,
            )
            rows = await cur.fetchall()
        live = [_row_to_entry(r) for r in rows if not r["tombstone"]]
        return live[:limit]

    @_retry_on_disconnect
    async def memory_history(self, namespace, key, *, limit=50) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM memory_entry WHERE namespace = %s AND key = %s "
                "ORDER BY revision DESC LIMIT %s",
                (namespace, key, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    @_retry_if_idempotent
    async def memory_delete(self, namespace, key, *, source_surface=None, event_id=None) -> dict:
        # Tombstone = append a deleting revision (history is preserved).
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT kind FROM memory_entry WHERE namespace = %s AND key = %s "
                "ORDER BY revision DESC LIMIT 1",
                (namespace, key),
            )
            latest = await cur.fetchone()
        kind = latest["kind"] if latest else "note"
        return await self._append(namespace, key, {"deleted": True}, kind, [], source_surface, event_id, True)

    @_retry_on_disconnect
    async def memory_search(self, namespace, query, *, limit=20) -> list[dict]:
        # Tenant-scoped: every leg filters on a single namespace first — no
        # implicit cross-project reads.
        #
        # Hybrid recall: embed the query and rank live entries two ways — by
        # meaning (cosine over pgvector) and by keyword (substring) — then fuse the
        # two rankings into ONE list with Reciprocal Rank Fusion (see _rrf_fuse) so
        # an entry that scores on both signals can outrank one that only tops a
        # single leg. When embeddings are disabled or the provider fails, `qvec` is
        # None and we degrade to pure keyword search — the exact pre-Phase-3
        # behavior (no fusion, keyword order preserved).
        qvec = await self._maybe_embed_query(query)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row

            if qvec is None:
                # Pure-keyword fallback: latest revision per key first, then keep
                # non-tombstoned substring matches (filtering before the DISTINCT ON
                # would resurface a deleted key whose earlier revision matched).
                cur = await conn.execute(
                    """
                    SELECT * FROM (
                        SELECT DISTINCT ON (key) *
                        FROM memory_entry
                        WHERE namespace = %s
                        ORDER BY key, revision DESC
                    ) latest
                    WHERE NOT tombstone AND value::text ILIKE %s
                    LIMIT %s
                    """,
                    (namespace, f"%{query}%", limit),
                )
                rows = await cur.fetchall()
                return [_row_to_entry(r) for r in rows[:limit]]

            # Semantic leg. Tune HNSW recall for this query only via
            # set_config(..., is_local=true): the value is scoped to the
            # surrounding transaction, so ef_search never leaks onto the pooled
            # connection's later reuse (e.g. the keyword leg below). Higher
            # ef_search inspects more index candidates → better recall on large
            # stores, at a little latency; small stores return the same rows
            # regardless. See settings.hnsw_ef_search for the tradeoff.
            #
            # Pick the TRUE latest revision per key first, THEN keep only the live
            # ones that carry an embedding. Filtering embeddings before the
            # DISTINCT ON would let a tombstone's prior (embedded) revision
            # resurface, leaking deleted keys.
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('hnsw.ef_search', %s, true)",
                    (str(settings.hnsw_ef_search),),
                )
                cur = await conn.execute(
                    """
                    SELECT * FROM (
                        SELECT DISTINCT ON (key) *
                        FROM memory_entry
                        WHERE namespace = %s
                        ORDER BY key, revision DESC
                    ) latest
                    WHERE NOT tombstone AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (namespace, qvec, limit),
                )
                semantic_rows = await cur.fetchall()

            # Keyword leg. Same latest-then-filter shape: take the latest revision
            # per key first, then keep non-tombstoned substring matches.
            cur = await conn.execute(
                """
                SELECT * FROM (
                    SELECT DISTINCT ON (key) *
                    FROM memory_entry
                    WHERE namespace = %s
                    ORDER BY key, revision DESC
                ) latest
                WHERE NOT tombstone AND value::text ILIKE %s
                LIMIT %s
                """,
                (namespace, f"%{query}%", limit),
            )
            keyword_rows = await cur.fetchall()

        return _rrf_fuse(semantic_rows, keyword_rows, limit)

    # ---------------------------------------------------------------- handoff
    # Handoffs are cross-surface (web/cli/desktop) within ONE project: stored as
    # kind='handoff' rows inside the caller's namespace, never a shared space.
    @_retry_if_idempotent
    async def handoff_save(self, namespace, key, value, *, source_surface=None, event_id=None) -> dict:
        return await self._append(namespace, key, value, "handoff", [], source_surface, event_id, False)

    async def handoff_load(self, namespace, key) -> dict | None:
        return await self.memory_get(namespace, key)

    async def handoff_list(self, namespace, *, limit=100) -> list[dict]:
        return await self.memory_list(namespace, kind="handoff", limit=limit)

    # --------------------------------------------------------------- sessions
    @_retry_on_disconnect  # a drop in the commit-ack window at worst orphans an
                           # unreferenced empty session row — harmless vs. a hard failure.
    async def session_create(self, namespace, *, surface=None, metadata=None) -> dict:
        sid = uuid.uuid4()
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "INSERT INTO session (session_id, namespace, surface, metadata) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (sid, namespace, surface, Jsonb(metadata or {})),
            )
            row = await cur.fetchone()
        return _session_to_dict(row)

    @_retry_on_disconnect  # at-least-once under a mid-failover drop: a replay in the
                           # narrow commit-ack window may append one duplicate event —
                           # acceptable for an append-only log vs. failing the call.
    async def session_append_event(self, namespace, session_id, kind, payload) -> dict:
        last_exc: Exception | None = None
        for _ in range(_MAX_RETRIES):
            try:
                async with self.pool.connection() as conn:
                    conn.row_factory = dict_row
                    async with conn.transaction():
                        # Tenant guard: the session must exist under this namespace.
                        cur = await conn.execute(
                            "SELECT 1 FROM session WHERE session_id = %s AND namespace = %s",
                            (session_id, namespace),
                        )
                        if await cur.fetchone() is None:
                            raise ValueError("session not found in namespace")
                        cur = await conn.execute(
                            """
                            INSERT INTO session_event (session_id, namespace, seq, kind, payload)
                            SELECT %s, %s, COALESCE(MAX(seq), 0) + 1, %s, %s
                            FROM session_event WHERE session_id = %s
                            RETURNING *
                            """,
                            (session_id, namespace, kind, Jsonb(sanitize(payload)), session_id),
                        )
                        row = await cur.fetchone()
                return _event_to_dict(row)
            except pg_errors.UniqueViolation as exc:
                last_exc = exc
                continue
        raise last_exc

    @_retry_on_disconnect
    async def session_get(self, namespace, session_id) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM session WHERE session_id = %s AND namespace = %s",
                (session_id, namespace),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return _session_to_dict(row)

    @_retry_on_disconnect
    async def session_list(self, namespace, *, limit=50) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM session WHERE namespace = %s ORDER BY created_at DESC LIMIT %s",
                (namespace, limit),
            )
            rows = await cur.fetchall()
        return [_session_to_dict(r) for r in rows]

    @_retry_on_disconnect
    async def session_events(self, namespace, session_id, *, limit=200) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM session_event WHERE session_id = %s AND namespace = %s "
                "ORDER BY seq ASC LIMIT %s",
                (session_id, namespace, limit),
            )
            rows = await cur.fetchall()
        return [_event_to_dict(r) for r in rows]

    # -------------------------------------------------------------- artifacts
    @_retry_on_disconnect  # content-addressed + ON CONFLICT DO NOTHING → safe to replay
    async def artifact_put(self, data: bytes, *, content_type=None) -> dict:
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            # Content-addressed + dedup: a repeat blob is a no-op.
            cur = await conn.execute(
                """
                INSERT INTO artifact (sha256, bytes, size, content_type)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                RETURNING sha256
                """,
                (sha, data, size, content_type),
            )
            inserted = await cur.fetchone()
        return {"sha256": sha, "size": size, "content_type": content_type, "deduped": inserted is None}

    @_retry_on_disconnect
    async def artifact_get(self, sha256) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT sha256, size, content_type, created_at FROM artifact WHERE sha256 = %s",
                (sha256,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "sha256": row["sha256"],
            "size": row["size"],
            "content_type": row["content_type"],
            "created_at": row["created_at"].isoformat(),
        }

    @_retry_on_disconnect
    async def artifact_read_range(self, sha256, offset: int, length: int) -> bytes | None:
        # Ranged read keeps peak memory to one window, never the whole blob.
        # Use dict_row consistently — pooled connections retain whatever
        # row_factory a prior call set, so positional access is unsafe here.
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT substring(bytes FROM %s FOR %s) AS chunk FROM artifact WHERE sha256 = %s",
                (offset + 1, length, sha256),  # SQL substring is 1-indexed
            )
            row = await cur.fetchone()
        if row is None:
            return None
        chunk = row["chunk"]
        return bytes(chunk) if chunk is not None else b""

    @_retry_on_disconnect
    async def artifact_list(self, *, limit=100) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT sha256, size, content_type, created_at FROM artifact ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            {
                "sha256": r["sha256"],
                "size": r["size"],
                "content_type": r["content_type"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ admin
    @_retry_on_disconnect
    async def stats(self) -> dict:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM memory_entry)                       AS memory_revisions,
                    (SELECT count(DISTINCT (namespace, key)) FROM memory_entry) AS memory_keys,
                    (SELECT count(*) FROM session)                            AS sessions,
                    (SELECT count(*) FROM session_event)                      AS session_events,
                    (SELECT count(*) FROM artifact)                           AS artifacts,
                    (SELECT COALESCE(sum(size), 0) FROM artifact)             AS artifact_bytes
                """
            )
            row = await cur.fetchone()
        return dict(row)

    @_retry_on_disconnect
    async def health(self) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True
