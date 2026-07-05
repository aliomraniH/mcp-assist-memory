"""Phase 7: variant-profile mechanism + the R1/R5/R6/R9 arms.

All namespaces default to CONTROL (advisory off, strictness control, remedy on)
until the Phase 10 protocol flips experiment namespaces.
"""
from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from errors.suggest import did_you_mean, levenshtein
from server import mcp_server
from storage.profiles import DEFAULT_PROFILE, resolve_profile


async def _set_profile(backend, ns, profile):
    async with backend.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO variant_profiles (namespace, profile) VALUES (%s, %s) "
            "ON CONFLICT (namespace) DO UPDATE SET profile = EXCLUDED.profile",
            (ns, Jsonb(profile)))
    backend._profile_cache.clear()  # test convenience: skip the 60s TTL


@pytest.fixture
def tools(backend):
    mcp_server.deps.backend = backend
    yield mcp_server
    mcp_server.deps.backend = None


# -------------------------------------------------------------- T7.0 mechanism
def test_resolve_profile_defaults_and_typo_tolerance():
    assert resolve_profile(None) == DEFAULT_PROFILE
    resolved = resolve_profile({"advisory_mode": "BOGUS", "remedy_errors": "off",
                                "clinical": True})
    assert resolved["advisory_mode"] == "off"       # typo falls back to default
    assert resolved["remedy_errors"] == "off"
    assert resolved["clinical"] is True


async def test_every_dict_response_echoes_profile(tools, backend, ns):
    out = await mcp_server.memory_save(namespace=ns, key="k", value={"v": 1})
    assert out["variant_profile"]["advisory_mode"] == "off"
    assert out["variant_profile"]["remedy_errors"] == "on"


# ---------------------------------------------------------------- T7.2 (R5)
async def test_advisory_off_is_control_no_lookup(reconcile_backend, ns):
    b = reconcile_backend
    b.resolver.heads[("o/r", "main")] = "f" * 40
    out = await b.memory_save(ns, "claim/pin", {"c": "x"}, kind="claim",
                              meta={"repo": "o/r", "branch": "main", "repo_sha": "a" * 40})
    assert "advisories" not in out and "advisory_status" not in out


async def test_advisory_minimal_flags_stale_pin(reconcile_backend, ns):
    b = reconcile_backend
    await _set_profile(b, ns, {"advisory_mode": "minimal"})
    b.resolver.heads[("o/r", "main")] = "f" * 40
    out = await b.memory_save(ns, "claim/pin", {"c": "x"}, kind="claim",
                              meta={"repo": "o/r", "branch": "main", "repo_sha": "a" * 40})
    assert out["advisory_status"] == "computed"
    adv = out["advisories"][0]
    assert adv["name"] == "stale_pin" and adv["will_resolve_stale"] is True
    assert "remediation" not in adv  # minimal arm: no prose


async def test_advisory_full_adds_remediation_prose(reconcile_backend, ns):
    b = reconcile_backend
    await _set_profile(b, ns, {"advisory_mode": "full"})
    b.resolver.heads[("o/r", "main")] = "f" * 40
    out = await b.memory_save(ns, "claim/pin", {"c": "x"}, kind="claim",
                              meta={"repo": "o/r", "branch": "main", "repo_sha": "a" * 40})
    assert "remediation" in out["advisories"][0]
    assert "pin the live head" in out["advisories"][0]["remediation"]


async def test_advisory_current_pin_stays_quiet(reconcile_backend, ns):
    b = reconcile_backend
    await _set_profile(b, ns, {"advisory_mode": "full"})
    b.resolver.heads[("o/r", "main")] = "a" * 40
    out = await b.memory_save(ns, "claim/pin", {"c": "x"}, kind="claim",
                              meta={"repo": "o/r", "branch": "main", "repo_sha": "a" * 40})
    assert out["advisory_status"] == "ok" and "advisories" not in out


async def test_advisory_timeout_never_fails_the_write(reconcile_backend, ns):
    import asyncio

    b = reconcile_backend
    await _set_profile(b, ns, {"advisory_mode": "minimal"})

    async def slow_head(repo, branch):
        await asyncio.sleep(5)

    b.resolver.branch_head = slow_head
    out = await b.memory_save(ns, "claim/pin", {"c": "x"}, kind="claim",
                              meta={"repo": "o/r", "branch": "main", "repo_sha": "a" * 40})
    assert out["verified_persisted"] is True
    assert out["advisory_status"] == "skipped_timeout"


# ---------------------------------------------------------------- T7.3 (R6)
def test_did_you_mean_levenshtein():
    assert levenshtein("limt", "limit") == 1
    valid = ["namespace", "key", "value", "limit"]
    assert "did you mean 'limit'" in did_you_mean("limt", valid)
    assert "valid arguments" in did_you_mean("zzzzzz", valid)


async def test_unknown_arg_hint_arm_end_to_end(tools, backend, ns):
    await _set_profile(backend, ns, {"arg_strictness": "hint"})
    async with Client(mcp_server.mcp) as client:
        with pytest.raises(ToolError) as exc:
            await client.call_tool("memory_get", {"namespace": ns, "kye": "k"})
        payload = json.loads(str(exc.value))
        assert payload["error"]["code"] == "unknown_arg"
        assert "did you mean 'key'" in payload["error"]["message"]
        assert payload["error"]["remedy"]
    # telemetered with the dedicated outcome
    async with backend.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT outcome FROM tool_events WHERE namespace = %s "
            "AND outcome = 'unknown_arg_rejected'", (ns,))
        assert await cur.fetchone() is not None


async def test_unknown_arg_control_arm_framework_message(tools, backend, ns):
    async with Client(mcp_server.mcp) as client:
        with pytest.raises(ToolError) as exc:
            await client.call_tool("memory_get", {"namespace": ns, "kye": "k"})
        # control: the framework's own rejection, not our payload
        assert "unknown_arg" not in str(exc.value)
    async with backend.pool.connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT count(*) AS n FROM tool_events WHERE namespace = %s "
            "AND outcome = 'unknown_arg_rejected'", (ns,))
        row = await cur.fetchone()
        assert row["n"] == 1  # still counted — the silent-failure rate has a number


# ---------------------------------------------------------------- T7.4 (R9)
async def test_remedy_stripped_when_arm_off(tools, backend, ns):
    import uuid

    await _set_profile(backend, ns, {"remedy_errors": "off"})
    with pytest.raises(ToolError) as exc:
        await mcp_server.session_append_event(
            namespace=ns, session_id=str(uuid.uuid4()), kind="n", payload={})
    payload = json.loads(str(exc.value))
    assert payload["error"]["code"] == "session_not_found"
    assert payload["error"]["remedy"] is None
    assert payload["error"]["variant_profile"]["remedy_errors"] == "off"
