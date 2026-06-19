"""FastMCP app and tool definitions: thin validation → StorageBackend calls."""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import secrets as _secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .auth import BearerAuthMiddleware
from .config import MB, Config
from .observability import AccessLogMiddleware, logged
from .models import (
    ARTIFACT_CHUNK_BYTES,
    ARTIFACT_GET_MODES,
    BINARY_NOT_TEXT,
    ENCODINGS,
    HANDOFF_KEY,
    INVALID_ARGUMENT,
    KINDS,
    MAX_VALUE_BYTES,
    NOT_FOUND,
    SESSION_EXISTS,
    SESSION_STATUSES,
    SURFACES,
    UPLOAD_TOO_LARGE,
    Artifact,
    MemoryRevision,
    ToolFault,
    map_surface,
    now_iso,
    parse_iso,
    preview,
    slugify,
    validate_enum,
    validate_key,
    validate_namespace,
    validate_tags,
)
from .secrets_scan import POSSIBLE_SECRET_TAG, scan_text, secret_warning
from .storage.base import StorageBackend
from .storage.sqlite_fs import SqliteFsBackend
from .zip_ingest import check_zip_safety, inspect_debug_capture, is_zip


def _encode_value(value: Any) -> tuple[str | None, bool]:
    """Strings store as text; everything else stores as JSON."""
    if value is None:
        raise ToolFault(INVALID_ARGUMENT, "value must not be null")
    if isinstance(value, str):
        return value, False
    try:
        return json.dumps(value, ensure_ascii=False), True
    except (TypeError, ValueError) as e:
        raise ToolFault(INVALID_ARGUMENT, f"value is not JSON-serializable: {e}")


def _revision_meta(e: MemoryRevision, with_preview: bool = False) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "key": e.key,
        "kind": e.kind,
        "tags": e.tags,
        "revision": e.revision,
        "source_surface": e.source_surface,
        "created_at": e.created_at,
    }
    if with_preview:
        meta["value_preview"] = preview(e.value)
    return meta


