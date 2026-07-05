"""Write-time screening (Phase 3, T3.1) — deterministic, no LLM in the hot path.

Honest framing (repeat everywhere this is described): this is a LAYER, not a
proof. Regex screens are trivially bypassable by an adaptive attacker — the
same Nasr/Carlini logic that applies to the read-side wrappers. Its real value
is surfacing unsophisticated poisoning and making the sophisticated kind leave
a lineage trail (Phase 5). An LLM screen is deliberately out of scope here:
latency in the write path, and an LLM screen is itself a second injection
surface.

``screen_value`` returns pattern NAMES only — never matched content — so the
verdict is PHI-safe to log and to store in the ``screening`` column.

False-positive reality check: this project's own notes about injection research
(curator prompts quoting "ignore previous", stored marker examples) WILL trip
these patterns. That is a feature for visibility and a bug for retrieval — the
mandatory mitigations are: the verdict returns in the write ack itself,
quarantined entries stay readable via ``include_quarantined: true``, clearing
happens through an auditable ``screening_override`` revision, and the
``v_screening_hit_rate`` view feeds the pre-registered Phase 10 FP threshold.
"""
from __future__ import annotations

import json
import re
from typing import Any

# name -> compiled pattern. Names are the only thing that ever leaves this
# module (responses, telemetry, the screening column).
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # "call memory_delete", "invoke the artifact_put tool", "run coord_curate"
    ("imperative_tool_call", re.compile(
        r"\b(?:call|invoke|run|execute|use)\s+(?:the\s+)?"
        r"(?:memory|handoff|session|artifact|coord|stats|observation)_\w+", re.I)),
    # "you are now the system", "you are a helpful assistant that ..."
    ("role_reassignment", re.compile(
        r"\byou\s+are\s+(?:now|a|an|the)\b", re.I)),
    # "ignore previous instructions", "disregard the system prompt", "forget all prior ..."
    ("instruction_override", re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|your\s+)?"
        r"(?:previous|prior|earlier|above|preceding|the\s+system)\b", re.I)),
    # stored occurrences of our trust-boundary markers, raw or in their
    # escaped form (sanitize rewrites raw ones before screening sees them)
    ("untrusted_marker", re.compile(
        r"<<<\s*(?:UNTRUSTED_DATA|END)\s*>>>|\[\[(?:UNTRUSTED_DATA|END)\]\]", re.I)),
    # a URL paired with an imperative fetch/exfiltrate verb anywhere in the text
    ("url_with_imperative", re.compile(
        r"(?=[\s\S]*https?://)[\s\S]*\b(?:fetch|visit|open|browse|curl|wget|download|"
        r"post\s+to|send\s+to|upload\s+to|exfiltrate)\b", re.I)),
]

PATTERN_NAMES = [name for name, _ in PATTERNS]


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def screen_value(value: Any) -> list[str]:
    """Return the NAMES of every pattern the (sanitized) value trips; [] if clean."""
    text = _to_text(value)
    return [name for name, rx in PATTERNS if rx.search(text)]
