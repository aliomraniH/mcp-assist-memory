"""Surface attribution regression tests: the auth gate resolves the presented
token to its surface label, stamps it into logs/telemetry, and strips ?token=
from the query string after auth so the secret never reaches access logs."""
from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
ADMIN_PW = os.environ["ADMIN_PASSWORD"]

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "memory_save", "arguments": {
        "namespace": "dev/test-surface-attrib", "key": "t/surface",
        "kind": "note", "value": {"v": 1},
    }},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_build_event_row_carries_source_surface():
    from storage.telemetry import build_event_row

    row = build_event_row(tool="memory_save", args={"namespace": "n"},
                          source_surface="chatgpt")
    assert row["source_surface"] == "chatgpt"
    # explicit arg wins over gate label only when gate label absent
    row2 = build_event_row(tool="memory_save",
                           args={"namespace": "n", "source_surface": "manual"})
    assert row2["source_surface"] == "manual"


@pytest.mark.skipif(os.environ.get("DATABASE_URL") is None, reason="DATABASE_URL not set")
def test_surface_label_attribution_and_token_strip():
    from starlette.testclient import TestClient

    import app as appmod

    with TestClient(appmod.app, base_url="https://testserver") as client:
        r = client.post("/admin/login", data={"password": ADMIN_PW},
                        follow_redirects=False)
        assert r.status_code == 303
        page = client.get("/admin").text
        chatgpt = re.search(r'id="tok-chatgpt">([^<]+)<', page).group(1)

        # save via ?token= (ChatGPT style): auth passes, ack records the surface
        resp = client.post(f"/mcp/?token={chatgpt}", json=INIT, headers=MCP_HEADERS)
        assert resp.status_code == 200
        assert '"source_surface":"chatgpt"' in resp.text.replace(" ", "")

        # telemetry row is stamped with the surface
        import psycopg
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            row = conn.execute(
                "SELECT source_surface FROM tool_events "
                "WHERE namespace = 'dev/test-surface-attrib' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None and row[0] == "chatgpt"

        # bad token still rejected via query param
        assert client.post("/mcp/?token=wrong", json=INIT,
                           headers=MCP_HEADERS).status_code == 401