def build_mcp(config: Config, backend: StorageBackend) -> FastMCP:
    mcp = FastMCP(
        "assist-memory",
        instructions=(
            "Shared memory, session, handoff, and artifact store for Claude "
            "surfaces (web/cli/desktop). Memory entries are append-only with "
            "full revision history per (namespace, key)."
        ),
        stateless_http=True,
        json_response=True,
        # Host-header (DNS-rebinding) checks are disabled: the public Replit
        # hostname isn't known at build time and every request is bearer-authed.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    def _scan_and_tag(text: str, tags: list[str], warnings: list[str]) -> None:
        hits = scan_text(text)
        if hits:
            if POSSIBLE_SECRET_TAG not in tags:
                tags.append(POSSIBLE_SECRET_TAG)
            warnings.append(secret_warning(hits))

    async def _save_memory(
        namespace: str | None,
        key: str,
        value: Any,
        kind: str | None,
        tags: list[str] | None,
        source_surface: str | None,
    ) -> dict[str, Any]:
        ns = validate_namespace(namespace)
        key = validate_key(key)
        kind_v = validate_enum(kind, KINDS, "kind", "note")
        surface = validate_enum(source_surface, SURFACES, "source_surface", "other")
        tag_list = validate_tags(tags)
        stored, is_json = _encode_value(value)
        size = len(stored.encode("utf-8"))
        if size > MAX_VALUE_BYTES:
            raise ToolFault(
                INVALID_ARGUMENT,
                f"value is {size} bytes; memory values are capped at "
                f"{MAX_VALUE_BYTES} bytes — use artifact_upload for larger payloads",
            )
        warnings: list[str] = []
        _scan_and_tag(stored, tag_list, warnings)
        await backend.ensure_capacity(size)
        rev = await backend.save_revision(ns, key, stored, is_json, kind_v, tag_list, surface)
        return {
            "namespace": ns,
            "key": key,
            "revision": rev.revision,
            "created_at": rev.created_at,
            "warnings": warnings,
        }

    # ------------------------------------------------------------ memory

    @mcp.tool()
    @logged
    async def memory_save(
        key: str,
        value: Any,
        namespace: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        source_surface: str | None = None,
    ) -> dict[str, Any]:
        """Save a memory entry. Append-only: every call creates a new revision."""
        return await _save_memory(namespace, key, value, kind, tags, source_surface)

    @mcp.tool()
    @logged
    async def memory_get(
        key: str, namespace: str | None = None, revision: int | None = None
    ) -> dict[str, Any]:
        """Get a memory entry. Omit revision for the latest; tombstoned keys are not found."""
        ns = validate_namespace(namespace)
        key = validate_key(key)
        if revision is not None and revision < 1:
            raise ToolFault(INVALID_ARGUMENT, "revision must be >= 1")
        row = await backend.get_revision(ns, key, revision)
        if row is None or (revision is None and row.deleted):
            raise ToolFault(
                NOT_FOUND,
                f"memory entry {key!r}"
                + (f" revision {revision}" if revision is not None else "")
                + f" not found in namespace {ns!r}",
            )
        return {
            "namespace": ns,
            "key": key,
            "revision": row.revision,
            "kind": row.kind,
            "value": row.decoded_value(),
            "tags": row.tags,
            "source_surface": row.source_surface,
            "created_at": row.created_at,
            "deleted": row.deleted,
        }

    @mcp.tool()
    @logged
    async def memory_list(
        namespace: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
        prefix: str | None = None,
    ) -> dict[str, Any]:
        """List memory entries (metadata only, no values). Filters are AND-ed."""
        ns = validate_namespace(namespace)
        if kind is not None:
            validate_enum(kind, KINDS, "kind", "note")
        entries = await backend.list_entries(ns, kind, tag, prefix)
        return {
            "namespace": ns,
            "entries": [_revision_meta(e) for e in entries],
            "count": len(entries),
        }

    @mcp.tool()
    @logged
    async def memory_search(query: str, namespace: str | None = None) -> dict[str, Any]:
        """Case-insensitive substring search over keys, tags, and values."""
        ns = validate_namespace(namespace)
        if not query:
            raise ToolFault(INVALID_ARGUMENT, "query must be a non-empty string")
        results = await backend.search_entries(ns, query)
        return {
            "namespace": ns,
            "results": [_revision_meta(e, with_preview=True) for e in results],
            "count": len(results),
        }

    @mcp.tool()
    @logged
    async def memory_history(key: str, namespace: str | None = None) -> dict[str, Any]:
        """All revisions of a key (ascending), including tombstones."""
        ns = validate_namespace(namespace)
        key = validate_key(key)
        history = await backend.get_history(ns, key)
        if not history:
            raise ToolFault(NOT_FOUND, f"memory entry {key!r} not found in namespace {ns!r}")
        return {
            "namespace": ns,
            "key": key,
            "revisions": [
                {
                    "revision": e.revision,
                    "created_at": e.created_at,
                    "source_surface": e.source_surface,
                    "kind": e.kind,
                    "deleted": e.deleted,
                    "value_preview": preview(e.value),
                }
                for e in history
            ],
            "count": len(history),
        }

    @mcp.tool()
    @logged
    async def memory_revert(
        key: str, to_revision: int, namespace: str | None = None
    ) -> dict[str, Any]:
        """Non-destructive undo: creates a NEW revision copying an older one's value."""
        ns = validate_namespace(namespace)
        key = validate_key(key)
        target = await backend.get_revision(ns, key, to_revision)
        if target is None:
            raise ToolFault(
                NOT_FOUND, f"revision {to_revision} of {key!r} not found in namespace {ns!r}"
            )
        if target.deleted:
            raise ToolFault(
                INVALID_ARGUMENT, f"revision {to_revision} is a tombstone; cannot revert to it"
            )
        await backend.ensure_capacity(len((target.value or "").encode("utf-8")))
        rev = await backend.save_revision(
            ns, key, target.value, target.value_is_json, target.kind,
            target.tags, target.source_surface,
        )
        return {
            "namespace": ns,
            "key": key,
            "revision": rev.revision,
            "reverted_to": to_revision,
        }

    @mcp.tool()
    @logged
    async def memory_delete(key: str, namespace: str | None = None) -> dict[str, Any]:
        """Tombstone delete: appends a delete revision; history is preserved."""
        ns = validate_namespace(namespace)
        key = validate_key(key)
        latest = await backend.get_revision(ns, key, None)
        if latest is None or latest.deleted:
            raise ToolFault(NOT_FOUND, f"memory entry {key!r} not found in namespace {ns!r}")
        rev = await backend.save_revision(
            ns, key, None, False, latest.kind, [], latest.source_surface, deleted=True
        )
        return {"namespace": ns, "key": key, "revision": rev.revision, "deleted": True}

    # ------------------------------------------------------------ sessions

    @mcp.tool()
    @logged
    async def session_start(
        surface: str,
        namespace: str | None = None,
        label: str | None = None,
        summary: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a work session. Generated ids follow YYYY-MM-DDTHH-MM-SSZ_<label>."""
        ns = validate_namespace(namespace)
        surface_v = validate_enum(surface, SURFACES, "surface", "other")
        if session_id is not None:
            if not session_id or len(session_id) > 256:
                raise ToolFault(INVALID_ARGUMENT, "session_id must be 1-256 characters")
            if await backend.get_session(session_id) is not None:
                raise ToolFault(SESSION_EXISTS, f"session {session_id!r} already exists")
            sid = session_id
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
            base = f"{stamp}_{slugify(label or 'session')}"
            sid = base
            while await backend.get_session(sid) is not None:
                sid = f"{base}-{_secrets.token_hex(2)}"
        session = await backend.create_session(sid, ns, surface_v, summary=summary)
        return {
            "session_id": session.session_id,
            "namespace": ns,
            "surface": surface_v,
            "status": session.status,
            "created_at": session.created_at,
        }

    @mcp.tool()
    @logged
    async def session_log(
        session_id: str, type: str, message: str, data: Any = None
    ) -> dict[str, Any]:
        """Append an event to an open session."""
        if not type:
            raise ToolFault(INVALID_ARGUMENT, "type must be a non-empty string")
        seq, timestamp = await backend.append_event(session_id, type, message, data)
        return {"session_id": session_id, "seq": seq, "timestamp": timestamp}

    @mcp.tool()
    @logged
    async def session_end(session_id: str, summary: str) -> dict[str, Any]:
        """Close a session and set its final summary."""
        session = await backend.close_session(session_id, summary)
        return {
            "session_id": session.session_id,
            "status": session.status,
            "ended_at": session.ended_at,
            "event_count": session.event_count,
        }

    @mcp.tool()
    @logged
    async def session_list(
        namespace: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List sessions, newest first."""
        ns = validate_namespace(namespace)
        if status is not None:
            validate_enum(status, SESSION_STATUSES, "status", "open")
        lim = 20 if limit is None else max(1, min(limit, 200))
        sessions = await backend.list_sessions(ns, status, lim)
        return {
            "namespace": ns,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "namespace": s.namespace,
                    "surface": s.surface,
                    "status": s.status,
                    "summary": s.summary,
                    "created_at": s.created_at,
                    "ended_at": s.ended_at,
                    "event_count": s.event_count,
                }
                for s in sessions
            ],
            "count": len(sessions),
        }

    @mcp.tool()
    @logged
    async def session_get(session_id: str) -> dict[str, Any]:
        """Full session record including ordered events and linked artifacts."""
        session = await backend.get_session(session_id)
        if session is None:
            raise ToolFault(NOT_FOUND, f"session {session_id!r} not found")
        linked = await backend.list_artifacts(session_id=session_id)
        return {
            "session_id": session.session_id,
            "namespace": session.namespace,
            "surface": session.surface,
            "status": session.status,
            "summary": session.summary,
            "created_at": session.created_at,
            "ended_at": session.ended_at,
            "events": [
                {
                    "seq": e.seq,
                    "timestamp": e.timestamp,
                    "type": e.type,
                    "message": e.message,
                    "data": e.data,
                }
                for e in session.events
            ],
            "artifacts": [
                {
                    "artifact_id": a.artifact_id,
                    "filename": a.filename,
                    "mime": a.mime,
                    "size_bytes": a.size_bytes,
                    "uploaded_at": a.uploaded_at,
                }
                for a in linked
            ],
        }

    # ------------------------------------------------------------ handoff

    @mcp.tool()
    @logged
    async def handoff_save(
        from_surface: str,
        content: str,
        namespace: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a cross-surface handoff (memory entry 'handoff/latest', kind=handoff)."""
        validate_enum(from_surface, SURFACES, "from_surface", "other")
        if not content:
            raise ToolFault(INVALID_ARGUMENT, "content must be a non-empty string")
        if session_id is not None and await backend.get_session(session_id) is None:
            raise ToolFault(INVALID_ARGUMENT, f"unknown session_id {session_id!r}")
        result = await _save_memory(
            namespace,
            HANDOFF_KEY,
            {"content": content, "session_id": session_id, "saved_at": now_iso()},
            "handoff",
            None,
            from_surface,
        )
        return {
            "namespace": result["namespace"],
            "key": HANDOFF_KEY,
            "revision": result["revision"],
            "warnings": result["warnings"],
        }

    @mcp.tool()
    @logged
    async def handoff_load(namespace: str | None = None) -> dict[str, Any]:
        """Load the latest handoff plus its revision history pointer for backtracking."""
        ns = validate_namespace(namespace)
        latest = await backend.get_revision(ns, HANDOFF_KEY, None)
        if latest is None or latest.deleted:
            raise ToolFault(NOT_FOUND, f"no handoff saved in namespace {ns!r}")
        value = latest.decoded_value()
        if not isinstance(value, dict):
            value = {"content": value, "session_id": None, "saved_at": latest.created_at}
        history = await backend.get_history(ns, HANDOFF_KEY)
        return {
            "namespace": ns,
            "content": value.get("content"),
            "from_surface": latest.source_surface,
            "session_id": value.get("session_id"),
            "saved_at": value.get("saved_at"),
            "revision": latest.revision,
            "history": [
                {
                    "revision": e.revision,
                    "created_at": e.created_at,
                    "source_surface": e.source_surface,
                    "value_preview": preview(e.value),
                }
                for e in history
            ],
        }

    # ------------------------------------------------------------ artifacts

    async def _ingest_debug_capture(
        ns: str, data: bytes, warnings: list[str]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Returns (debug_capture response field, session_id to link)."""
        zf = check_zip_safety(data, config.max_zip_decompressed_bytes)
        capture, inspect_warnings = inspect_debug_capture(zf)
        warnings.extend(inspect_warnings)
        if capture is None:
            return None, None

        sid = capture.session_id
        meta = capture.session_json.get("metadata") or {}
        surface = map_surface(meta.get("claude_surface"))
        summary = capture.results_summary or "debug-capture import"
        existing = await backend.get_session(sid)
        if existing is None:
            await backend.create_session(
                sid,
                ns,
                surface,
                status="closed",
                summary=summary,
                created_at=parse_iso(meta.get("created_at")) or now_iso(),
                ended_at=parse_iso(meta.get("ended_at")) or now_iso(),
            )
        else:
            await backend.update_session_import(sid, summary)

        brief_key = None
        if capture.brief_text is not None:
            brief_key = f"debug/{sid}/brief"
            tags = ["debug-capture"]
            _scan_and_tag(capture.brief_text, tags, warnings)
            await backend.ensure_capacity(len(capture.brief_text.encode("utf-8")))
            await backend.save_revision(
                ns, brief_key, capture.brief_text, False, "handoff", tags, surface
            )

        return (
            {
                "recognized": True,
                "session_id": sid,
                "session_created": existing is None,
                "brief_memory_key": brief_key,
                "results_summary": summary,
            },
            sid,
        )

    @mcp.tool()
    @logged
    async def artifact_upload(
        filename: str,
        content: str,
        encoding: str,
        namespace: str | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
        source_surface: str | None = None,
    ) -> dict[str, Any]:
        """Upload an artifact (text, json, or base64 binary). ZIPs are safety-checked;
        debug-capture session exports are auto-ingested."""
        ns = validate_namespace(namespace)
        encoding_v = validate_enum(encoding, ENCODINGS, "encoding", "text")
        surface = validate_enum(source_surface, SURFACES, "source_surface", "other")
        tag_list = validate_tags(tags)
        name = PurePosixPath(filename.replace("\\", "/")).name
        if not name or name in (".", ".."):
            raise ToolFault(INVALID_ARGUMENT, f"invalid filename {filename!r}")
        if session_id is not None and await backend.get_session(session_id) is None:
            raise ToolFault(INVALID_ARGUMENT, f"unknown session_id {session_id!r}")

        if encoding_v == "text":
            data = content.encode("utf-8")
        elif encoding_v == "json":
            try:
                data = json.dumps(
                    json.loads(content), ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            except json.JSONDecodeError as e:
                raise ToolFault(INVALID_ARGUMENT, f"content is not valid JSON: {e}")
        else:
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ToolFault(INVALID_ARGUMENT, f"content is not valid base64: {e}")

        if len(data) > config.max_upload_bytes:
            raise ToolFault(
                UPLOAD_TOO_LARGE,
                f"decoded upload is {len(data)} bytes; the per-upload limit is "
                f"{config.max_upload_mb} MB",
            )
        await backend.ensure_capacity(len(data))

        warnings: list[str] = []
        if encoding_v in ("text", "json"):
            _scan_and_tag(content, tag_list, warnings)

        debug_capture = None
        link_session = session_id
        is_capture = False
        if is_zip(data):
            debug_capture, capture_sid = await _ingest_debug_capture(ns, data, warnings)
            if capture_sid is not None:
                link_session = capture_sid
                is_capture = True

        if encoding_v == "json":
            mime = "application/json"
        elif is_zip(data):
            mime = "application/zip"
        else:
            mime = mimetypes.guess_type(name)[0] or (
                "text/plain" if encoding_v == "text" else "application/octet-stream"
            )

        artifact = await backend.store_artifact(
            Artifact(
                artifact_id=f"art_{_secrets.token_hex(6)}",
                namespace=ns,
                filename=name,
                mime=mime,
                size_bytes=len(data),
                sha256="",
                uploaded_at=now_iso(),
                source_surface=surface,
                session_id=link_session,
                memory_key=debug_capture["brief_memory_key"] if debug_capture else None,
                tags=tag_list,
                storage_path="",
                is_debug_capture=is_capture,
            ),
            data,
        )
        return {
            "artifact_id": artifact.artifact_id,
            "namespace": ns,
            "filename": artifact.filename,
            "mime": artifact.mime,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "uploaded_at": artifact.uploaded_at,
            "session_id": artifact.session_id,
            "debug_capture": debug_capture,
            "warnings": warnings,
        }

    @mcp.tool()
    @logged
    async def artifact_list(
        namespace: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """List artifact metadata, optionally filtered by session."""
        ns = validate_namespace(namespace)
        artifacts = await backend.list_artifacts(ns, session_id)
        return {
            "namespace": ns,
            "artifacts": [a.public_meta() for a in artifacts],
            "count": len(artifacts),
        }

    @mcp.tool()
    @logged
    async def artifact_get(
        artifact_id: str,
        mode: str | None = None,
        offset: int | None = None,
        length: int | None = None,
    ) -> dict[str, Any]:
        """Fetch artifact metadata or content. Content modes return at most 1 MB per
        call; page through larger files with offset/length until eof is true."""
        mode_v = validate_enum(mode, ARTIFACT_GET_MODES, "mode", "metadata")
        artifact = await backend.get_artifact(artifact_id)
        if artifact is None:
            raise ToolFault(NOT_FOUND, f"artifact {artifact_id!r} not found")
        result = artifact.public_meta()
        if mode_v == "metadata":
            return result

        start = offset or 0
        if start < 0:
            raise ToolFault(INVALID_ARGUMENT, "offset must be >= 0")
        if start > 0 and start >= artifact.size_bytes:
            raise ToolFault(
                INVALID_ARGUMENT,
                f"offset {start} is past the end of the artifact "
                f"({artifact.size_bytes} bytes)",
            )
        if length is not None and not (1 <= length <= ARTIFACT_CHUNK_BYTES):
            raise ToolFault(
                INVALID_ARGUMENT,
                f"length must be between 1 and {ARTIFACT_CHUNK_BYTES} bytes per call; "
                "page through larger files with offset",
            )
        want = length if length is not None else min(
            ARTIFACT_CHUNK_BYTES, max(artifact.size_bytes - start, 0)
        )
        chunk = await backend.read_artifact_bytes(artifact_id, start, want) if want else b""

        if mode_v == "text":
            try:
                content = chunk.decode("utf-8")
            except UnicodeDecodeError:
                raise ToolFault(
                    BINARY_NOT_TEXT,
                    "selected range is not valid UTF-8 text (binary content, or the "
                    "range boundary splits a multi-byte character); use base64 mode",
                )
        else:
            content = base64.b64encode(chunk).decode("ascii")

        result.update(
            {
                "content": content,
                "encoding": mode_v,
                "offset": start,
                "length": len(chunk),
                "eof": start + len(chunk) >= artifact.size_bytes,
            }
        )
        return result

    # ------------------------------------------------------------ meta

    @mcp.tool()
    @logged
    async def server_status() -> dict[str, Any]:
        """Storage usage, entity counts, version, and configured limits."""
        usage = await backend.usage()
        # The Postgres backend has no data_dir; report the VM filesystem's free
        # space so the return shape stays identical across backends.
        probe = config.data_dir if config.data_dir.exists() else Path("/")
        free = shutil.disk_usage(probe).free
        return {
            "version": __version__,
            "storage": {
                "used_mb": round(usage.used_bytes / MB, 2),
                "limit_mb": config.max_total_storage_mb,
                "data_dir_free_mb": round(free / MB, 1),
            },
            "counts": {
                "memory_keys": usage.memory_keys,
                "memory_revisions": usage.memory_revisions,
                "sessions": usage.sessions,
                "open_sessions": usage.open_sessions,
                "artifacts": usage.artifacts,
            },
            "limits": {
                "max_upload_mb": config.max_upload_mb,
                "max_total_storage_mb": config.max_total_storage_mb,
            },
        }

    return mcp


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(config: Config, backend: StorageBackend | None = None) -> Starlette:
    """Full ASGI app: bearer auth → health route + Streamable HTTP MCP at /mcp."""
    backend = backend or SqliteFsBackend(config.data_dir, config.max_total_storage_bytes)
    mcp = build_mcp(config, backend)
    app = mcp.streamable_http_app()
    app.router.routes.insert(0, Route("/", _health, methods=["GET"]))
    # Outermost first: access log sees every request, including 401s.
    app.add_middleware(BearerAuthMiddleware, token=config.auth_token)
    app.add_middleware(AccessLogMiddleware)
    return app
