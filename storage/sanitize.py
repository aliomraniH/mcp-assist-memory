"""Write-path sanitization and untrusted-data wrapping (lethal-trifecta defense).

Two jobs:

* ``sanitize`` runs on every write. It strips control characters and rewrites
  any text that tries to forge our own ``<<<UNTRUSTED_DATA>>>`` / ``<<<END>>>``
  delimiters into a VISIBLE, ONE-WAY escape (``[[UNTRUSTED_DATA]]`` /
  ``[[END]]``) so a stored value can't smuggle a fake boundary into a
  consumer's context — and the attempt stays visible instead of vanishing
  (Phase 3, T3.3). The escape is never undone on read: unescaping would
  reconstruct the spoof at exactly the moment it's dangerous. Exact-match
  graders and parsers must account for stored marker-like content appearing
  escaped.
* ``wrap_value`` runs at the read boundary. It wraps stored strings in the
  untrusted markers so a downstream model treats them as data, not instructions.
"""
from __future__ import annotations

import re
from typing import Any

UNTRUSTED_OPEN = "<<<UNTRUSTED_DATA>>>"
UNTRUSTED_CLOSE = "<<<END>>>"

# Forged delimiters (any casing / internal whitespace) are escaped on write —
# a visible one-way rewrite, never a silent strip and never unescaped on read.
_DELIM_RE = re.compile(r"<<<\s*(UNTRUSTED_DATA|END)\s*>>>", re.IGNORECASE)
# Strip C0 control chars except tab (\x09), newline (\x0a), carriage return (\x0d).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _escape_delim(m: re.Match) -> str:
    return f"[[{m.group(1).upper()}]]"


def scrub_text(text: str) -> str:
    return _CTRL_RE.sub("", _DELIM_RE.sub(_escape_delim, text))


def sanitize(value: Any) -> Any:
    """Recursively scrub strings in a JSON-able structure (called on write)."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {scrub_text(str(k)): sanitize(v) for k, v in value.items()}
    return value


def wrap_untrusted(text: str) -> str:
    return f"{UNTRUSTED_OPEN}{text}{UNTRUSTED_CLOSE}"


def wrap_value(value: Any) -> Any:
    """Wrap stored strings in untrusted markers at the read boundary."""
    if isinstance(value, str):
        return wrap_untrusted(value)
    if isinstance(value, list):
        return [wrap_value(v) for v in value]
    if isinstance(value, dict):
        return {k: wrap_value(v) for k, v in value.items()}
    return value


# Matches one leading <<<UNTRUSTED_DATA>>> and one trailing <<<END>>> so a wrapped
# value can be recovered (e.g. before json.loads). Mirrors wrap_untrusted exactly.
_WRAP_RE = re.compile(
    rf"^{re.escape(UNTRUSTED_OPEN)}(.*){re.escape(UNTRUSTED_CLOSE)}$", re.DOTALL
)


def strip_untrusted(text: str) -> str:
    """Remove the wrapping markers from a single wrapped string (inverse of
    ``wrap_untrusted``). Returns the text unchanged if it isn't wrapped."""
    m = _WRAP_RE.match(text)
    return m.group(1) if m else text


def unwrap_value(value: Any) -> Any:
    """Recursively strip read-boundary markers added by ``wrap_value``.

    Use this when a consumer needs the raw stored value back (for example to
    ``json.loads`` a value that was a JSON string before it was wrapped). The
    wrapping is still applied on every read; this is the supported way to undo it
    at the point of parsing, not a way to disable the prompt-injection guard."""
    if isinstance(value, str):
        return strip_untrusted(value)
    if isinstance(value, list):
        return [unwrap_value(v) for v in value]
    if isinstance(value, dict):
        return {k: unwrap_value(v) for k, v in value.items()}
    return value
