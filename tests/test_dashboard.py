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
async def test_capabilities_page_serves_html():
    # The public capabilities page lives at /capabilities (NOT /docs, which
    # FastAPI reserves for Swagger UI). Serve the fixed file as text/html.
    from fastapi.responses import FileResponse

    import app as appmod

    resp = await appmod.capabilities_page()
    assert isinstance(resp, FileResponse)
    assert resp.media_type == "text/html"
    assert str(resp.path).endswith("docs/mcp-capabilities.html")


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
        web = re.search(r'id="tok-web">([^<]+)<', page).group(1)
        desktop = re.search(r'id="tok-desktop-cli">([^<]+)<', page).group(1)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        assert web and desktop and web != desktop  # one distinct token per surface

        # bearer gate on /mcp: no token rejected, EITHER active token accepted
        assert client.post("/mcp/", json=INIT, headers=MCP_HEADERS).status_code == 401
        for tok in (web, desktop):
            ok = client.post("/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {tok}"})
            assert ok.status_code != 401
        # web surface can't send headers -> ?token= must also be accepted
        assert client.post(f"/mcp/?token={web}", json=INIT, headers=MCP_HEADERS).status_code != 401

        # rotate ONLY web → web token changes & old web rejected, desktop untouched
        assert client.post(
            "/admin/rotate", data={"csrf": csrf, "label": "web"}, follow_redirects=False
        ).status_code == 303
        page2 = client.get("/admin").text
        new_web = re.search(r'id="tok-web">([^<]+)<', page2).group(1)
        same_desktop = re.search(r'id="tok-desktop-cli">([^<]+)<', page2).group(1)
        assert new_web != web
        assert same_desktop == desktop  # rotating one surface leaves the other intact
        assert client.post(
            "/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {web}"}
        ).status_code == 401
        for tok in (new_web, same_desktop):
            assert client.post(
                "/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {tok}"}
            ).status_code != 401
