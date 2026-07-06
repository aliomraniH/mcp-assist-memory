"""Pure-logic tests for the post-deploy repository_dispatch trigger (no network).

Covers the resolution rules that decide whether — and where — the smoke workflow
gets fired after a deploy: repo-slug parsing, token precedence, the dispatch
request shape, and the /healthz readiness poll. The actual POST is best-effort
and exercised end-to-end by the smoke workflow itself; here we lock the
decisions that must not silently drift."""
from __future__ import annotations

import pytest

from scripts.post_deploy_dispatch import (
    EVENT_TYPE,
    _parse_repo_slug,
    build_dispatch_request,
    resolve_explicit_token,
    resolve_repo,
    wait_for_health,
)


def test_parse_repo_slug_forms():
    assert _parse_repo_slug("owner/repo") == "owner/repo"
    assert _parse_repo_slug("owner/repo.git") == "owner/repo"
    assert _parse_repo_slug("git@github.com:owner/repo.git") == "owner/repo"
    assert _parse_repo_slug("https://github.com/owner/repo.git") == "owner/repo"
    assert _parse_repo_slug("https://github.com/owner/repo") == "owner/repo"
    # a token-embedded https remote (connector) still yields the bare slug
    assert _parse_repo_slug("https://x-access-token:ghs_abc@github.com/owner/repo.git") == "owner/repo"
    assert _parse_repo_slug("") is None
    assert _parse_repo_slug("not-a-repo") is None


def test_resolve_repo_env_precedence():
    assert resolve_repo({"SMOKE_DISPATCH_REPO": "a/b", "GITHUB_REPOSITORY": "c/d"}) == "a/b"
    assert resolve_repo({"GITHUB_REPOSITORY": "c/d"}) == "c/d"
    # a full URL in the env var is normalized too
    assert resolve_repo({"SMOKE_DISPATCH_REPO": "git@github.com:o/r.git"}) == "o/r"


def test_resolve_explicit_token_precedence():
    assert resolve_explicit_token({"SMOKE_DISPATCH_TOKEN": "s", "GITHUB_TOKEN": "g"}) == "s"
    assert resolve_explicit_token({"GITHUB_TOKEN": "g"}) == "g"
    assert resolve_explicit_token({"GITHUB_TOKEN": "   "}) is None
    assert resolve_explicit_token({}) is None


def test_build_dispatch_request_shape():
    url, body = build_dispatch_request("owner/repo", "https://live.example/")
    assert url == "https://api.github.com/repos/owner/repo/dispatches"
    assert body["event_type"] == EVENT_TYPE
    assert body["client_payload"]["base_url"] == "https://live.example"  # trailing slash trimmed
    assert body["client_payload"]["source"] == "post-deploy"


def test_build_dispatch_request_omits_absent_base_url():
    _, body = build_dispatch_request("owner/repo", None)
    assert "base_url" not in body["client_payload"]


class _FakeResp:
    def __init__(self, status_code: int, db: str = "ok"):
        self.status_code = status_code
        self._db = db

    def json(self):
        return {"db": self._db}


def test_wait_for_health_returns_true_when_ready():
    client = type("C", (), {"get": lambda self, url: _FakeResp(200)})()
    assert wait_for_health(client, "http://x/healthz", timeout=1, interval=0.01) is True


def test_wait_for_health_times_out_when_never_ready():
    client = type("C", (), {"get": lambda self, url: _FakeResp(503, db="down")})()
    assert wait_for_health(client, "http://x/healthz", timeout=0.05, interval=0.01) is False


def test_wait_for_health_tolerates_exceptions_then_succeeds():
    calls = {"n": 0}

    def _get(self, url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("not up yet")
        return _FakeResp(200)

    client = type("C", (), {"get": _get})()
    assert wait_for_health(client, "http://x/healthz", timeout=1, interval=0.01) is True
    assert calls["n"] >= 3
