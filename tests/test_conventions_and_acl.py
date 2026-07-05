"""Acceptance checks (cumulative) + Phase 9 minimal namespace ACL.

Rule 4: conventions live where the model acts — every new behavior must have
its sentence in the affected tool description, checked here by grep so a
refactor can't silently drop one. (The costliest incident in the program was
one missing sentence.)
"""
from __future__ import annotations

import json
import pathlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from server import mcp_server

SRC = (pathlib.Path(__file__).parent.parent / "server" / "mcp_server.py").read_text()


# ------------------------------------------------ rule-4 convention sentences
@pytest.mark.parametrize("phrase", [
    # T2.1 actor-scoped dedup + the measurement rule
    "event_id dedup is scoped to (namespace, actor)",
    "must never share an actor",
    # T2.2 visible dedup
    "deduplicated:true",
    # T2.3 verified acks
    "read-back verified",
    # T3.2 quarantine + override convention
    "include_quarantined:true",
    "meta.screening_override",
    # T3.3 / T4.2 envelope + escape documentation
    "<<<UNTRUSTED_DATA>>>",
    "appears escaped",
    # T4.1 prefix usage example
    'prefix: "run/T02/"',
    "next_cursor",
    # T5.x provenance
    "origin ∈ tool|retrieval|synthesized|human|unknown",
    "derived_from",
    # Phase 6 additions surfaced in coord_health description
    "needs_reverification",
    "skepticism",
    # T8.1/T8.2 feedback channel
    "observation_log",
    "include patient data",
])
def test_convention_sentence_present(phrase):
    assert phrase in SRC, f"missing convention sentence: {phrase!r}"


def test_fresh_save_ack_contains_acceptance_fields():
    """The acceptance-criteria field list for a fresh memory_save response,
    asserted against a real write in test_baseline_contract/test_variants;
    here we pin the documented contract in the tool description."""
    doc = mcp_server.memory_save.__doc__ or ""
    for field in ("verified_persisted", "revision_id", "content_hash", "deduplicated"):
        assert field in doc


# ----------------------------------------------------------- Phase 9 ACL
@pytest.fixture
def tools(backend):
    mcp_server.deps.backend = backend
    yield mcp_server
    mcp_server.deps.backend = None


async def test_acl_unconfigured_is_inert(tools, backend, ns, monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "token_namespace_acl", None)
    mcp_server._parse_acl.cache_clear()
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool(
            "memory_save", {"namespace": ns, "key": "k", "value": {"v": 1}})
        assert result.data["verified_persisted"] is True


async def test_acl_denies_out_of_scope_namespace_fail_closed(tools, backend, ns, monkeypatch):
    acl = json.dumps({"tok-a": ["allowed-"]})
    monkeypatch.setattr(mcp_server.settings, "token_namespace_acl", acl)
    monkeypatch.setattr(mcp_server, "_request_token", lambda: "tok-a")
    mcp_server._parse_acl.cache_clear()
    try:
        async with Client(mcp_server.mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "memory_save", {"namespace": ns, "key": "k", "value": {"v": 1}})
            payload = json.loads(str(exc.value))
            assert payload["error"]["code"] == "acl_denied"
            assert "TOKEN_NAMESPACE_ACL" in payload["error"]["remedy"]
            # allowed prefix passes
            result = await client.call_tool(
                "memory_save", {"namespace": "allowed-proj", "key": "k", "value": {"v": 1}})
            assert result.data["verified_persisted"] is True
    finally:
        mcp_server._parse_acl.cache_clear()


async def test_acl_unknown_token_denied(tools, backend, ns, monkeypatch):
    acl = json.dumps({"tok-a": ["allowed-"]})
    monkeypatch.setattr(mcp_server.settings, "token_namespace_acl", acl)
    monkeypatch.setattr(mcp_server, "_request_token", lambda: None)
    mcp_server._parse_acl.cache_clear()
    try:
        async with Client(mcp_server.mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool("memory_get", {"namespace": ns, "key": "k"})
    finally:
        mcp_server._parse_acl.cache_clear()
