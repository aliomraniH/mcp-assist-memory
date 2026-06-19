from starlette.testclient import TestClient

from assist_memory.dashboard import (
    csrf_token,
    make_session,
    valid_session,
)
from assist_memory.server import create_app

from .conftest import make_config

SECRET = "session-secret-xyz"


def test_session_round_trip():
    cookie = make_session(SECRET, ttl=60)
    assert valid_session(SECRET, cookie)


def test_session_rejects_tampering_and_wrong_secret():
    cookie = make_session(SECRET, ttl=60)
    assert not valid_session("other-secret", cookie)
    exp, sig = cookie.rsplit(".", 1)
    assert not valid_session(SECRET, f"{exp}.deadbeef")
    assert not valid_session(SECRET, None)
    assert not valid_session(SECRET, "garbage")


def test_session_rejects_expired():
    cookie = make_session(SECRET, ttl=-1)
    assert not valid_session(SECRET, cookie)


def test_csrf_is_bound_to_session():
    cookie = "1000000000.aaaa"
    other = "2000000000.bbbb"
    assert csrf_token(SECRET, cookie) == csrf_token(SECRET, cookie)
    assert csrf_token(SECRET, cookie) != csrf_token(SECRET, other)
    assert csrf_token(SECRET, cookie) != csrf_token("other", cookie)


def test_admin_paths_bypass_bearer_but_dashboard_requires_login(tmp_path):
    # No admin store wired in: /admin routes are not mounted, but the bearer
    # middleware still must not 401 the /admin path (it delegates auth there).
    with TestClient(create_app(make_config(tmp_path))) as client:
        # /admin is exempt from bearer auth; without routes mounted it 404s,
        # which proves it was NOT blocked with a 401 by the bearer middleware.
        assert client.get("/admin", follow_redirects=False).status_code == 404
        # the MCP endpoint is still protected
        assert client.get("/mcp").status_code == 401
