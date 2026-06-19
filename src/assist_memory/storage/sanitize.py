"""Write-path sanitization and the untrusted-data wrapper.

Two defenses, kept deliberately separate:

1. `sanitize()` runs on every value written to storage. It is round-trip-safe
   for ordinary text (so the 18 tool contracts are unchanged for normal input):
   it only removes C0 control characters that have no business in stored text
   and *defangs* our own delimiter tokens so a malicious payload cannot forge
   the boundary of the untrusted-data wrapper.

2. `wrap_untrusted()` fences a string in `<<<UNTRUSTED_DATA>>> … <<<END>>>` so a
   downstream LLM treats recalled content as data, not instructions. Because the
   payload is sanitized first, an attacker cannot smuggle a literal `<<<END>>>`
   to break out of the fence.

Note: wrapping is NOT applied to raw memory values returned by the backend —
that would corrupt `decoded_value()`, `memory_revert`, and `handoff_load`'s JSON
and change the tool contracts. `wrap_untrusted()` is the helper the prompt-
assembly layer uses when injecting recalled content into a model prompt.
"""

from __future__ import annotations

import re

MARKER_START = "<<<UNTRUSTED_DATA>>>"
MARKER_END = "<<<END>>>"

# Defanged replacements: the angle-bracket delimiters are removed so the token
# can never terminate or open a wrapper, while the text stays human-readable.
_DEFANGED_START = "[UNTRUSTED_DATA]"
_DEFANGED_END = "[END]"

# C0 control chars except tab (\x09), newline (\x0a), carriage return (\x0d),
# plus DEL (\x7f). These are stripped from stored text.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_START_RE = re.compile(re.escape(MARKER_START), re.IGNORECASE)
_END_RE = re.compile(re.escape(MARKER_END), re.IGNORECASE)


def strip_control(text: str) -> str:
    return _CONTROL_RE.sub("", text)


def defang_markers(text: str) -> str:
    text = _START_RE.sub(_DEFANGED_START, text)
    return _END_RE.sub(_DEFANGED_END, text)


def sanitize(text: str | None) -> str | None:
    """Neutralize a string for storage. None passes through unchanged."""
    if text is None:
        return None
    return defang_markers(strip_control(text))


def wrap_untrusted(text: str | None) -> str:
    """Fence (already-sanitized) untrusted text for safe injection into a prompt."""
    inner = sanitize(text) if text is not None else ""
    return f"{MARKER_START}\n{inner}\n{MARKER_END}"
