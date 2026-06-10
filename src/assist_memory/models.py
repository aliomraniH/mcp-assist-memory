"""Shared types, validation helpers, and the tool error contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

KINDS = ("note", "decision", "todo", "handoff", "config")
SURFACES = ("web", "cli", "desktop", "other")
SESSION_STATUSES = ("open", "closed")
ENCODINGS = ("text", "json", "base64")
ARTIFACT_GET_MODES = ("metadata", "text", "base64")

DEFAULT_NAMESPACE = "default"
NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_KEY_LEN = 256
MAX_VALUE_BYTES = 256 * 1024
ARTIFACT_CHUNK_BYTES = 1024 * 1024
ZIP_MAX_ENTRIES = 2000
ZIP_INNER_FILE_CAP = 4 * 1024 * 1024
HANDOFF_KEY = "handoff/latest"

# Error codes (SPEC §5)
INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_FOUND = "NOT_FOUND"
SESSION_CLOSED = "SESSION_CLOSED"
SESSION_EXISTS = "SESSION_EXISTS"
UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
STORAGE_FULL = "STORAGE_FULL"
ZIP_UNSAFE = "ZIP_UNSAFE"
BINARY_NOT_TEXT = "BINARY_NOT_TEXT"


class ToolFault(Exception):
    """Domain error surfaced to the MCP client as {"code", "message", ...}."""

    def __init__(self, code: str, message: str, **extra: Any):
        self.code = code
        self.message = message
        self.extra = extra
        payload = {"code": code, "message": message, **extra}
        super().__init__(json.dumps(payload))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> str | None:
    """Best-effort normalization of an external ISO timestamp; None if unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_namespace(namespace: str | None) -> str:
    ns = namespace if namespace is not None else DEFAULT_NAMESPACE
    if not NAMESPACE_RE.match(ns):
        raise ToolFault(
            INVALID_ARGUMENT,
            f"invalid namespace {ns!r}: must match {NAMESPACE_RE.pattern}",
        )
    return ns


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not (1 <= len(key) <= MAX_KEY_LEN):
        raise ToolFault(
            INVALID_ARGUMENT, f"key must be a string of 1-{MAX_KEY_LEN} characters"
        )
    if not key.isprintable():
        raise ToolFault(INVALID_ARGUMENT, "key must contain only printable characters")
    return key


def validate_enum(value: str | None, allowed: tuple[str, ...], name: str, default: str) -> str:
    v = value if value is not None else default
    if v not in allowed:
        raise ToolFault(
            INVALID_ARGUMENT, f"invalid {name} {v!r}: must be one of {', '.join(allowed)}"
        )
    return v


def validate_tags(tags: list[str] | None) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ToolFault(INVALID_ARGUMENT, "tags must be a list of strings")
    return list(tags)


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    return slug or "session"


def map_surface(value: Any) -> str:
    """Map an external surface name onto our enum, defaulting to 'other'."""
    if isinstance(value, str) and value.lower() in SURFACES:
        return value.lower()
    return "other"


def preview(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class MemoryRevision:
    namespace: str
    key: str
    revision: int
    kind: str
    value: str | None  # raw stored text (JSON-encoded when value_is_json)
    value_is_json: bool
    tags: list[str]
    source_surface: str
    deleted: bool
    created_at: str

    def decoded_value(self) -> Any:
        if self.value is None:
            return None
        return json.loads(self.value) if self.value_is_json else self.value


@dataclass
class SessionEvent:
    seq: int
    timestamp: str
    type: str
    message: str
    data: Any | None


@dataclass
class Session:
    session_id: str
    namespace: str
    surface: str
    status: str
    summary: str | None
    created_at: str
    ended_at: str | None
    events: list[SessionEvent] = field(default_factory=list)
    event_count: int = 0


@dataclass
class Artifact:
    artifact_id: str
    namespace: str
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    uploaded_at: str
    source_surface: str
    session_id: str | None
    memory_key: str | None
    tags: list[str]
    storage_path: str
    is_debug_capture: bool

    def public_meta(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "namespace": self.namespace,
            "filename": self.filename,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "uploaded_at": self.uploaded_at,
            "source_surface": self.source_surface,
            "session_id": self.session_id,
            "memory_key": self.memory_key,
            "tags": self.tags,
            "is_debug_capture": self.is_debug_capture,
        }


@dataclass
class StorageUsage:
    used_bytes: int
    memory_keys: int
    memory_revisions: int
    sessions: int
    open_sessions: int
    artifacts: int
