"""ZIP safety checks and debug-capture session-export inspection (SPEC §6, §7.2)."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field

from .models import ZIP_INNER_FILE_CAP, ZIP_MAX_ENTRIES, ZIP_UNSAFE, ToolFault

ZIP_MAGIC = b"PK\x03\x04"
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_S_IFLNK = 0o120000
_S_IFMT = 0o170000


def is_zip(data: bytes) -> bool:
    return data[:4] == ZIP_MAGIC


def check_zip_safety(data: bytes, max_decompressed_bytes: int) -> zipfile.ZipFile:
    """Validate the archive before anything is read from it; returns the open ZipFile."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, NotImplementedError) as e:
        raise ToolFault(ZIP_UNSAFE, f"not a readable zip archive: {e}")

    if len(infos) > ZIP_MAX_ENTRIES:
        raise ToolFault(
            ZIP_UNSAFE, f"zip has {len(infos)} entries (limit {ZIP_MAX_ENTRIES})"
        )

    declared_total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or _DRIVE_LETTER.match(name):
            raise ToolFault(ZIP_UNSAFE, f"zip entry has an absolute path: {name!r}")
        if ".." in name.replace("\\", "/").split("/"):
            raise ToolFault(ZIP_UNSAFE, f"zip entry has a path-traversal name: {name!r}")
        if (info.external_attr >> 16) & _S_IFMT == _S_IFLNK:
            raise ToolFault(ZIP_UNSAFE, f"zip entry is a symlink: {name!r}")
        declared_total += info.file_size

    if declared_total > max_decompressed_bytes:
        raise ToolFault(
            ZIP_UNSAFE,
            f"declared decompressed size {declared_total} bytes exceeds the "
            f"{max_decompressed_bytes}-byte cap",
        )
    return zf


def _read_capped(zf: zipfile.ZipFile, name: str, cap: int = ZIP_INNER_FILE_CAP) -> bytes:
    """Stream a single member with a hard byte cap, defending against lying headers."""
    out = io.BytesIO()
    with zf.open(name) as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)
            if out.tell() > cap:
                raise ToolFault(
                    ZIP_UNSAFE,
                    f"zip member {name!r} decompresses past the {cap}-byte cap",
                )
    return out.getvalue()


@dataclass
class DebugCapture:
    session_json: dict
    brief_text: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def session_id(self) -> str:
        return self.session_json["session_id"]

    @property
    def results_summary(self) -> str | None:
        results = self.session_json.get("results")
        if isinstance(results, dict) and isinstance(results.get("summary"), str):
            return results["summary"]
        return None


def inspect_debug_capture(
    zf: zipfile.ZipFile,
) -> tuple[DebugCapture | None, list[str]]:
    """Look for session.json (schema_version "1.0") at root or one level deep.

    Returns (capture, warnings). A malformed session.json never fails the
    upload — it just isn't recognized, with a warning.
    """
    names = set(zf.namelist())
    candidates = [
        n
        for n in sorted(names)
        if n == "session.json" or (n.endswith("/session.json") and n.count("/") == 1)
    ]
    if not candidates:
        return None, []

    warnings: list[str] = []
    for candidate in candidates:
        try:
            payload = json.loads(_read_capped(zf, candidate).decode("utf-8"))
        except ToolFault:
            raise
        except (json.JSONDecodeError, UnicodeDecodeError):
            warnings.append(
                f"{candidate} is present but not valid JSON; stored as a plain artifact"
            )
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "1.0"
            or not isinstance(payload.get("session_id"), str)
            or not payload["session_id"]
        ):
            warnings.append(
                f"{candidate} is present but not a recognized debug-capture export "
                "(need schema_version '1.0' and session_id); stored as a plain artifact"
            )
            continue

        prefix = candidate[: -len("session.json")]
        brief_name = f"{prefix}agent-handoff/brief.md"
        brief_text: str | None = None
        if brief_name in names:
            try:
                brief_text = _read_capped(zf, brief_name).decode("utf-8")
            except UnicodeDecodeError:
                warnings.append(f"{brief_name} is not valid UTF-8; brief not indexed")
        return DebugCapture(payload, brief_text, warnings), warnings

    return None, warnings
