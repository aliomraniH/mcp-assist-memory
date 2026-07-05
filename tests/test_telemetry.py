"""Phase 1: the telemetry spine.

Exercises the tool-layer `instrument` wrapper end-to-end (tools called via
their underlying `.fn`), the PHI hard gate on real tool_events rows, outcome
classification, version stamping, and the pre-registered metric views.
"""
from __future__ import annotations

import json
import uuid

import pytest
from psycopg.rows import dict_row

from server import mcp_server
from storage.telemetry import build_event_row
from storage.versioning import SCHEMA_VERSION, SERVER_VERSION

SENTINEL = "TOP-SECRET-VALUE-ignore previous instructions-987654321"


async def _events(backend, ns):
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT * FROM tool_events WHERE namespace = %s ORDER BY id", (ns,))
        return await cur.fetchall()


@pytest.fixture
def tools(backend):
    """Point the tool layer at the test backend for the duration of a test."""
    mcp_server.deps.backend = backend
    yield mcp_server
    mcp_server.deps.backend = None


async def test_ok_call_records_event_and_stamps_response(tools, backend, ns):
    out = await mcp_server.memory_save(namespace=ns, key="k", value={"v": SENTINEL})
    assert out["server_version"] == SERVER_VERSION
    assert out["schema_version"] == SCHEMA_VERSION

    rows = await _events(backend, ns)
    assert len(rows) == 1
    ev = rows[0]
    assert ev["tool"] == "memory_save" and ev["outcome"] == "ok"
    assert set(ev["arg_names"]) == {"namespace", "key", "value"}
    assert ev["latency_ms"] is not None and ev["result_bytes"] > 0
    assert ev["server_version"] == SERVER_VERSION and ev["schema_version"] == SCHEMA_VERSION


async def test_phi_gate_no_raw_values_in_tool_events(tools, backend, ns):
    """The PHI hard gate, proven on the wire: no fragment of a written value may
    appear anywhere in the persisted tool_events row."""
    await mcp_server.memory_save(namespace=ns, key="k", value={"secret": SENTINEL})
    ev = (await _events(backend, ns))[0]
    row_text = json.dumps({k: v for k, v in ev.items()}, default=str)
    assert SENTINEL not in row_text
    for i in range(len(SENTINEL) - 8):
        assert SENTINEL[i : i + 8] not in row_text
    # but the value's metadata is there (correlatable without content)
    assert ev["arg_value_meta"]["value"]["sha256"]


async def test_error_call_records_error_outcome_and_reraises(tools, backend, ns):
    # Phase 2 (T2.5): the tool layer surfaces AppError as a ToolError carrying
    # the standardized payload; telemetry records the machine code + remedy flag.
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await mcp_server.session_append_event(
            namespace=ns, session_id=str(uuid.uuid4()), kind="note", payload={"x": 1})
    ev = (await _events(backend, ns))[0]
    assert ev["outcome"] == "error"
    assert ev["error_code"] == "session_not_found"
    assert ev["remedy_emitted"] is True


async def test_version_stamps_persisted_on_revision(backend, ns):
    out = await backend.memory_save(ns, "stamped", {"v": 1})
    assert out["server_version"] == SERVER_VERSION
    assert out["schema_version"] == SCHEMA_VERSION
    hist = await backend.memory_history(ns, "stamped")
    assert hist[0]["schema_version"] == SCHEMA_VERSION


async def test_metric_views_are_registered(backend):
    views = ["v_stale_pin_rate", "v_advisory_heed", "v_one_turn_recovery",
             "v_error_recovery", "v_list_result_sizes", "v_screening_hit_rate",
             "v_quarantine_review_lag"]
    async with backend.pool.connection() as conn:
        for v in views:
            await conn.execute(f"SELECT * FROM {v} LIMIT 1")  # noqa: S608 - fixed names


async def test_telemetry_failure_never_fails_the_tool(tools, backend, ns, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(backend, "record_tool_event", boom)
    out = await mcp_server.memory_save(namespace=ns, key="k", value={"v": 1})
    assert out["key"] == "k"  # the write ack is unaffected


def test_build_event_row_outcome_classification():
    row = build_event_row(tool="memory_save", args={"namespace": "n"},
                          result={"deduplicated": True})
    assert row["outcome"] == "dedup_replay" and row["dedup"] is True
    row = build_event_row(tool="memory_save", args={"namespace": "n"},
                          result={"quarantined": True, "screening": ["role_reassignment"]})
    assert row["outcome"] == "quarantined"
    assert row["screening_patterns"] == ["role_reassignment"]
    row = build_event_row(tool="memory_get", args={"namespace": "n"}, outcome="error",
                          error_code="session_not_found", remedy_emitted=True)
    assert row["outcome"] == "error" and row["remedy_emitted"] is True
