"""FastMCP instance and the 22 tools.

The tools are thin: they validate/relay to the injected ``StorageBackend``.
The backend is set on ``deps`` during the FastAPI lifespan (one pool, injected),
so tools never open connections or read config themselves.

Tenancy: every per-project tool takes a required ``namespace`` (namespace ==
project == tenant) and the backend filters every query on it — there are no
implicit cross-project reads. Artifacts are content-addressed and global, and
``coord_drift_scan``/``stats`` are deliberately store-wide coordination/admin views.

Tool surface (22):
  memory:   memory_save, memory_get, memory_list, memory_history, memory_delete, memory_search
  handoff:  handoff_save, handoff_load, handoff_list
  session:  session_create, session_append_event, session_get, session_list, session_events
  artifact: artifact_put, artifact_get, artifact_list
  coord:    coord_health, coord_drift_scan, coord_reconcile, coord_curate
  admin:    stats
"""
from __future__ import annotations

import base64
import functools
import inspect
import time
from dataclasses import dataclass
from typing import Any

import structlog
from fastmcp import FastMCP

from config import settings
from storage.base import StorageBackend
from storage.versioning import stamp

log = structlog.get_logger("assist-memory.tools")


@dataclass
class Deps:
    backend: StorageBackend | None = None


deps = Deps()


def _backend() -> StorageBackend:
    if deps.backend is None:  # pragma: no cover - lifespan always sets this
        raise RuntimeError("storage backend not initialized")
    return deps.backend


def instrument(fn):
    """Telemetry + version stamping for every tool (Phase 1).

    Records one PHI-safe tool_events row per call (arguments pass through
    redact() — names/lengths/hashes only) and stamps dict-shaped responses with
    server_version/schema_version. Telemetry failure is logged and swallowed:
    it is observability, never part of the user's persistence ack, so it must
    never fail (or slow-fail) a tool call. Errors from the tool itself re-raise
    unchanged after being recorded.
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        start = time.monotonic()
        call_args = dict(sig.bind_partial(*args, **kwargs).arguments)
        outcome, error_code, remedy_emitted, result = "ok", None, False, None
        try:
            result = await fn(*args, **kwargs)
            if isinstance(result, dict):
                result = stamp(result)
            return result
        except Exception as exc:
            outcome = "error"
            # AppError (Phase 2) carries a machine code + remedy; anything else
            # falls back to the exception class name.
            error_code = getattr(exc, "code", None) or type(exc).__name__
            payload = getattr(exc, "payload", None)
            if isinstance(payload, dict):
                remedy_emitted = bool((payload.get("error") or {}).get("remedy"))
            raise
        finally:
            backend = deps.backend
            if backend is not None:
                try:
                    await backend.record_tool_event(
                        tool=fn.__name__, args=call_args, result=result,
                        outcome=outcome, error_code=error_code,
                        remedy_emitted=remedy_emitted,
                        latency_ms=int((time.monotonic() - start) * 1000),
                    )
                except Exception as tel_exc:  # noqa: BLE001 - observability only
                    log.warning("tool_event_record_failed", tool=fn.__name__,
                                error=str(tel_exc))

    return wrapper


mcp: FastMCP = FastMCP(name="assist-memory")


# ------------------------------------------------------------------ memory
@mcp.tool
@instrument
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
@instrument
async def memory_get(namespace: str, key: str) -> dict | None:
    """Return the latest live revision of a key in a namespace, or null if missing/deleted."""
    return await _backend().memory_get(namespace, key)


@mcp.tool
@instrument
async def memory_list(
    namespace: str, kind: str | None = None, tag: str | None = None, limit: int = 100
) -> list[dict]:
    """List the latest live entry per key in a namespace, optionally filtered by kind/tag."""
    return await _backend().memory_list(namespace, kind=kind, tag=tag, limit=limit)


@mcp.tool
@instrument
async def memory_history(namespace: str, key: str, limit: int = 50) -> list[dict]:
    """Return revision history (newest first) for a key in a namespace, including tombstones."""
    return await _backend().memory_history(namespace, key, limit=limit)


@mcp.tool
@instrument
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
@instrument
async def memory_search(namespace: str, query: str, limit: int = 20) -> list[dict]:
    """Search memory within ONE namespace (no cross-project reads).

    Ranks live entries by meaning using embeddings (pgvector cosine) and backfills
    keyword/substring matches. When no embedding provider is configured it degrades
    to pure substring search."""
    return await _backend().memory_search(namespace, query, limit=limit)


# ------------------------------------------------------------------ handoff
@mcp.tool
@instrument
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
@instrument
async def handoff_load(namespace: str, key: str) -> dict | None:
    """Load the latest handoff for a shared key in a namespace (written by any surface)."""
    return await _backend().handoff_load(namespace, key)


@mcp.tool
@instrument
async def handoff_list(namespace: str, limit: int = 100) -> list[dict]:
    """List active handoffs in a namespace."""
    return await _backend().handoff_list(namespace, limit=limit)


# ------------------------------------------------------------------ session
@mcp.tool
@instrument
async def session_create(namespace: str, surface: str | None = None, metadata: dict | None = None) -> dict:
    """Start an episodic session in a project namespace; returns its session_id."""
    return await _backend().session_create(namespace, surface=surface, metadata=metadata)


@mcp.tool
@instrument
async def session_append_event(namespace: str, session_id: str, kind: str, payload: Any) -> dict:
    """Append an ordered event to a session in this namespace; returns the assigned seq."""
    return await _backend().session_append_event(namespace, session_id, kind, payload)


@mcp.tool
@instrument
async def session_get(namespace: str, session_id: str) -> dict | None:
    """Fetch session metadata (scoped to the namespace)."""
    return await _backend().session_get(namespace, session_id)


@mcp.tool
@instrument
async def session_list(namespace: str, limit: int = 50) -> list[dict]:
    """List recent sessions in a namespace (newest first)."""
    return await _backend().session_list(namespace, limit=limit)


@mcp.tool
@instrument
async def session_events(namespace: str, session_id: str, limit: int = 200) -> list[dict]:
    """Return a session's events in seq order (scoped to the namespace)."""
    return await _backend().session_events(namespace, session_id, limit=limit)


