"""Pin the classification logic of scripts/diagnose_integrations.py.

The script needs live credentials to say anything interesting, but the
diagnosis itself — which hop failed and what the reason string is — must not
drift, or the next resolver outage is undebuggable again (the 2026-07-16
incident: resolver_enabled:true, every branch-head resolution silently None).
Stub transports only; no network, no secrets.
"""
from __future__ import annotations

import httpx
import pytest

from scripts.diagnose_integrations import (
    FAIL,
    OK,
    SKIP,
    check_anthropic,
    check_connector_token,
    check_github_api,
    check_github_egress,
    diagnose_config,
)

CONNECTOR_ENV = {
    "REPLIT_CONNECTORS_HOSTNAME": "connectors.replit.example",
    "WEB_REPL_RENEWAL": "renewal-token",
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- config: which credential path is active ---------------------------------

def test_config_pat_wins_over_connector():
    r = diagnose_config({"GITHUB_TOKEN": "ghp_x", **CONNECTOR_ENV})
    assert r["github_path"] == "pat"


def test_config_connector_path_and_identity_flavor():
    r = diagnose_config(CONNECTOR_ENV)
    assert (r["github_path"], r["connector_identity"]) == ("connector", "depl")
    assert diagnose_config(
        {"REPLIT_CONNECTORS_HOSTNAME": "h", "REPL_IDENTITY": "i"}
    )["connector_identity"] == "repl"


def test_config_nothing_configured_fails():
    r = diagnose_config({})
    assert (r["github_path"], r["status"]) == ("none", FAIL)
    assert not r["anthropic_configured"]


# --- egress: any HTTP response proves reachability ----------------------------

@pytest.mark.asyncio
async def test_egress_any_status_is_reachable():
    async with _client(lambda req: httpx.Response(403)) as client:
        assert (await check_github_egress(client))["status"] == OK


@pytest.mark.asyncio
async def test_egress_transport_error_is_the_only_failure():
    def raise_(req):
        raise httpx.ConnectError("boom")
    async with _client(raise_) as client:
        r = await check_github_egress(client)
    assert r["status"] == FAIL and "network_unreachable" in r["reason"]


# --- connector proxy: every swallowed branch gets a name ----------------------

@pytest.mark.asyncio
async def test_connector_not_configured_is_skip_not_fail():
    async with _client(lambda req: httpx.Response(200)) as client:
        assert (await check_connector_token({}, client))["status"] == SKIP


@pytest.mark.asyncio
async def test_connector_proxy_http_error_named():
    async with _client(lambda req: httpx.Response(502)) as client:
        r = await check_connector_token(CONNECTOR_ENV, client)
    assert r["status"] == FAIL and r["reason"] == "http_502 from connector proxy"


@pytest.mark.asyncio
async def test_connector_no_github_connection_named():
    async with _client(lambda req: httpx.Response(200, json={"items": []})) as client:
        r = await check_connector_token(CONNECTOR_ENV, client)
    assert r["status"] == FAIL and "no_github_connection" in r["reason"]


@pytest.mark.asyncio
async def test_connector_token_minted_but_never_in_reason():
    payload = {"items": [{"settings": {"access_token": "sekret",
                                       "oauth": {"credentials": {"expires_at": "2027-01-01T00:00:00Z"}}}}]}
    async with _client(lambda req: httpx.Response(200, json=payload)) as client:
        r = await check_connector_token(CONNECTOR_ENV, client)
    assert r["status"] == OK and r["token"] == "sekret"
    assert "sekret" not in r["reason"]


# --- github api: auth vs quota vs visibility ----------------------------------

def _github(rate_status=200, remaining=4999, branch_status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/rate_limit":
            return httpx.Response(
                rate_status,
                json={"resources": {"core": {"remaining": remaining, "reset": 1}}})
        return httpx.Response(branch_status, json={"commit": {"sha": "a" * 40}})
    return handler


@pytest.mark.asyncio
async def test_github_expired_token_is_auth_failed():
    async with _client(_github(rate_status=401)) as client:
        r = await check_github_api("t", client, "o/r", "main")
    assert r["status"] == FAIL and "auth_failed" in r["reason"]


@pytest.mark.asyncio
async def test_github_exhausted_quota_is_rate_limited():
    async with _client(_github(remaining=0)) as client:
        r = await check_github_api("t", client, "o/r", "main")
    assert r["status"] == FAIL and "rate_limited" in r["reason"]


@pytest.mark.asyncio
async def test_github_private_repo_is_repo_not_visible():
    async with _client(_github(branch_status=404)) as client:
        r = await check_github_api("t", client, "o/r", "main")
    assert r["status"] == FAIL and "repo_not_visible" in r["reason"]


@pytest.mark.asyncio
async def test_github_healthy_reports_head_and_quota():
    async with _client(_github()) as client:
        r = await check_github_api("t", client, "o/r", "main")
    assert (r["status"], r["head"], r["core_remaining"]) == (OK, "a" * 40, 4999)


# --- anthropic: the exception class is the diagnosis --------------------------

@pytest.mark.asyncio
async def test_anthropic_no_key_is_skip():
    assert (await check_anthropic(None, "m"))["status"] == SKIP


@pytest.mark.asyncio
async def test_anthropic_exception_class_surfaced_never_message():
    class AuthenticationError(Exception):
        pass

    async def create(**kwargs):
        raise AuthenticationError("key sk-ant-... is invalid")

    r = await check_anthropic("k", "claude-opus-4-1", create=create)
    assert r["status"] == FAIL and r["reason"] == "AuthenticationError"
    assert "sk-ant" not in str(r)


@pytest.mark.asyncio
async def test_anthropic_round_trip_ok():
    class Resp:
        model = "claude-opus-4-1"

    async def create(**kwargs):
        return Resp()

    r = await check_anthropic("k", "claude-opus-4-1", create=create)
    assert r["status"] == OK and "claude-opus-4-1" in r["reason"]
