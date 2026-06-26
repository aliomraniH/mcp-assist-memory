"""Backend embedding provider — factory selection and the retry/backoff
semantics that make embedding best-effort under Voyage rate limits.

Offline: ``httpx`` is mocked, so there is no network call and no DB (these run
even when ``DATABASE_URL`` is unset)."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from storage.embeddings import (
    DisabledEmbedder,
    VoyageEmbedder,
    build_embedder,
    embed_text,
    to_vector_literal,
)

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


class _Resp:
    def __init__(self, status, *, data=None, retry_after=None):
        self.status_code = status
        self._data = {"data": data} if data is not None else {}
        self.headers = {} if retry_after is None else {"retry-after": str(retry_after)}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("POST", VOYAGE_URL), response=self  # type: ignore[arg-type]
            )

    def json(self):
        return self._data


def _ok(vectors):
    return _Resp(200, data=[{"index": i, "embedding": v} for i, v in enumerate(vectors)])


def _mock_posts(monkeypatch, responses):
    """Patch httpx.AsyncClient so .post() returns the given responses in order;
    returns a dict whose ['n'] counts how many POSTs were made."""
    seq = iter(responses)
    counter = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            counter["n"] += 1
            return next(seq)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return counter


def test_build_embedder_selection():
    assert isinstance(build_embedder(SimpleNamespace(voyage_api_key=None)), DisabledEmbedder)
    e = build_embedder(SimpleNamespace(
        voyage_api_key="k", embedding_model="voyage-3.5-lite", embedding_dim=1024))
    assert isinstance(e, VoyageEmbedder) and e.enabled and e.dim == 1024


async def test_disabled_embedder_returns_none():
    assert await DisabledEmbedder().embed(["anything"]) is None


async def test_embed_empty_is_noop_without_http(monkeypatch):
    posts = _mock_posts(monkeypatch, [])
    assert await VoyageEmbedder("k", "m", 4).embed([]) == []
    assert posts["n"] == 0


async def test_embed_preserves_request_order(monkeypatch):
    # API returns the items out of order; result must be sorted back by index.
    _mock_posts(monkeypatch, [_Resp(200, data=[
        {"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}])])
    assert await VoyageEmbedder("k", "m", 1).embed(["a", "b"]) == [[1.0], [2.0]]


async def test_embed_retries_5xx_429_then_succeeds(monkeypatch):
    import asyncio

    async def _anoop(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _anoop)  # don't actually back off
    posts = _mock_posts(monkeypatch, [
        _Resp(429, retry_after=0), _Resp(503, retry_after=0), _ok([[0.1, 0.2]])])
    out = await VoyageEmbedder("k", "m", 2).embed(["hello"], max_retries=5)
    assert out == [[0.1, 0.2]]
    assert posts["n"] == 3  # 429, 503, then 200


async def test_embed_fast_fails_on_429_without_retries(monkeypatch):
    # Live path (max_retries=0): a single attempt, then raise — no added latency.
    posts = _mock_posts(monkeypatch, [_Resp(429, retry_after=0)])
    with pytest.raises(httpx.HTTPStatusError):
        await VoyageEmbedder("k", "m", 2).embed(["x"], max_retries=0)
    assert posts["n"] == 1


async def test_embed_non_retryable_4xx_raises_immediately(monkeypatch):
    # A 400 is not in the retry set: it must raise on the first try even with a budget.
    posts = _mock_posts(monkeypatch, [_Resp(400)])
    with pytest.raises(httpx.HTTPStatusError):
        await VoyageEmbedder("k", "m", 2).embed(["x"], max_retries=5)
    assert posts["n"] == 1


def test_retry_delay_prefers_retry_after_then_exponential():
    assert VoyageEmbedder._retry_delay(_Resp(429, retry_after=3), 0) == 3.0
    assert VoyageEmbedder._retry_delay(_Resp(429, retry_after=999), 0) == 32.0   # capped
    assert VoyageEmbedder._retry_delay(_Resp(429, retry_after="bad"), 1) == 4.0  # bad header -> 2*2^1
    assert VoyageEmbedder._retry_delay(_Resp(429), 0) == 2.0                     # 2*2^0
    assert VoyageEmbedder._retry_delay(_Resp(429), 10) == 32.0                   # capped


def test_embed_text_and_vector_literal():
    assert embed_text("k", "hello") == "k\nhello"
    assert embed_text("k", {"b": 1, "a": 2}) == 'k\n{"a": 2, "b": 1}'  # sorted keys
    assert to_vector_literal([0.5, 1.0]) == "[0.5,1.0]"
