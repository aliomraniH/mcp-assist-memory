from __future__ import annotations

import json

from storage.sanitize import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    sanitize,
    strip_untrusted,
    unwrap_value,
    wrap_value,
)


def test_sanitize_escapes_forged_delimiters():
    # Phase 3 (T3.3) deliberate flip: forged delimiters are VISIBLY escaped
    # (one-way), no longer silently stripped, and never unescaped on read.
    dirty = f"safe {UNTRUSTED_OPEN} injected {UNTRUSTED_CLOSE} tail"
    clean = sanitize(dirty)
    assert UNTRUSTED_OPEN not in clean
    assert UNTRUSTED_CLOSE not in clean
    assert clean == "safe [[UNTRUSTED_DATA]] injected [[END]] tail"
    # variant casing / internal whitespace still caught, still visible
    assert sanitize("<<< untrusted_data >>>x<<< end >>>") == "[[UNTRUSTED_DATA]]x[[END]]"
    # one-way: unwrap helpers never reconstruct the spoof
    assert unwrap_value(clean) == clean


def test_sanitize_strips_control_chars_but_keeps_newlines():
    assert sanitize("a\x00b\x07c") == "abc"
    assert sanitize("line1\nline2\t!") == "line1\nline2\t!"


def test_sanitize_recurses_into_structures():
    out = sanitize({"k": [f"x{UNTRUSTED_OPEN}y", {"n": "z\x00"}]})
    assert out["k"][0] == "x[[UNTRUSTED_DATA]]y"
    assert out["k"][1]["n"] == "z"


def test_wrap_value_wraps_strings_only():
    wrapped = wrap_value({"a": "hi", "b": 3})
    assert wrapped["a"] == f"{UNTRUSTED_OPEN}hi{UNTRUSTED_CLOSE}"
    assert wrapped["b"] == 3


def test_unwrap_value_inverts_wrap_value():
    sanitized = sanitize({"a": "hi", "b": 3, "c": ["x", "y"]})
    assert unwrap_value(wrap_value(sanitized)) == sanitized


def test_strip_untrusted_is_idempotent_and_safe_on_unwrapped():
    raw = "plain text, no markers"
    assert strip_untrusted(raw) == raw
    wrapped = f"{UNTRUSTED_OPEN}{raw}{UNTRUSTED_CLOSE}"
    once = strip_untrusted(wrapped)
    assert once == raw
    assert strip_untrusted(once) == raw  # idempotent


def test_unwrap_restores_json_string_for_parsing():
    payload = json.dumps({"nested": {"k": 1}, "list": [1, 2]})
    wrapped = wrap_value(sanitize(payload))
    assert wrapped.startswith(UNTRUSTED_OPEN) and wrapped.endswith(UNTRUSTED_CLOSE)
    # A consumer must strip markers before json.loads.
    assert json.loads(unwrap_value(wrapped)) == {"nested": {"k": 1}, "list": [1, 2]}
