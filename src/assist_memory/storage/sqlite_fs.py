"""Default backend: SQLite (metadata + small values) + filesystem blob store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..models import (
    NOT_FOUND,
    SESSION_CLOSED,
    STORAGE_FULL,
    Artifact,
    MemoryRevision,
    Session,
    SessionEvent,
    StorageUsage,
    ToolFault,
    now_iso,
)
from .base import StorageBackend

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_revisions (
  id INTEGER PRIMARY KEY,
  namespace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
  kind TEXT NOT NULL, value TEXT,
  value_is_json INTEGER NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '[]',
  source_surface TEXT NOT NULL DEFAULT 'other',
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (namespace, key, revision)
);
CREATE INDEX IF NOT EXISTS idx_memory_ns_key
  ON memory_revisions (namespace, key, revision DESC);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  surface TEXT NOT NULL, status TEXT NOT NULL,
  summary TEXT, created_at TEXT NOT NULL, ended_at TEXT
);

CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  seq INTEGER NOT NULL, timestamp TEXT NOT NULL,
  type TEXT NOT NULL, message TEXT NOT NULL, data TEXT,
  UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events (session_id, seq);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  filename TEXT NOT NULL, mime TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
  uploaded_at TEXT NOT NULL, source_surface TEXT NOT NULL,
  session_id TEXT, memory_key TEXT, tags TEXT NOT NULL DEFAULT '[]',
  storage_path TEXT NOT NULL, is_debug_capture INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_artifacts_ns ON artifacts (namespace);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts (session_id);

PRAGMA user_version = 1;
"""


