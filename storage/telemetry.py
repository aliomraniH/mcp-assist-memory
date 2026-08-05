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


# Closed, low-cardinality dimension for WHY a call terminated badly. Raw
# exception messages must never become a dimension: unbounded cardinality, they
# drift with every reword, and they are the likeliest place for user content to
# leak into telemetry. Anything unrecognised collapses to 'internal'.
ERROR_TYPES = ("confirm_mismatch", "unresolved_conflict_destructive",
               "idempotency_conflict", "intent_mismatch", "quarantined",
               "internal")

# Gate rule / error code -> error_type. The gate names its own rule; this map
# only narrows those names to the fixed enum.
_ERROR_TYPE_BY_RULE = {
    "confirm_mismatch": "confirm_mismatch",
    "unresolved_conflict_destructive": "unresolved_conflict_destructive",
    "idempotency_conflict": "idempotency_conflict",
    "intent_mismatch": "intent_mismatch",
    "quarantined": "quarantined",
}


def classify_error_type(gate_rule: str | None, error_code: str | None,
                        outcome: str) -> str | None:
    """Map a terminal verdict to the closed error_type enum.

    Returns None for successful calls — error_type is nullable and a null means
    "nothing went wrong", never "we could not tell".
    """
    if outcome not in ("error", "quarantined"):
        return None
    for candidate in (gate_rule, error_code):
        if candidate and candidate in _ERROR_TYPE_BY_RULE:
            return _ERROR_TYPE_BY_RULE[candidate]
    if outcome == "quarantined":
        return "quarantined"
    return "internal"


def build_event_row(
    *,
    tool: str,
    args: dict[str, Any],
    result: Any = None,
    outcome: str = "ok",
    error_code: str | None = None,
    remedy_emitted: bool = False,
    latency_ms: int | None = None,
    source_surface: str | None = None,
    gate: dict | None = None,
    emit_event_id: str | None = None,
) -> dict[str, Any]:
    """Assemble one PHI-safe tool_events row (column name → value).

    `gate` carries the verdict off the EXCEPTION path. This is the structural
    half of the FINDING-5 fix: a blocked call raises, so its result is None, so
    reading the verdict from the result alone leaves gate_tier/gate_decision
    NULL on precisely the rows that matter. A block is a COMPLETED operation
    with a verdict, not an absence of one.
    """
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
    # Intent Gate (0009): the verdict travels on the row (nullable — ungated
    # calls stay NULL). G2-1 asserts trigger discipline on gate_tier.
    #
    # Result first, then the exception-carried verdict. A successful call has
    # its verdict on the result; a BLOCKED call has it only here, and without
    # this fallback the block row is written with null gate columns and is
    # invisible to every analytics view (validation FINDING-5b).
    gate_block = r.get("gate") if isinstance(r.get("gate"), dict) else {}
    if not gate_block and isinstance(gate, dict):
        gate_block = gate
    gate_rule = gate_block.get("rule")
    return {
        "namespace": args.get("namespace"),
        "tool": tool,
        "actor": args.get("actor"),
        "session_id": str(args["session_id"]) if args.get("session_id") else None,
        "source_surface": source_surface or args.get("source_surface"),
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
        "gate_tier": gate_block.get("tier"),
        "gate_decision": gate_block.get("decision"),
        "gate_rule": gate_rule,
        "error_type": classify_error_type(gate_rule, error_code, outcome),
        # Rows written at/after the 0011 boundary. Everything before it was
        # produced by an emitter that lost blocks, so the two populations are
        # not comparable and must not be silently summed.
        "discontinuity": False,
        "emit_event_id": emit_event_id,
    }
