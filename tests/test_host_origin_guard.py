"""fastmcp (>=3.4.3) Host/Origin protection is ENABLED and configured, not
disabled. This locks in the wiring so a future edit (or dependency drift) can't
silently drop the guard or misconfigure the allow-lists — a class of bug that
only shows up behind the Replit edge in prod (uniform 421s) and never locally.

See app.py `mcp.http_app(...)` and config.mcp_allowed_hosts / mcp_allowed_origins.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x")
os.environ.setdefault("MCP_AUTH_TOKEN", "seed-token-guard")

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
from config import settings  # noqa: E402

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "guard", "version": "0"}},
}
BASE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


@pytest.fixture(scope="module")
def client():
    # Entering the context manager runs the MCP app lifespan (session manager),
    # so a request that clears the guard reaches the transport instead of raising.
    with TestClient(appmod.mcp_app) as c:
        yield c


def _post(client, host=None, origin=None):
    headers = dict(BASE_HEADERS)
    if host:
        headers["host"] = host
    if origin:
        headers["origin"] = origin
    return client.post("/", json=INIT, headers=headers)


def test_deployment_host_is_allowed(client):
    r = _post(client, host="mcp-assist-memory.replit.app")
    assert r.status_code == 200


def test_wildcard_replit_hosts_allowed(client):
    # `*.replit.app` / `*.replit.dev` patterns keep custom Replit subdomains working.
    assert _post(client, host="foo.replit.app").status_code == 200
    assert _post(client, host="bar.replit.dev").status_code == 200


def test_claude_web_connector_origin_allowed(client):
    r = _post(client, host="mcp-assist-memory.replit.app", origin="https://claude.ai")
    assert r.status_code == 200


def test_unlisted_host_is_misdirected(client):
    r = _post(client, host="evil.example.com")
    assert r.status_code == 421  # Host guard: "Misdirected Request"


def test_forbidden_origin_is_rejected(client):
    r = _post(client, host="mcp-assist-memory.replit.app",
              origin="https://evil.example.com")
    assert r.status_code == 403  # Origin guard: "Forbidden Origin"


def test_guard_is_enabled_and_configured():
    # Protection on, with the deployment domain and claude.ai explicitly listed.
    assert settings.mcp_host_origin_protection is True
    assert "mcp-assist-memory.replit.app" in settings.mcp_allowed_hosts_list
    assert "https://claude.ai" in settings.mcp_allowed_origins_list
