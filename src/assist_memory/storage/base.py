"""StorageBackend ABC — the single seam between tool code and persistence.

Tool code never touches sqlite3, psycopg, file paths, or open() directly;
swapping backends (SQLite+filesystem ↔ Postgres+pgvector) means implementing
this class only.

Methods are async: the production backend runs over an AsyncConnectionPool, and
the MCP tools that call them are async. The wire contracts of the 18 tools are
unchanged; only the Python def/async def shape differs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Artifact, MemoryRevision, Session, StorageUsage


class StorageBackend(ABC):
    # -- capacity -----------------------------------------------------------

    @abstractmethod
    async def usage(self) -> StorageUsage:
        """Current storage usage (bytes across DB + blobs) and entity counts."""

    @abstractmethod
    async def ensure_capacity(self, incoming_bytes: int) -> None:
        """Raise ToolFault(STORAGE_FULL) if the write would exceed the global cap."""

    # -- memory -------------------------------------------------------------

    @abstractmethod
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
        """Append a new revision row; returns the created revision.

        `event_id` (keyword-only, backend-internal) is an idempotency key: if a
        revision with the same event_id already exists, the existing latest
        revision is returned instead of appending a duplicate. None disables
        dedupe. No Phase-0 tool passes it, so the 18 tool contracts are unchanged.
        """

    @abstractmethod
    async def get_revision(
        self, namespace: str, key: str, revision: int | None
    ) -> MemoryRevision | None:
        """Fetch a specific revision, or the latest when revision is None."""

    @abstractmethod
    async def list_entries(
        self,
        namespace: str,
        kind: str | None = None,
        tag: str | None = None,
        prefix: str | None = None,
    ) -> list[MemoryRevision]:
        """Latest non-tombstone revision per key, filters AND-ed."""

    @abstractmethod
    async def search_entries(self, namespace: str, query: str) -> list[MemoryRevision]:
        """Case-insensitive substring match over keys, tags, values (latest, live)."""

    @abstractmethod
    async def get_history(self, namespace: str, key: str) -> list[MemoryRevision]:
        """All revisions ascending, including tombstones. Empty if key never existed."""

    # -- sessions -----------------------------------------------------------

    @abstractmethod
    async def create_session(
        self,
        session_id: str,
        namespace: str,
        surface: str,
        status: str = "open",
        summary: str | None = None,
        created_at: str | None = None,
        ended_at: str | None = None,
    ) -> Session: ...

    @abstractmethod
    async def get_session(self, session_id: str) -> Session | None:
        """Full record including ordered events."""

    @abstractmethod
    async def append_event(
        self, session_id: str, type: str, message: str, data: Any | None
    ) -> tuple[int, str]:
        """Append an event; returns (seq, timestamp)."""

    @abstractmethod
    async def close_session(self, session_id: str, summary: str | None) -> Session: ...

    @abstractmethod
    async def update_session_import(self, session_id: str, summary: str | None) -> Session:
        """Idempotent debug-capture re-import: force status=closed, refresh summary."""

    @abstractmethod
    async def list_sessions(
        self, namespace: str, status: str | None = None, limit: int = 20
    ) -> list[Session]: ...

    # -- artifacts ----------------------------------------------------------

    @abstractmethod
    async def store_artifact(self, artifact: Artifact, content: bytes) -> Artifact:
        """Persist blob (content-addressed) + metadata row. storage_path/sha256
        on the input are recomputed by the backend."""

    @abstractmethod
    async def get_artifact(self, artifact_id: str) -> Artifact | None: ...

    @abstractmethod
    async def read_artifact_bytes(
        self, artifact_id: str, offset: int, length: int
    ) -> bytes: ...

    @abstractmethod
    async def list_artifacts(
        self, namespace: str | None = None, session_id: str | None = None
    ) -> list[Artifact]: ...
