"""The storage backend interface.

This ABC is the swap point: the service depends only on this surface, and a
``PostgresBackend`` implements it. The 18 MCP tools map 1:1 onto these methods
(plus ``health``, used only by ``/healthz``). Keeping this interface stable is
what lets the storage tier change without touching the tool layer.

Tenancy: ``namespace`` is the per-project tenant key (namespace == project).
Every per-project method takes it and every query filters on it — there are no
implicit cross-project reads. Artifacts are the deliberate exception: they are
content-addressed and dedup globally, so they are not tenant-scoped.

Idempotency note: ``event_id`` is a keyword-only, default-``None`` argument on
the mutating methods. Existing callers are unaffected; a caller that wants
exactly-once semantics passes a UUID and a duplicate is a no-op that returns the
already-applied revision (see Phase 2 reconciliation).
"""
from __future__ import annotations

import abc
from typing import Any


class StorageBackend(abc.ABC):
    # ---------------- memory: append-only, revisioned KV ----------------
    @abc.abstractmethod
    async def memory_save(
        self,
        namespace: str,
        key: str,
        value: Any,
        *,
        kind: str = "note",
        tags: list[str] | None = None,
        source_surface: str | None = None,
        event_id: str | None = None,
        meta: dict | None = None,
        actor: str = "unattributed",
        origin: str = "unknown",
        origin_detail: str | None = None,
        origin_model_id: str | None = None,
        origin_model_family: str | None = None,
        derived_from: list[str] | None = None,
    ) -> dict: ...

    @abc.abstractmethod
    async def memory_get(self, namespace: str, key: str) -> dict | None: ...

    @abc.abstractmethod
    async def memory_list(
        self, namespace: str, *, kind: str | None = None, tag: str | None = None,
        prefix: str | None = None, limit: int = 100, include_quarantined: bool = False,
    ) -> list[dict]: ...

    @abc.abstractmethod
    async def memory_list_page(
        self, namespace: str, *, kind: str | None = None, tag: str | None = None,
        prefix: str | None = None, limit: int = 100, cursor: str | None = None,
        include_quarantined: bool = False,
    ) -> dict: ...

    @abc.abstractmethod
    async def memory_history(self, namespace: str, key: str, *, limit: int = 50) -> list[dict]: ...

    @abc.abstractmethod
    async def memory_delete(
        self, namespace: str, key: str, *, source_surface: str | None = None, event_id: str | None = None,
        meta: dict | None = None, actor: str = "unattributed",
    ) -> dict: ...

    @abc.abstractmethod
    async def memory_search(
        self, namespace: str, query: str, *, limit: int = 20, include_quarantined: bool = False
    ) -> list[dict]: ...

    # ---------------- handoff: cross-surface convention, scoped to a project ----------------
    @abc.abstractmethod
    async def handoff_save(
        self, namespace: str, key: str, value: Any, *, source_surface: str | None = None, event_id: str | None = None,
        meta: dict | None = None, actor: str = "unattributed",
        origin: str = "unknown", origin_detail: str | None = None,
        origin_model_id: str | None = None, origin_model_family: str | None = None,
        derived_from: list[str] | None = None,
    ) -> dict: ...

    @abc.abstractmethod
    async def handoff_load(
        self, namespace: str, key: str, *, include_quarantined: bool = False
    ) -> dict | None: ...

    @abc.abstractmethod
    async def handoff_list(
        self, namespace: str, *, limit: int = 100, include_quarantined: bool = False
    ) -> list[dict]: ...

    # ---------------- sessions: episodic memory (tenant-scoped) ----------------
    @abc.abstractmethod
    async def session_create(
        self, namespace: str, *, surface: str | None = None, metadata: dict | None = None
    ) -> dict: ...

    @abc.abstractmethod
    async def session_append_event(
        self, namespace: str, session_id: str, kind: str, payload: Any,
        *, actor: str = "unattributed", event_id: str | None = None,
    ) -> dict: ...

    @abc.abstractmethod
    async def session_get(self, namespace: str, session_id: str) -> dict | None: ...

    @abc.abstractmethod
    async def session_list(self, namespace: str, *, limit: int = 50) -> list[dict]: ...

    @abc.abstractmethod
    async def session_events(self, namespace: str, session_id: str, *, limit: int = 200) -> list[dict]: ...

    # ---------------- artifacts: immutable content-addressed blobs (bytea) ----------------
    @abc.abstractmethod
    async def artifact_put(self, data: bytes, *, content_type: str | None = None) -> dict: ...

    @abc.abstractmethod
    async def artifact_get(self, sha256: str) -> dict | None: ...

    @abc.abstractmethod
    async def artifact_read_range(self, sha256: str, offset: int, length: int) -> bytes | None: ...

    @abc.abstractmethod
    async def artifact_list(self, *, limit: int = 100) -> list[dict]: ...

    # ---------------- coordination (drift detection) ----------------
    @abc.abstractmethod
    async def coord_health(self, namespace: str, *, limit: int = 200) -> dict: ...

    @abc.abstractmethod
    async def coord_drift_scan(self, *, limit: int = 50) -> dict: ...

    @abc.abstractmethod
    async def coord_reconcile(self, namespace: str, *, limit: int = 100) -> dict: ...

    @abc.abstractmethod
    async def coord_reconcile_repo(
        self, repo: str, *, pr: int | None = None, branch: str | None = None, limit: int = 500
    ) -> dict: ...

    # ---------------- admin ----------------
    @abc.abstractmethod
    async def stats(self) -> dict: ...

    @abc.abstractmethod
    async def health(self) -> bool: ...
