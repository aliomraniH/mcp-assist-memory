"""In-process smoke test of the live MCP handshake — the CI gate for the 421-style
transport/auth/host regression that previously only surfaced via a manual curl
against prod.

Runs the SAME check functions the post-deploy probe (scripts/smoke_mcp.py) runs,
but against the app in-process via Starlette's TestClient over an ephemeral
Postgres. So `pytest -q` (the existing CI job) now fails the build if the
connector handshake, the bearer gate, the tool surface, or /healthz breaks —
before a bad build can be deployed. The standalone script covers the post-deploy
"flag a live deploy" half against a real base URL.
"""
from __future__ import annotations

import os

import pytest

# config/app read these at import time; set before importing app (matches
# test_dashboard.py). Harmless if the env/CI already set them.
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("MCP_AUTH_TOKEN", "seed-token-xyz")

from scripts.smoke_mcp import (  # noqa: E402
    DOCUMENTED_TOOL_COUNT,
    EXPECTED_TOOL_COUNT,
    SmokeError,
    check_handshake,
    check_healthz,
    check_unauthorized,
    run_smoke,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("DATABASE_URL") is None, reason="DATABASE_URL not set"
)


def _active_token() -> str:
    """The gate accepts any active token from the admin store (env MCP_AUTH_TOKEN
    only seeds the `web` token on first boot; the live tokens live in Postgres)."""
    from admin_store import AdminStore

    store = AdminStore(os.environ["DATABASE_URL"])
    store.init()
    tokens = store.get_active_tokens()
    assert tokens, "expected at least one active token after admin.init()"
    return next(iter(tokens))


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import app as appmod

    # follow_redirects stays default; each smoke call passes follow_redirects
    # explicitly so a 307 from a missing path shim fails instead of being followed.
    with TestClient(appmod.app, base_url="https://testserver") as c:
        yield c


def test_smoke_all_checks_pass(client):
    """The whole probe passes end-to-end against the running app."""
    results = run_smoke(client, _active_token())
    joined = " | ".join(results)
    assert "healthz: 200 db=ok" in joined
    assert f"tools/list: 200 ({EXPECTED_TOOL_COUNT} tools)" in joined


def test_smoke_healthz_reports_db_ok(client):
    assert check_healthz(client) == "healthz: 200 db=ok"


def test_smoke_bearer_gate_rejects_missing_and_bad_token(client):
    assert check_unauthorized(client) == ["no-token: 401", "bad-token: 401"]


def test_smoke_handshake_returns_expected_tool_count(client):
    out = check_handshake(client, _active_token())
    assert out == ["initialize: 200", f"tools/list: 200 ({EXPECTED_TOOL_COUNT} tools)"]


def test_smoke_wrong_tool_count_fails(client):
    """A drift in the tool surface (a tool silently dropped/added) must fail the
    smoke test — this is the half-broken-connector signal."""
    with pytest.raises(SmokeError, match="tools, expected"):
        check_handshake(client, _active_token(), expected_tools=EXPECTED_TOOL_COUNT + 1)


def test_smoke_missing_token_raises(client):
    with pytest.raises(SmokeError, match="no token supplied"):
        run_smoke(client, "")


# --- the count-never-goes-stale guards -----------------------------------------
# These pin every hand-written "N tools" number to the ONE source of truth (the
# live tool registry). An intentional add/remove auto-updates the derived
# EXPECTED_TOOL_COUNT; an accidental tool drop (or a stale doc/literal) fails here
# with a message that says exactly which number to fix.

import re  # noqa: E402


def _registered_count() -> int:
    from server.mcp_server import registered_tool_names

    return len(registered_tool_names())


_SURFACE_COUNT_PATTERNS = (
    r"(\d+)-tools?\b",          # "23-tool MCP", "23-tool surface"
    r"\bThe (\d+) tools?\b",    # "## The 23 tools"
    r"and the (\d+) tools?\b",  # docstring: "FastMCP instance and the 23 tools"
    r"surface \((\d+)\)",       # docstring: "Tool surface (23)"
)


def _documented_tool_counts(text: str) -> list[int]:
    """Every number that documents the FULL tool SURFACE.

    Deliberately narrow: it must not match unrelated "N tools" prose (e.g. "the
    18 tools map 1:1 onto StorageBackend", a subset/architecture claim), only the
    phrasings this repo uses for the whole surface — so this guard tracks the tool
    count without policing every sentence that happens to mention tools.
    """
    nums: list[str] = []
    for pat in _SURFACE_COUNT_PATTERNS:
        nums += re.findall(pat, text, flags=re.IGNORECASE)
    return [int(n) for n in nums]


def test_derived_expected_tool_count_matches_registry():
    """In a full-dependency env EXPECTED_TOOL_COUNT is derived from the live
    registry — so the smoke probe's assertion tracks the real surface for free."""
    assert EXPECTED_TOOL_COUNT == _registered_count()


def test_documented_fallback_literal_matches_registry():
    """The bare-probe fallback literal can't silently rot: if a tool is added or
    dropped, bump DOCUMENTED_TOOL_COUNT (and the docs below) in the same commit."""
    assert DOCUMENTED_TOOL_COUNT == _registered_count(), (
        f"scripts.smoke_mcp.DOCUMENTED_TOOL_COUNT={DOCUMENTED_TOOL_COUNT} but the "
        f"registry has {_registered_count()} tools — update the literal."
    )


def test_mcp_server_docstring_tool_count_consistent():
    import server.mcp_server as mcp_server

    expected = _registered_count()
    found = _documented_tool_counts(mcp_server.__doc__ or "")
    assert found, "expected a 'N tools' / 'surface (N)' count in mcp_server's docstring"
    assert all(n == expected for n in found), (
        f"server/mcp_server.py docstring documents {sorted(set(found))} tools but "
        f"the registry has {expected} — a tool was added/dropped, update the docstring."
    )


def test_readme_tool_count_consistent():
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    expected = _registered_count()
    found = _documented_tool_counts(readme.read_text())
    assert found, "expected a 'N tools' / 'N-tool' count in README.md"
    assert all(n == expected for n in found), (
        f"README.md documents {sorted(set(found))} tools but the registry has "
        f"{expected} — a tool was added/dropped, update the README."
    )
