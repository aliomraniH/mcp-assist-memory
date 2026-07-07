"""Curator outcome status (Finding 3) — pure unit tests, no DB, no network.

The status lets a caller tell a *deliberate* empty result (the model ran and
chose to persist nothing: a valid NOOP) apart from a *fail-closed* one (SDK
missing, API/auth/rate-limit failure, or unparseable output). The write path is
fail-closed regardless; these pin the classification the surface now exposes.
"""
from __future__ import annotations

import sys
import types

import pytest

from storage.curator import (
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_OK,
    AnthropicCurator,
    DisabledCurator,
    _extract_json,
)


# ---- _extract_json: (result, parsed_ok) classification --------------------
@pytest.mark.parametrize("text,ok,ops", [
    ('{"operations": [{"op": "NOOP"}]}', True, 1),
    ('{"operations": []}', True, 0),                     # legit NOOP: parsed, empty
    ('prose before {"operations": []} after', True, 0),  # brace-span extraction
    ('```json\n{"operations": []}\n```', True, 0),       # fenced
    ('', False, 0),                                      # blank
    ('not json at all', False, 0),                       # unparseable
    ('[1, 2, 3]', False, 0),                             # valid JSON, wrong shape
    ('"a string"', False, 0),                            # valid JSON, wrong shape
])
def test_extract_json_classifies(text, ok, ops):
    result, parsed_ok = _extract_json(text)
    assert parsed_ok is ok
    assert isinstance(result["operations"], list)
    assert len(result["operations"]) == ops


def test_extract_json_coerces_non_list_operations():
    # A dict parsed successfully (ok=True) but with a bad `operations` field is a
    # curator that answered — its ops just coerce to [] rather than reading error.
    result, ok = _extract_json('{"operations": "oops"}')
    assert ok is True
    assert result["operations"] == []


# ---- DisabledCurator -------------------------------------------------------
async def test_disabled_curator_status():
    out = await DisabledCurator().curate({"trace": []})
    assert out == {"operations": [], "curator_status": STATUS_DISABLED}


# ---- AnthropicCurator: failure/success → status (fake SDK, no network) -----
def _install_fake_anthropic(monkeypatch, *, raises=None, text=None):
    """Swap the lazily-imported `anthropic` module for a stub whose client either
    raises or returns a single text block — exercises curate() without a network."""
    fake = types.ModuleType("anthropic")

    class _Client:
        def __init__(self, **kw):
            self.messages = self

        async def create(self, **kw):
            if raises is not None:
                raise raises
            block = types.SimpleNamespace(type="text", text=text)
            return types.SimpleNamespace(content=[block])

    fake.AsyncAnthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)


async def test_anthropic_curate_maps_exception_to_error(monkeypatch):
    _install_fake_anthropic(monkeypatch, raises=RuntimeError("rate limited"))
    out = await AnthropicCurator("key", model="m").curate({"trace": []})
    assert out["operations"] == []
    assert out["curator_status"] == STATUS_ERROR
    assert out["curator_error"] == "RuntimeError"  # class name only — never prose


async def test_anthropic_curate_unparseable_is_error(monkeypatch):
    _install_fake_anthropic(monkeypatch, text="the model rambled, no json here")
    out = await AnthropicCurator("key", model="m").curate({"trace": []})
    assert out["curator_status"] == STATUS_ERROR
    assert out["curator_error"] == "unparseable_response"


async def test_anthropic_curate_success_is_ok_even_when_empty(monkeypatch):
    _install_fake_anthropic(monkeypatch, text='{"operations": []}')
    out = await AnthropicCurator("key", model="m").curate({"trace": []})
    assert out["curator_status"] == STATUS_OK
    assert out["operations"] == []       # a genuine NOOP — empty, but status ok
    assert "curator_error" not in out
