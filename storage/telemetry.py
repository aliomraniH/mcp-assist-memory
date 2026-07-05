"""Telemetry spine (Phase 1, T1.1): build PHI-safe ``tool_events`` rows.

The row builder is pure (testable without a DB); ``PostgresBackend.
record_tool_event`` inserts the row. Argument values pass through ``redact()``
ONLY — names, lengths, hashes; never content (global ground rule 5). Screening
hits record pattern NAMES, never matched text.

Telemetry is observability, not persistence of user data: a failed telemetry
insert is logged and swallowed at the call site so it can never fail a tool
call — the fail-closed rule protects the user's write ack, which telemetry is
not part of.
"""
from __future__ import annotations

import json
from typing import Any

from storage.redact import redact
from storage.versioning import SCHEMA_VERSION, SERVER_VERSION

# Keys copied from a dict-shaped tool result into telemetry columns. These are
# self-describing response fields (booleans / names / counters), never content.
_OUTCOME_ROWS = ("ok", "dedup_replay", "error", "quarantined",
                 "unknown_arg_accepted", "unknown_arg_rejected")


def _advisory_names(result: dict) -> list[str] | None:
    """Advisory NAMES only. Advisories in responses are dicts with a `name` (or
    plain strings); anything else is ignored rather than risking content."""
    advisories = result.get("advisories")
    if not isinstance(advisories, list):
        return None
    names = []
    for a in advisories:
        if isinstance(a, str):
            names.append(a)
        elif isinstance(a, dict) and isinstance(a.get("name"), str):
            names.append(a["name"])
    return names or None


def build_event_row(
    *,
    tool: str,
    args: dict[str, Any],
    result: Any = None,
    outcome: str = "ok",
    error_code: str | None = None,
    remedy_emitted: bool = False,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    """Assemble one PHI-safe tool_events row (column name → value)."""
    arg_names, arg_value_meta = redact(args)
    r = result if isinstance(result, dict) else {}

    if outcome == "ok":
        if r.get("deduplicated") is True:
            outcome = "dedup_replay"
        elif r.get("quarantined") is True:
            outcome = "quarantined"
    assert outcome in _OUTCOME_ROWS, f"unknown outcome {outcome!r}"

    result_bytes = None
    if result is not None:
        try:
            result_bytes = len(json.dumps(result, default=str))
        except (TypeError, ValueError):
            result_bytes = None

    screening = r.get("screening")
    return {
        "namespace": args.get("namespace"),
        "tool": tool,
        "actor": args.get("actor"),
        "session_id": str(args["session_id"]) if args.get("session_id") else None,
        "arg_names": arg_names,
        "arg_value_meta": arg_value_meta,
        "variant_profile": r.get("variant_profile"),
        "server_version": SERVER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "error_code": error_code,
        "remedy_emitted": remedy_emitted,
        "advisories": _advisory_names(r),
        "advisory_status": r.get("advisory_status"),
        "screening_patterns": list(screening) if isinstance(screening, list) else None,
        "dedup": r.get("deduplicated"),
        "verified_persisted": r.get("verified_persisted"),
        "latency_ms": latency_ms,
        "readback_latency_ms": r.get("readback_latency_ms"),
        "result_bytes": result_bytes,
        "truncated": r.get("truncated"),
    }
