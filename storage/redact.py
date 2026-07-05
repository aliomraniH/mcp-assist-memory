"""PHI hard gate for telemetry (Phase 0, T0.4).

Every telemetry row goes through ``redact()``: it keeps argument NAMES and
value METADATA (type, canonical-JSON length, sha256) and never a raw value.
This is the invariant `tests/test_redact.py` pins — no raw ``value``/``content``
string may ever land in a `tool_events` row. Built and tested before any
logging exists to use it (global ground rule 5).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _value_meta(value: Any) -> dict:
    """Type + canonical length + sha256 of one argument value. Never the value."""
    if value is None:
        return {"type": "null"}
    canon = _canon(value)
    return {
        "type": type(value).__name__,
        "len": len(canon),
        "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    }


def redact(args: dict[str, Any] | None) -> tuple[list[str], dict[str, dict]]:
    """Return ``(arg_names, arg_value_meta)`` for a tool call's arguments.

    ``arg_names`` is the sorted list of argument names that were present;
    ``arg_value_meta`` maps each name to ``{type, len, sha256}`` (or
    ``{type: "null"}``). Raw values never appear in the output — the sha256
    lets identical inputs be correlated without ever storing content.
    """
    if not isinstance(args, dict) or not args:
        return [], {}
    names = sorted(str(k) for k in args)
    return names, {str(k): _value_meta(v) for k, v in args.items()}