# ----------------------------------------------------------------- artifact
@mcp.tool
@instrument
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
@instrument
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
@instrument
async def artifact_list(limit: int = 100) -> list[dict]:
    """List stored artifacts (newest first)."""
    return await _backend().artifact_list(limit=limit)


# ------------------------------------------------------------- coordination
@mcp.tool
@instrument
async def coord_health(namespace: str, limit: int = 200) -> dict:
    """Drift report for ONE namespace, computed from stored provenance (no git
    required): `stale` entries whose repo_sha is behind the namespace's latest,
    `duplicate_content` (distinct keys holding an identical fact), and
    `claim_collisions` (multiple live claims about the same subject/PR). Read it
    at session start to see what needs re-verifying before trusting the store."""
    return await _backend().coord_health(namespace, limit=limit)


@mcp.tool
@instrument
async def coord_drift_scan(limit: int = 50) -> dict:
    """Store-wide scan for the same fact living under more than one namespace
    (namespace drift, e.g. a project split across two namespaces). Like `stats`,
    this is a deliberately cross-tenant coordination/admin view, not a per-project
    read. Returns content hashes that span >1 namespace, worst first."""
    return await _backend().coord_drift_scan(limit=limit)


@mcp.tool
@instrument
async def coord_reconcile(namespace: str, limit: int = 100) -> dict:
    """Reconcile every live claim in a namespace against GitHub and record an
    append-only verdict (current | stale | unverifiable) per claim under
    coord/_reconcile/<key> — the user's entry is never rewritten. Resolution is
    derived from each claim's provenance (meta.repo + meta.pr / meta.branch), not
    its prose. When the backend has no GitHub token the resolver is disabled and
    every verdict is `unverifiable` (never silently `current`). Run it at session
    start to learn which claims need re-verifying."""
    return await _backend().coord_reconcile(namespace, limit=limit)


@mcp.tool
@instrument
async def coord_curate(namespace: str, session_id: str, dry_run: bool = False) -> dict:
    """Pull-triggered, write-side LLM curation of a finished session. Reads the
    session's execution trace plus similar existing memories, asks the curator what
    is worth persisting, and (unless dry_run) applies the resulting operations
    deterministically: ADD/UPDATE/MERGE/SUPERSEDE/NOOP. Every op passes a fail-closed
    PHI gate first, claims without provenance (meta.repo + meta.pr/branch) are
    downgraded to notes, supersession sets a validity boundary (history is kept, never
    deleted), and writes are idempotent so re-running the same session never
    double-writes. When the curator is disabled (no Anthropic key) it returns
    {curator_enabled: false, operations: []} — a clear no-op, never a guess. Run it at
    session end to consolidate durable lessons. dry_run=True returns the proposed
    operations without writing."""
    return await _backend().coord_curate(namespace, session_id, dry_run=dry_run)


# -------------------------------------------------------------------- admin
@mcp.tool
@instrument
async def stats() -> dict:
    """Return store-wide counts (memory revisions/keys, sessions, events, artifacts, bytes)."""
    return await _backend().stats()
