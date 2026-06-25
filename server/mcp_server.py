"""FastMCP instance and the 18 tools.

The tools are thin: they validate/relay to the injected ``StorageBackend``.
The backend is set on ``deps`` during the FastAPI lifespan (one pool, injected),
so tools never open connections or read config themselves.

Tenancy: every per-project tool takes a required ``namespace`` (namespace ==
project == tenant) and the backend filters every query on it — there are no
implicit cross-project reads. Artifacts are content-addressed and global.

Tool surface (18):
  memory:   memory_save, memory_get, memory_list, memory_history, memory_delete, memory_search
  handoff:  handoff_save, handoff_load, handoff_list
  session:  session_create, session_append_event, session_get, session_list, session_events
  artifact: artifact_put, artifact_get, artifact_list
  admin:    stats
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from config import settings
from storage.base import StorageBackend


@dataclass
class Deps:
    backend: StorageBackend | None = None


deps = Deps()


def _backend() -> StorageBackend:
    if deps.backend is None:  # pragma: no cover - lifespan always sets this
        raise RuntimeError("storage backend not initialized")
    return deps.backend


mcp: FastMCP = FastMCP(name="assist-memory")


# ------------------------------------------------------------------ memory
@mcp.tool
async def memory_save(
    namespace: str,
    key: str,
    value: Any,
    kind: str = "note",
    tags: list[str] | None = None,
    source_surface: str | None = None,
    event_id: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Append a new revision of a memory entry in a project namespace.
    kind ∈ note|decision|todo|handoff|config|claim|knowledge (claim = a verifiable
    assertion about external mutable state that expires; knowledge = a durable fact).
    Pass a stable event_id (uuid) for exactly-once writes during offline reconcile.

    meta is an optional coordination envelope: its repo_sha/base_sha/branch/dirty/
    session_id keys are projected into indexed columns (the rest kept as-is) so a
    reader can mechanically ask "is this still current?" instead of parsing prose.
    Best-effort — omit it and the entry stores exactly as before."""
    return await _backend().memory_save(
        namespace, key, value, kind=kind, tags=tags,
        source_surface=source_surface, event_id=event_id, meta=meta,
    )


@mcp.tool
async def memory_get(namespace: str, key: str) -> dict | None:
    """Return the latest live revision of a key in a namespace, or null if missing/deleted."""
    return await _backend().memory_get(namespace, key)


@mcp.tool
async def memory_list(
    namespace: str, kind: str | None = None, tag: str | None = None, limit: int = 100
) -> list[dict]:
    """List the latest live entry per key in a namespace, optionally filtered by kind/tag."""
    return await _backend().memory_list(namespace, kind=kind, tag=tag, limit=limit)


@mcp.tool
async def memory_history(namespace: str, key: str, limit: int = 50) -> list[dict]:
    """Return revision history (newest first) for a key in a namespace, including tombstones."""
    return await _backend().memory_history(namespace, key, limit=limit)


@mcp.tool
async def memory_delete(
    namespace: str, key: str, source_surface: str | None = None, event_id: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Soft-delete a key by appending a tombstone revision (history preserved).
    meta optionally records the provenance of the deletion (repo_sha/session_id…)."""
    return await _backend().memory_delete(
        namespace, key, source_surface=source_surface, event_id=event_id, meta=meta,
    )


@mcp.tool
async def memory_search(namespace: str, query: str, limit: int = 20) -> list[dict]:
    """Search memory within ONE namespace (no cross-project reads).

    Ranks live entries by meaning using embeddings (pgvector cosine) and backfills
    keyword/substring matches. When no embedding provider is configured it degrades
    to pure substring search."""
    return await _backend().memory_search(namespace, query, limit=limit)


# ------------------------------------------------------------------ handoff
@mcp.tool
async def handoff_save(
    namespace: str, key: str, value: Any, source_surface: str | None = None, event_id: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Save a cross-surface handoff under a shared key within a project namespace
    (read it back with handoff_load). meta is the optional coordination envelope
    (see memory_save)."""
    return await _backend().handoff_save(
        namespace, key, value, source_surface=source_surface, event_id=event_id, meta=meta,
    )


@mcp.tool
async def handoff_load(namespace: str, key: str) -> dict | None:
    """Load the latest handoff for a shared key in a namespace (written by any surface)."""
    return await _backend().handoff_load(namespace, key)


@mcp.tool
async def handoff_list(namespace: str, limit: int = 100) -> list[dict]:
    """List active handoffs in a namespace."""
    return await _backend().handoff_list(namespace, limit=limit)


# ------------------------------------------------------------------ session
@mcp.tool
async def session_create(namespace: str, surface: str | None = None, metadata: dict | None = None) -> dict:
    """Start an episodic session in a project namespace; returns its session_id."""
    return await _backend().session_create(namespace, surface=surface, metadata=metadata)


@mcp.tool
async def session_append_event(namespace: str, session_id: str, kind: str, payload: Any) -> dict:
    """Append an ordered event to a session in this namespace; returns the assigned seq."""
    return await _backend().session_append_event(namespace, session_id, kind, payload)


@mcp.tool
async def session_get(namespace: str, session_id: str) -> dict | None:
    """Fetch session metadata (scoped to the namespace)."""
    return await _backend().session_get(namespace, session_id)


@mcp.tool
async def session_list(namespace: str, limit: int = 50) -> list[dict]:
    """List recent sessions in a namespace (newest first)."""
    return await _backend().session_list(namespace, limit=limit)


@mcp.tool
async def session_events(namespace: str, session_id: str, limit: int = 200) -> list[dict]:
    """Return a session's events in seq order (scoped to the namespace)."""
    return await _backend().session_events(namespace, session_id, limit=limit)


# ----------------------------------------------------------------- artifact
@mcp.tool
async def artifact_put(content_base64: str, content_type: str | None = None) -> dict:
    """Store an immutable blob (base64). Rejects blobs over the configured size cap.
    Returns its sha256 (content address). Artifacts are content-addressed and global."""
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"content_base64 is not valid base64: {exc}") from exc
    if len(data) > settings.max_artifact_bytes:
        raise ValueError(
            f"artifact {len(data)} bytes exceeds cap {settings.max_artifact_bytes}; "
            "store large objects in object storage and reference the sha256"
        )
    return await _backend().artifact_put(data, content_type=content_type)


@mcp.tool
async def artifact_get(sha256: str) -> dict | None:
    """Return artifact metadata. Small blobs (< inline limit) include base64 content;
    larger blobs are fetched via GET /artifact/{sha256} (streamed)."""
    meta = await _backend().artifact_get(sha256)
    if meta is None:
        return None
    if meta["size"] <= settings.artifact_inline_limit:
        data = await _backend().artifact_read_range(sha256, 0, meta["size"])
        meta = {**meta, "content_base64": base64.b64encode(data or b"").decode("ascii")}
    else:
        meta = {**meta, "content_url": f"/artifact/{sha256}", "inline": False}
    return meta


@mcp.tool
async def artifact_list(limit: int = 100) -> list[dict]:
    """List stored artifacts (newest first)."""
    return await _backend().artifact_list(limit=limit)


# -------------------------------------------------------------------- admin
@mcp.tool
async def stats() -> dict:
    """Return store-wide counts (memory revisions/keys, sessions, events, artifacts, bytes)."""
    return await _backend().stats()
