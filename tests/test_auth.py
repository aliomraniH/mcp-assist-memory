from starlette.testclient import TestClient

from assist_memory.server import create_app

from .conftest import TEST_TOKEN, make_config

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def make_client(tmp_path) -> TestClient:
    return TestClient(create_app(make_config(tmp_path)))


def test_health_check_is_anonymous_and_bare(tmp_path):
    with make_client(tmp_path) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_missing_token_is_401(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post("/mcp", json=INIT_BODY, headers=MCP_HEADERS)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_401(tmp_path):
    with make_client(tmp_path) as client:
        for bad in ("Bearer wrong-token", "Basic abc", TEST_TOKEN, "bearer " + TEST_TOKEN):
            response = client.post(
                "/mcp", json=INIT_BODY, headers={**MCP_HEADERS, "Authorization": bad}
            )
            assert response.status_code == 401, f"expected 401 for {bad!r}"


def test_all_methods_and_paths_require_auth(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/mcp").status_code == 401
        assert client.delete("/mcp").status_code == 401
        assert client.post("/").status_code == 401  # only GET / is anonymous
        assert client.get("/anything-else").status_code == 401


def test_correct_token_reaches_mcp(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/mcp",
            json=INIT_BODY,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["result"]["serverInfo"]["name"] == "assist-memory"


def test_query_param_token_works_for_headerless_clients(tmp_path):
    """Clients that can't send custom headers (e.g. claude.ai web connectors)
    can authenticate with ?token=<MCP_AUTH_TOKEN> instead."""
    with make_client(tmp_path) as client:
        response = client.post(
            f"/mcp?token={TEST_TOKEN}", json=INIT_BODY, headers=MCP_HEADERS
        )
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "assist-memory"


def test_wrong_query_param_token_is_401(tmp_path):
    with make_client(tmp_path) as client:
        assert client.post(
            "/mcp?token=wrong", json=INIT_BODY, headers=MCP_HEADERS
        ).status_code == 401
        # a wrong header is not rescued by also being wrong in the query
        assert client.post(
            "/mcp?token=wrong",
            json=INIT_BODY,
            headers={**MCP_HEADERS, "Authorization": "Bearer wrong"},
        ).status_code == 401
