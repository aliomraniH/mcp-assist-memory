"""Connector-sourced GitHub token + resolver selection (no network).

These cover the wiring that lets the reconciler run off a connected GitHub
account (Replit connector) instead of an explicit GITHUB_TOKEN, plus the
precedence/fallback rules in build_resolver."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from storage.github_token import (
    ConnectorTokenProvider,
    _ttl_from_credentials,
    build_connector_token_provider,
)
from storage.reconcile import DisabledResolver, GitHubResolver, build_resolver


def _settings(**kw):
    base = dict(
        github_token=None,
        github_api_url="https://api.github.com",
        replit_connectors_hostname=None,
        repl_identity=None,
        web_repl_renewal=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_connector_provider_needs_host_and_identity():
    assert build_connector_token_provider(_settings()) is None
    assert build_connector_token_provider(_settings(replit_connectors_hostname="h")) is None
    assert build_connector_token_provider(_settings(repl_identity="i")) is None
    p = build_connector_token_provider(_settings(replit_connectors_hostname="h", repl_identity="i"))
    assert isinstance(p, ConnectorTokenProvider)
    # deployments use WEB_REPL_RENEWAL instead of REPL_IDENTITY
    p2 = build_connector_token_provider(_settings(replit_connectors_hostname="h", web_repl_renewal="r"))
    assert isinstance(p2, ConnectorTokenProvider)


def test_build_resolver_precedence():
    # 1. explicit token wins
    r = build_resolver(_settings(github_token="ghp_static"))
    assert isinstance(r, GitHubResolver) and r.enabled
    # 2. else connector when its env is present
    r = build_resolver(_settings(replit_connectors_hostname="h", repl_identity="i"))
    assert isinstance(r, GitHubResolver) and r.enabled
    # 3. else disabled
    assert isinstance(build_resolver(_settings()), DisabledResolver)


@pytest.mark.asyncio
async def test_static_token_wrapped_as_provider():
    r = GitHubResolver("ghp_static")
    assert await r._token_provider() == "ghp_static"


@pytest.mark.asyncio
async def test_resolver_unverifiable_when_no_token():
    # A provider that yields no token must short-circuit to None (no network),
    # which the caller maps to "unverifiable".
    async def _none():
        return None

    r = GitHubResolver(_none)
    assert await r.merged_state("o/r", 1) is None
    assert await r.branch_head("o/r", "main") is None


@pytest.mark.asyncio
async def test_provider_caches_token(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"settings": {"access_token": "tok-123"}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    p = ConnectorTokenProvider("host", "repl ident")
    assert await p() == "tok-123"
    assert await p() == "tok-123"  # served from cache
    assert calls["n"] == 1


def test_ttl_from_credentials():
    assert _ttl_from_credentials({"expires_in": 3600}) == pytest.approx(3540.0)
    assert _ttl_from_credentials({"expires_in": 1}) == 30.0          # clamped to min
    assert _ttl_from_credentials({}) == 600.0                         # default
    assert _ttl_from_credentials({"expires_at": "not-a-date"}) == 600.0


def test_ttl_from_credentials_expires_at():
    # An already-past expiry must not yield a negative/zero window — clamp to min,
    # so a stale token is re-fetched promptly rather than cached "forever".
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert _ttl_from_credentials({"expires_at": past}) == 30.0
    future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    assert _ttl_from_credentials({"expires_at": future}) == pytest.approx(240.0, abs=5)


@pytest.mark.asyncio
async def test_provider_refetches_after_cache_expiry(monkeypatch):
    """OAuth-refresh resilience: once the cache window lapses the provider must
    re-fetch (returning a rotated token), not serve the stale one forever."""
    tokens = iter(["tok-a", "tok-b"])

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"settings": {
                "access_token": next(tokens),
                "oauth": {"credentials": {"expires_in": 90}},  # TTL -> max(90-60,30)=30s
            }}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    p = ConnectorTokenProvider("host", "repl ident")
    assert await p() == "tok-a"      # fetch 1, cached ~30s
    clock["t"] += 5
    assert await p() == "tok-a"      # still within window -> cached
    clock["t"] += 60                 # window lapsed
    assert await p() == "tok-b"      # re-fetched (rotated token)
