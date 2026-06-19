"""Postgres + pgvector backend (Neon-backed).

Implements the StorageBackend ABC over a single injected AsyncConnectionPool.
No module opens its own connection — the pool is created in the FastAPI lifespan
(see app.py) and handed in here.

Discipline:
  * Writes pass through sanitize() (control-char strip + sentinel defang).
  * Memory revisions are append-only; `revision` is computed server-side
    (max+1) under a per-key transaction advisory lock, never client-supplied.
  * `event_id` gives idempotent saves: a repeated event_id returns the existing
    revision instead of appending.
  * Artifact blobs are content-addressed bytea in `artifact_blob` (deduped by
    sha256); metadata lives in `artifact`. Blob reads use substring() windows
    so the whole `bytes` column is never materialized.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..models import (
    NOT_FOUND,
    SESSION_CLOSED,
    STORAGE_FULL,
    UPLOAD_TOO_LARGE,
    Artifact,
    MemoryRevision,
    Session,
    SessionEvent,
    StorageUsage,
    ToolFault,
)
from .base import StorageBackend
from .sanitize import sanitize

_MB = 1024 * 1024


def _fmt(dt: datetime | None) -> str | None:
    """Render a timestamptz as the canonical 'YYYY-MM-DDTHH:MM:SSZ' the tools emit."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PostgresBackend(StorageBackend):
    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        max_total_storage_bytes: int,
        max_artifact_bytes: int,
    ):
        # The pool may be attached after construction: app.py builds the backend
        # at import and opens the pool in the FastAPI lifespan. Tests pass a live
        # pool directly.
        self.pool = pool  # type: ignore[assignment]
        self.max_total_storage_bytes = max_total_storage_bytes
        self.max_artifact_bytes = max_artifact_bytes

    # -- capacity -----------------------------------------------------------

    async def usage(self) -> StorageUsage:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT
                     (SELECT COUNT(*) FROM
                        (SELECT DISTINCT namespace, key FROM memory_entry) d) AS keys,
                     (SELECT COUNT(*) FROM memory_entry) AS revs,
                     (SELECT COUNT(*) FROM session) AS sessions,
                     (SELECT COUNT(*) FROM session WHERE status='open') AS open_sessions,
                     (SELECT COUNT(*) FROM artifact) AS artifacts,
                     (SELECT COALESCE(SUM(octet_length(value)), 0)
                        FROM memory_entry) AS value_bytes,
                     (SELECT COALESCE(SUM(size), 0) FROM artifact_blob) AS blob_bytes"""
            )
            row = await cur.fetchone()
        assert row is not None
        return StorageUsage(
            used_bytes=int(row["value_bytes"]) + int(row["blob_bytes"]),
            memory_keys=row["keys"],
            memory_revisions=row["revs"],
            sessions=row["sessions"],
            open_sessions=row["open_sessions"],
            artifacts=row["artifacts"],
        )

    async def ensure_capacity(self, incoming_bytes: int) -> None:
        used = (await self.usage()).used_bytes
        if used + incoming_bytes > self.max_total_storage_bytes:
            raise ToolFault(
                STORAGE_FULL,
                f"write of {incoming_bytes} bytes would exceed the global storage cap: "
                f"{used / _MB:.1f} MB used of {self.max_total_storage_bytes // _MB} MB limit",
                used_mb=round(used / _MB, 1),
                limit_mb=self.max_total_storage_bytes // _MB,
            )

    # -- memory -------------------------------------------------------------

    @staticmethod
    def _row_to_revision(row: dict[str, Any]) -> MemoryRevision:
        return MemoryRevision(
            namespace=row["namespace"],
            key=row["key"],
            revision=row["revision"],
            kind=row["kind"],
            value=row["value"],
            value_is_json=bool(row["value_is_json"]),
            tags=list(row["tags"]),
            source_surface=row["source_surface"],
            deleted=bool(row["tombstone"]),
            created_at=_fmt(row["created_at"]) or "",
        )

    async def save_revision(
        self,
        namespace: str,
        key: str,
        value: str | None,
        value_is_json: bool,
        kind: str,
        tags: list[str],
        source_surface: str,
        deleted: bool = False,
        *,
        event_id: str | None = None,
    ) -> MemoryRevision:
        value = sanitize(value)
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Serialize all writes for this (namespace, key) so the max+1 revision
            # computation cannot race. Lock is released at transaction end.
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{namespace}/{key}",),
            )
            if event_id is not None:
                await cur.execute(
                    "SELECT * FROM memory_entry WHERE event_id = %s", (event_id,)
                )
                existing = await cur.fetchone()
                if existing is not None:
                    return self._row_to_revision(existing)

            await cur.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS r FROM memory_entry "
                "WHERE namespace = %s AND key = %s",
                (namespace, key),
            )
            revision = (await cur.fetchone())["r"]  # type: ignore[index]
            await cur.execute(
                "INSERT INTO memory_entry "
                "(namespace, key, revision, kind, value, value_is_json, tags, "
                " source_surface, event_id, tombstone) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING created_at",
                (
                    namespace,
                    key,
                    revision,
                    kind,
                    value,
                    value_is_json,
                    tags,
                    source_surface,
                    event_id,
                    deleted,
                ),
            )
            created_at = _fmt((await cur.fetchone())["created_at"])  # type: ignore[index]
        return MemoryRevision(
            namespace, key, revision, kind, value, value_is_json, list(tags),
            source_surface, deleted, created_at or "",
        )

    async def get_revision(
        self, namespace: str, key: str, revision: int | None
    ) -> MemoryRevision | None:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if revision is None:
                await cur.execute(
                    "SELECT * FROM memory_entry WHERE namespace=%s AND key=%s "
                    "ORDER BY revision DESC LIMIT 1",
                    (namespace, key),
                )
            else:
                await cur.execute(
                    "SELECT * FROM memory_entry WHERE namespace=%s AND key=%s AND revision=%s",
                    (namespace, key, revision),
                )
            row = await cur.fetchone()
        return self._row_to_revision(row) if row else None

    async def _latest_live(self, namespace: str) -> list[MemoryRevision]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT * FROM (
                       SELECT DISTINCT ON (namespace, key) *
                       FROM memory_entry WHERE namespace=%s
                       ORDER BY namespace, key, revision DESC
                   ) latest
                   WHERE tombstone = FALSE
                   ORDER BY key""",
                (namespace,),
            )
            rows = await cur.fetchall()
        return [self._row_to_revision(r) for r in rows]

    async def list_entries(
        self,
        namespace: str,
        kind: str | None = None,
        tag: str | None = None,
        prefix: str | None = None,
    ) -> list[MemoryRevision]:
        entries = await self._latest_live(namespace)
        if kind is not None:
            entries = [e for e in entries if e.kind == kind]
        if tag is not None:
            entries = [e for e in entries if tag in e.tags]
        if prefix is not None:
            entries = [e for e in entries if e.key.startswith(prefix)]
        return entries

    async def search_entries(self, namespace: str, query: str) -> list[MemoryRevision]:
        q = query.lower()
        results = []
        for e in await self._latest_live(namespace):
            haystacks = [e.key.lower(), " ".join(e.tags).lower()]
            if e.value is not None:
                haystacks.append(e.value.lower())
            if any(q in h for h in haystacks):
                results.append(e)
        return results

    async def get_history(self, namespace: str, key: str) -> list[MemoryRevision]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM memory_entry WHERE namespace=%s AND key=%s "
                "ORDER BY revision ASC",
                (namespace, key),
            )
            rows = await cur.fetchall()
        return [self._row_to_revision(r) for r in rows]

    # -- sessions -----------------------------------------------------------

    @staticmethod
    def _row_to_session(
        row: dict[str, Any], events: list[SessionEvent], count: int
    ) -> Session:
        return Session(
            session_id=row["session_id"],
            namespace=row["namespace"],
            surface=row["surface"],
            status=row["status"],
            summary=row["summary"],
            created_at=_fmt(row["created_at"]) or "",
            ended_at=_fmt(row["ended_at"]),
            events=events,
            event_count=count,
        )

    async def create_session(
        self,
        session_id: str,
        namespace: str,
        surface: str,
        status: str = "open",
        summary: str | None = None,
        created_at: str | None = None,
        ended_at: str | None = None,
    ) -> Session:
        summary = sanitize(summary)
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO session (session_id, namespace, surface, status, summary, "
                " created_at, ended_at) "
                "VALUES (%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, now()), %s::timestamptz) "
                "RETURNING created_at, ended_at",
                (session_id, namespace, surface, status, summary, created_at, ended_at),
            )
            row = await cur.fetchone()
        assert row is not None
        return Session(
            session_id, namespace, surface, status, summary,
            _fmt(row["created_at"]) or "", _fmt(row["ended_at"]),
        )

    async def get_session(self, session_id: str) -> Session | None:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM session WHERE session_id=%s", (session_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await cur.execute(
                "SELECT * FROM session_event WHERE session_id=%s ORDER BY seq ASC",
                (session_id,),
            )
            event_rows = await cur.fetchall()
        events = [
            SessionEvent(
                seq=e["seq"],
                timestamp=_fmt(e["timestamp"]) or "",
                type=e["type"],
                message=e["message"],
                data=e["data"],
            )
            for e in event_rows
        ]
        return self._row_to_session(row, events, len(events))

    async def append_event(
        self, session_id: str, type: str, message: str, data: Any | None
    ) -> tuple[int, str]:
        message = sanitize(message) or ""
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,)
            )
            await cur.execute(
                "SELECT status FROM session WHERE session_id=%s", (session_id,)
            )
            row = await cur.fetchone()
            if row is None:
                raise ToolFault(NOT_FOUND, f"session {session_id!r} not found")
            if row["status"] != "open":
                raise ToolFault(SESSION_CLOSED, f"session {session_id!r} is closed")
            await cur.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM session_event WHERE session_id=%s",
                (session_id,),
            )
            seq = (await cur.fetchone())["s"]  # type: ignore[index]
            await cur.execute(
                "INSERT INTO session_event (session_id, seq, type, message, data) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING timestamp",
                (session_id, seq, type, message, Jsonb(data) if data is not None else None),
            )
            timestamp = _fmt((await cur.fetchone())["timestamp"])  # type: ignore[index]
        return seq, timestamp or ""

    async def close_session(self, session_id: str, summary: str | None) -> Session:
        summary = sanitize(summary)
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM session WHERE session_id=%s", (session_id,)
            )
            row = await cur.fetchone()
            if row is None:
                raise ToolFault(NOT_FOUND, f"session {session_id!r} not found")
            if row["status"] != "open":
                raise ToolFault(
                    SESSION_CLOSED, f"session {session_id!r} is already closed"
                )
            await cur.execute(
                "UPDATE session SET status='closed', ended_at=now(), summary=%s "
                "WHERE session_id=%s",
                (summary, session_id),
            )
        session = await self.get_session(session_id)
        assert session is not None
        return session

    async def update_session_import(self, session_id: str, summary: str | None) -> Session:
        summary = sanitize(summary)
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE session SET status='closed', summary=%s, "
                "ended_at=COALESCE(ended_at, now()) WHERE session_id=%s",
                (summary, session_id),
            )
        session = await self.get_session(session_id)
        if session is None:
            raise ToolFault(NOT_FOUND, f"session {session_id!r} not found")
        return session

    async def list_sessions(
        self, namespace: str, status: str | None = None, limit: int = 20
    ) -> list[Session]:
        query = (
            "SELECT s.*, (SELECT COUNT(*) FROM session_event e "
            " WHERE e.session_id = s.session_id) AS event_count "
            "FROM session s WHERE namespace=%s"
        )
        params: list[Any] = [namespace]
        if status is not None:
            query += " AND status=%s"
            params.append(status)
        query += " ORDER BY created_at DESC, session_id DESC LIMIT %s"
        params.append(limit)
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [self._row_to_session(r, [], r["event_count"]) for r in rows]

    # -- artifacts ----------------------------------------------------------

    @staticmethod
    def _row_to_artifact(row: dict[str, Any]) -> Artifact:
        return Artifact(
            artifact_id=row["artifact_id"],
            namespace=row["namespace"],
            filename=row["filename"],
            mime=row["mime"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            uploaded_at=_fmt(row["uploaded_at"]) or "",
            source_surface=row["source_surface"],
            session_id=row["session_id"],
            memory_key=row["memory_key"],
            tags=list(row["tags"]),
            storage_path=row["sha256"],
            is_debug_capture=bool(row["is_debug_capture"]),
        )

    async def store_artifact(self, artifact: Artifact, content: bytes) -> Artifact:
        if len(content) > self.max_artifact_bytes:
            raise ToolFault(
                UPLOAD_TOO_LARGE,
                f"artifact is {len(content)} bytes; the per-blob storage limit is "
                f"{self.max_artifact_bytes // _MB} MB",
            )
        digest = hashlib.sha256(content).hexdigest()
        artifact.sha256 = digest
        artifact.size_bytes = len(content)
        artifact.storage_path = digest
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO artifact_blob (sha256, bytes, size, content_type) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (sha256) DO NOTHING",
                (digest, content, len(content), artifact.mime),
            )
            await cur.execute(
                "INSERT INTO artifact (artifact_id, namespace, filename, mime, size_bytes, "
                " sha256, source_surface, session_id, memory_key, tags, is_debug_capture) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING uploaded_at",
                (
                    artifact.artifact_id,
                    artifact.namespace,
                    artifact.filename,
                    artifact.mime,
                    artifact.size_bytes,
                    digest,
                    artifact.source_surface,
                    artifact.session_id,
                    artifact.memory_key,
                    artifact.tags,
                    artifact.is_debug_capture,
                ),
            )
            artifact.uploaded_at = _fmt((await cur.fetchone())["uploaded_at"]) or ""  # type: ignore[index]
        return artifact

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM artifact WHERE artifact_id=%s", (artifact_id,)
            )
            row = await cur.fetchone()
        return self._row_to_artifact(row) if row else None

    async def read_artifact_bytes(
        self, artifact_id: str, offset: int, length: int
    ) -> bytes:
        # Window read via substring(): the whole `bytes` column is never selected.
        # substring() is 1-based, so offset 0 maps to FROM 1.
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT substring(b.bytes FROM %s FOR %s) "
                "FROM artifact a JOIN artifact_blob b ON a.sha256 = b.sha256 "
                "WHERE a.artifact_id = %s",
                (offset + 1, length, artifact_id),
            )
            row = await cur.fetchone()
        if row is None:
            raise ToolFault(NOT_FOUND, f"artifact {artifact_id!r} not found")
        chunk = row[0]
        return bytes(chunk) if chunk is not None else b""

    async def list_artifacts(
        self, namespace: str | None = None, session_id: str | None = None
    ) -> list[Artifact]:
        query = "SELECT * FROM artifact WHERE TRUE"
        params: list[Any] = []
        if namespace is not None:
            query += " AND namespace=%s"
            params.append(namespace)
        if session_id is not None:
            query += " AND session_id=%s"
            params.append(session_id)
        query += " ORDER BY uploaded_at DESC, artifact_id DESC"
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [self._row_to_artifact(r) for r in rows]
