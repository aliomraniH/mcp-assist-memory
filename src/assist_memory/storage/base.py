"""StorageBackend ABC — the single seam between tool code and persistence.

Tool code never touches sqlite3, file paths, or open() directly; swapping to
Replit Object Storage or Postgres later means implementing this class only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Artifact, MemoryRevision, Session, StorageUsage


class StorageBackend(ABC):
    # -- capacity -----------------------------------------------------------

    @abstractmethod
    def usage(self) -> StorageUsage:
        """Current storage usage (bytes across DB + blobs) and entity counts."""

    @abstractmethod
    def ensure_capacity(self, incoming_bytes: int) -> None:
        """Raise ToolFault(STORAGE_FULL) if the write would exceed the global cap."""

    # -- memory -------------------------------------------------------------

    @abstractmethod
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
        """Append a new revision row; returns the created revision."""

    @abstractmethod
    def get_revision(
        self, namespace: str, key: str, revision: int | None
    ) -> MemoryRevision | None:
        """Fetch a specific revision, or the latest when revision is None."""

    @abstractmethod
    def list_entries(
        self,
        namespace: str,
        kind: str | None = None,
        tag: str | None = None,
        prefix: str | None = None,
    ) -> list[MemoryRevision]:
        """Latest non-tombstone revision per key, filters AND-ed."""

    @abstractmethod
    def search_entries(self, namespace: str, query: str) -> list[MemoryRevision]:
        """Case-insensitive substring match over keys, tags, values (latest, live)."""

    @abstractmethod
    def get_history(self, namespace: str, key: str) -> list[MemoryRevision]:
        """All revisions ascending, including tombstones. Empty if key never existed."""

    # -- sessions -----------------------------------------------------------

    @abstractmethod
    def create_session(
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
    def get_session(self, session_id: str) -> Session | None:
        """Full record including ordered events."""

    @abstractmethod
    def append_event(
        self, session_id: str, type: str, message: str, data: Any | None
    ) -> tuple[int, str]:
        """Append an event; returns (seq, timestamp)."""

    @abstractmethod
    def close_session(self, session_id: str, summary: str | None) -> Session: ...

    @abstractmethod
    def update_session_import(self, session_id: str, summary: str | None) -> Session:
        """Idempotent debug-capture re-import: force status=closed, refresh summary."""

    @abstractmethod
    def list_sessions(
        self, namespace: str, status: str | None = None, limit: int = 20
    ) -> list[Session]: ...

    # -- artifacts ----------------------------------------------------------

    @abstractmethod
    def store_artifact(self, artifact: Artifact, content: bytes) -> Artifact:
        """Persist blob (content-addressed) + metadata row. storage_path/sha256
        on the input are recomputed by the backend."""

    @abstractmethod
    def get_artifact(self, artifact_id: str) -> Artifact | None: ...

    @abstractmethod
    def read_artifact_bytes(self, artifact_id: str, offset: int, length: int) -> bytes: ...

    @abstractmethod
    def list_artifacts(
        self, namespace: str | None = None, session_id: str | None = None
    ) -> list[Artifact]: ...
