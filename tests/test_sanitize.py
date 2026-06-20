from __future__ import annotations

from storage.sanitize import UNTRUSTED_CLOSE, UNTRUSTED_OPEN, sanitize, wrap_value


def test_sanitize_strips_forged_delimiters():
    dirty = f"safe {UNTRUSTED_OPEN} injected {UNTRUSTED_CLOSE} tail"
    clean = sanitize(dirty)
    assert UNTRUSTED_OPEN not in clean
    assert UNTRUSTED_CLOSE not in clean
    assert "injected" in clean


def test_sanitize_strips_control_chars_but_keeps_newlines():
    assert sanitize("a\x00b\x07c") == "abc"
    assert sanitize("line1\nline2\t!") == "line1\nline2\t!"


def test_sanitize_recurses_into_structures():
    out = sanitize({"k": [f"x{UNTRUSTED_OPEN}y", {"n": "z\x00"}]})
    assert out["k"][0] == "xy"
    assert out["k"][1]["n"] == "z"


def test_wrap_value_wraps_strings_only():
    wrapped = wrap_value({"a": "hi", "b": 3})
    assert wrapped["a"] == f"{UNTRUSTED_OPEN}hi{UNTRUSTED_CLOSE}"
    assert wrapped["b"] == 3
