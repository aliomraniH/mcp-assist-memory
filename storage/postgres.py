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

import hashlib
import uuid
from typing import Any

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from storage.base import StorageBackend
from storage.sanitize import sanitize, wrap_value

_MAX_RETRIES = 3


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
    def __init__(self, pool) -> None:
        self.pool = pool

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
                                (namespace, key, revision, kind, value, source_surface, tags, event_id, tombstone)
                            SELECT %s, %s,
                                   COALESCE(MAX(revision), 0) + 1,
                                   %s, %s, %s, %s, %s, %s
                            FROM memory_entry WHERE namespace = %s AND key = %s
                            RETURNING *
                            """,
                            (namespace, key, kind, payload, source_surface, tags, event_id,
                             tombstone, namespace, key),
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

    async def memory_save(
        self, namespace, key, value, *, kind="note", tags=None, source_surface=None, event_id=None
    ) -> dict:
        return await self._append(namespace, key, value, kind, tags, source_surface, event_id, False)

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

    async def memory_search(self, namespace, query, *, limit=20) -> list[dict]:
        # Tenant-scoped: search always filters on a single namespace. No
        # implicit cross-project reads. (pgvector semantic recall arrives Phase 3.)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                """
                SELECT DISTINCT ON (key) *
                FROM memory_entry
                WHERE namespace = %s AND value::text ILIKE %s
                ORDER BY key, revision DESC
                LIMIT %s
                """,
                (namespace, f"%{query}%", limit),
            )
            rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows if not r["tombstone"]]

    # ---------------------------------------------------------------- handoff
    # Handoffs are cross-surface (web/cli/desktop) within ONE project: stored as
    # kind='handoff' rows inside the caller's namespace, never a shared space.
    async def handoff_save(self, namespace, key, value, *, source_surface=None, event_id=None) -> dict:
        return await self._append(namespace, key, value, "handoff", [], source_surface, event_id, False)

    async def handoff_load(self, namespace, key) -> dict | None:
        return await self.memory_get(namespace, key)

    async def handoff_list(self, namespace, *, limit=100) -> list[dict]:
        return await self.memory_list(namespace, kind="handoff", limit=limit)

    # --------------------------------------------------------------- sessions
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

    async def session_list(self, namespace, *, limit=50) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM session WHERE namespace = %s ORDER BY created_at DESC LIMIT %s",
                (namespace, limit),
            )
            rows = await cur.fetchall()
        return [_session_to_dict(r) for r in rows]

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

    async def health(self) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True
