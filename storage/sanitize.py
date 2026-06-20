"""Write-path sanitization and untrusted-data wrapping (lethal-trifecta defense).

Two jobs:

* ``sanitize`` runs on every write. It strips control characters and any text
  that tries to forge our own ``<<<UNTRUSTED_DATA>>>`` / ``<<<END>>>`` delimiters
  so a stored value can't smuggle a fake boundary into a consumer's context.
* ``wrap_value`` runs at the read boundary. It wraps stored strings in the
  untrusted markers so a downstream model treats them as data, not instructions.
"""
from __future__ import annotations

import re
from typing import Any

UNTRUSTED_OPEN = "<<<UNTRUSTED_DATA>>>"
UNTRUSTED_CLOSE = "<<<END>>>"

# Forged delimiters (any casing / internal whitespace) are removed on write.
_DELIM_RE = re.compile(r"<<<\s*(?:UNTRUSTED_DATA|END)\s*>>>", re.IGNORECASE)
# Strip C0 control chars except tab (\x09), newline (\x0a), carriage return (\x0d).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def scrub_text(text: str) -> str:
    return _CTRL_RE.sub("", _DELIM_RE.sub("", text))


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