class SqliteFsBackend(StorageBackend):
    def __init__(self, data_dir: Path | str, max_total_storage_bytes: int):
        self.data_dir = Path(data_dir)
        self.blobs_dir = self.data_dir / "blobs"
        self.db_path = self.data_dir / "assist_memory.db"
        self.max_total_storage_bytes = max_total_storage_bytes
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- capacity -----------------------------------------------------------

    def usage(self) -> StorageUsage:
        with self._lock:
            used = 0
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.db_path) + suffix)
                if p.exists():
                    used += p.stat().st_size
            for blob in self.blobs_dir.rglob("*"):
                if blob.is_file():
                    used += blob.stat().st_size
            row = self._conn.execute(
                """SELECT
                     (SELECT COUNT(DISTINCT namespace || '/' || key) FROM memory_revisions) AS keys,
                     (SELECT COUNT(*) FROM memory_revisions) AS revs,
                     (SELECT COUNT(*) FROM sessions) AS sessions,
                     (SELECT COUNT(*) FROM sessions WHERE status='open') AS open_sessions,
                     (SELECT COUNT(*) FROM artifacts) AS artifacts"""
            ).fetchone()
        return StorageUsage(
            used_bytes=used,
            memory_keys=row["keys"],
            memory_revisions=row["revs"],
            sessions=row["sessions"],
            open_sessions=row["open_sessions"],
            artifacts=row["artifacts"],
        )

    def ensure_capacity(self, incoming_bytes: int) -> None:
        used = self.usage().used_bytes
        if used + incoming_bytes > self.max_total_storage_bytes:
            mb = 1024 * 1024
            raise ToolFault(
                STORAGE_FULL,
                f"write of {incoming_bytes} bytes would exceed the global storage cap: "
                f"{used / mb:.1f} MB used of {self.max_total_storage_bytes // mb} MB limit",
                used_mb=round(used / mb, 1),
                limit_mb=self.max_total_storage_bytes // mb,
            )

    # -- memory -------------------------------------------------------------

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> MemoryRevision:
        return MemoryRevision(
            namespace=row["namespace"],
            key=row["key"],
            revision=row["revision"],
            kind=row["kind"],
            value=row["value"],
            value_is_json=bool(row["value_is_json"]),
            tags=json.loads(row["tags"]),
            source_surface=row["source_surface"],
            deleted=bool(row["deleted"]),
            created_at=row["created_at"],
        )

    def save_revision(
        self,
        namespace: str,
        key: str,
        value: str | None,
        value_is_json: bool,
        kind: str,
        tags: list[str],
        source_surface: str,
        deleted: bool = False,
    ) -> MemoryRevision:
        created_at = now_iso()
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(revision), 0) AS r FROM memory_revisions "
                "WHERE namespace=? AND key=?",
                (namespace, key),
            )
            revision = cur.fetchone()["r"] + 1
            self._conn.execute(
                "INSERT INTO memory_revisions "
                "(namespace, key, revision, kind, value, value_is_json, tags, "
                " source_surface, deleted, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    namespace,
                    key,
                    revision,
                    kind,
                    value,
                    int(value_is_json),
                    json.dumps(tags),
                    source_surface,
                    int(deleted),
                    created_at,
                ),
            )
            self._conn.commit()
        return MemoryRevision(
            namespace, key, revision, kind, value, value_is_json, list(tags),
            source_surface, deleted, created_at,
        )

    def get_revision(
        self, namespace: str, key: str, revision: int | None
    ) -> MemoryRevision | None:
        with self._lock:
            if revision is None:
                row = self._conn.execute(
                    "SELECT * FROM memory_revisions WHERE namespace=? AND key=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (namespace, key),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM memory_revisions "
                    "WHERE namespace=? AND key=? AND revision=?",
                    (namespace, key, revision),
                ).fetchone()
        return self._row_to_revision(row) if row else None

    def _latest_live(self, namespace: str) -> list[MemoryRevision]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT m.* FROM memory_revisions m
                   JOIN (SELECT namespace, key, MAX(revision) AS rev
                         FROM memory_revisions WHERE namespace=?
                         GROUP BY namespace, key) t
                     ON m.namespace=t.namespace AND m.key=t.key AND m.revision=t.rev
                   WHERE m.deleted=0
                   ORDER BY m.key""",
                (namespace,),
            ).fetchall()
        return [self._row_to_revision(r) for r in rows]

    def list_entries(
        self,
        namespace: str,
        kind: str | None = None,
        tag: str | None = None,
        prefix: str | None = None,
    ) -> list[MemoryRevision]:
        entries = self._latest_live(namespace)
        if kind is not None:
            entries = [e for e in entries if e.kind == kind]
        if tag is not None:
            entries = [e for e in entries if tag in e.tags]
        if prefix is not None:
            entries = [e for e in entries if e.key.startswith(prefix)]
        return entries

    def search_entries(self, namespace: str, query: str) -> list[MemoryRevision]:
        q = query.lower()
        results = []
        for e in self._latest_live(namespace):
            haystacks = [e.key.lower(), " ".join(e.tags).lower()]
            if e.value is not None:
                haystacks.append(e.value.lower())
            if any(q in h for h in haystacks):
                results.append(e)
        return results

    def get_history(self, namespace: str, key: str) -> list[MemoryRevision]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_revisions WHERE namespace=? AND key=? "
                "ORDER BY revision ASC",
                (namespace, key),
            ).fetchall()
        return [self._row_to_revision(r) for r in rows]

    # -- sessions -----------------------------------------------------------

    def _row_to_session(self, row: sqlite3.Row, events: list[SessionEvent], count: int) -> Session:
        return Session(
            session_id=row["session_id"],
            namespace=row["namespace"],
            surface=row["surface"],
            status=row["status"],
            summary=row["summary"],
            created_at=row["created_at"],
            ended_at=row["ended_at"],
            events=events,
            event_count=count,
        )

    def create_session(
        self,
        session_id: str,
        namespace: str,
        surface: str,
        status: str = "open",
        summary: str | None = None,
        created_at: str | None = None,
        ended_at: str | None = None,
    ) -> Session:
        created_at = created_at or now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, namespace, surface, status, "
                "summary, created_at, ended_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, namespace, surface, status, summary, created_at, ended_at),
            )
            self._conn.commit()
        return Session(session_id, namespace, surface, status, summary, created_at, ended_at)

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            event_rows = self._conn.execute(
                "SELECT * FROM session_events WHERE session_id=? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        events = [
            SessionEvent(
                seq=e["seq"],
                timestamp=e["timestamp"],
                type=e["type"],
                message=e["message"],
                data=json.loads(e["data"]) if e["data"] is not None else None,
            )
            for e in event_rows
        ]
        return self._row_to_session(row, events, len(events))

    def append_event(
        self, session_id: str, type: str, message: str, data: Any | None
    ) -> tuple[int, str]:
        timestamp = now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise ToolFault(NOT_FOUND, f"session {session_id!r} not found")
            if row["status"] != "open":
                raise ToolFault(SESSION_CLOSED, f"session {session_id!r} is closed")
            seq = (
                self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM session_events "
                    "WHERE session_id=?",
                    (session_id,),
                ).fetchone()["s"]
                + 1
            )
            self._conn.execute(
                "INSERT INTO session_events (session_id, seq, timestamp, type, "
                "message, data) VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    seq,
                    timestamp,
                    type,
                    message,
                    json.dumps(data) if data is not None else None,
                ),
            )
            self._conn.commit()
        return seq, timestamp

    def close_session(self, session_id: str, summary: str | None) -> Session:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise ToolFault(NOT_FOUND, f"session {session_id!r} not found")
            if row["status"] != "open":
                raise ToolFault(SESSION_CLOSED, f"session {session_id!r} is already closed")
            self._conn.execute(
                "UPDATE sessions SET status='closed', ended_at=?, summary=? "
                "WHERE session_id=?",
                (now_iso(), summary, session_id),
            )
            self._conn.commit()
        session = self.get_session(session_id)
        assert session is not None
        return session

    def update_session_import(self, session_id: str, summary: str | None) -> Session:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='closed', summary=?, "
                "ended_at=COALESCE(ended_at, ?) WHERE session_id=?",
                (summary, now_iso(), session_id),
            )
            self._conn.commit()
        session = self.get_session(session_id)
        if session is None:
            raise ToolFault(NOT_FOUND, f"session {session_id!r} not found")
        return session

    def list_sessions(
        self, namespace: str, status: str | None = None, limit: int = 20
    ) -> list[Session]:
        query = (
            "SELECT s.*, (SELECT COUNT(*) FROM session_events e "
            " WHERE e.session_id = s.session_id) AS event_count "
            "FROM sessions s WHERE namespace=?"
        )
        params: list[Any] = [namespace]
        if status is not None:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC, session_id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_session(r, [], r["event_count"]) for r in rows]

    # -- artifacts ----------------------------------------------------------

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=row["artifact_id"],
            namespace=row["namespace"],
            filename=row["filename"],
            mime=row["mime"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            uploaded_at=row["uploaded_at"],
            source_surface=row["source_surface"],
            session_id=row["session_id"],
            memory_key=row["memory_key"],
            tags=json.loads(row["tags"]),
            storage_path=row["storage_path"],
            is_debug_capture=bool(row["is_debug_capture"]),
        )

    def store_artifact(self, artifact: Artifact, content: bytes) -> Artifact:
        digest = hashlib.sha256(content).hexdigest()
        rel_path = f"{digest[:2]}/{digest}"
        blob_path = self.blobs_dir / rel_path
        artifact.sha256 = digest
        artifact.size_bytes = len(content)
        artifact.storage_path = rel_path
        with self._lock:
            if not blob_path.exists():
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                blob_path.write_bytes(content)
            self._conn.execute(
                "INSERT INTO artifacts (artifact_id, namespace, filename, mime, "
                "size_bytes, sha256, uploaded_at, source_surface, session_id, "
                "memory_key, tags, storage_path, is_debug_capture) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact.artifact_id,
                    artifact.namespace,
                    artifact.filename,
                    artifact.mime,
                    artifact.size_bytes,
                    artifact.sha256,
                    artifact.uploaded_at,
                    artifact.source_surface,
                    artifact.session_id,
                    artifact.memory_key,
                    json.dumps(artifact.tags),
                    artifact.storage_path,
                    int(artifact.is_debug_capture),
                ),
            )
            self._conn.commit()
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        return self._row_to_artifact(row) if row else None

    def read_artifact_bytes(self, artifact_id: str, offset: int, length: int) -> bytes:
        artifact = self.get_artifact(artifact_id)
        if artifact is None:
            raise ToolFault(NOT_FOUND, f"artifact {artifact_id!r} not found")
        with open(self.blobs_dir / artifact.storage_path, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def list_artifacts(
        self, namespace: str | None = None, session_id: str | None = None
    ) -> list[Artifact]:
        query = "SELECT * FROM artifacts WHERE 1=1"
        params: list[Any] = []
        if namespace is not None:
            query += " AND namespace=?"
            params.append(namespace)
        if session_id is not None:
            query += " AND session_id=?"
            params.append(session_id)
        query += " ORDER BY uploaded_at DESC, artifact_id DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_artifact(r) for r in rows]
