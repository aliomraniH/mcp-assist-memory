"""Admin dashboard: session-cookie helpers (no DB) and a DB-gated end-to-end
flow (login → live token → /mcp auth → rotate invalidates the old token)."""
from __future__ import annotations

import os
import re

import pytest

# Ensure config can build when the e2e test imports `app`. Set before any import
# of config/app so the settings singleton picks them up.
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("MCP_AUTH_TOKEN", "seed-token-xyz")

from dashboard import csrf_token, make_session, valid_session  # noqa: E402

SECRET = "session-secret-xyz"

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "verify", "version": "0"}},
}


def test_session_round_trip():
    cookie = make_session(SECRET, ttl=60)
    assert valid_session(SECRET, cookie)


def test_session_rejects_tampering_and_wrong_secret():
    cookie = make_session(SECRET, ttl=60)
    assert not valid_session("other-secret", cookie)
    exp, _sig = cookie.rsplit(".", 1)
    assert not valid_session(SECRET, f"{exp}.deadbeef")
    assert not valid_session(SECRET, None)
    assert not valid_session(SECRET, "garbage")


def test_session_rejects_expired():
    assert not valid_session(SECRET, make_session(SECRET, ttl=-1))


def test_csrf_is_bound_to_session():
    cookie, other = "1000000000.aaaa", "2000000000.bbbb"
    assert csrf_token(SECRET, cookie) == csrf_token(SECRET, cookie)
    assert csrf_token(SECRET, cookie) != csrf_token(SECRET, other)
    assert csrf_token(SECRET, cookie) != csrf_token("other", cookie)


@pytest.mark.skipif(os.environ.get("DATABASE_URL") is None, reason="DATABASE_URL not set")
def test_admin_login_rotate_and_mcp_auth():
    from starlette.testclient import TestClient

    import app as appmod

    with TestClient(appmod.app, base_url="https://testserver") as client:
        assert client.get("/healthz").json()["status"] == "ok"

        # wrong then right password
        r = client.post("/admin/login", data={"password": "nope"}, follow_redirects=False)
        assert r.status_code == 200 and "Incorrect" in r.text
        r = client.post("/admin/login", data={"password": "test-admin-pw"}, follow_redirects=False)
        assert r.status_code == 303

        page = client.get("/admin").text
        token = re.search(r'id="tok">([^<]+)<', page).group(1)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        assert token

        # bearer gate on /mcp uses the live token
        assert client.post("/mcp/", json=INIT, headers=MCP_HEADERS).status_code == 401
        ok = client.post("/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
        assert ok.status_code != 401

        # rotate → token changes, old rejected, new accepted
        assert client.post("/admin/rotate", data={"csrf": csrf}, follow_redirects=False).status_code == 303
        new_token = re.search(r'id="tok">([^<]+)<', client.get("/admin").text).group(1)
        assert new_token != token
        assert client.post(
            "/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        ).status_code == 401
        assert client.post(
            "/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {new_token}"}
        ).status_code != 401
