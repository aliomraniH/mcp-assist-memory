"""Embeddings for semantic recall (Phase 3).

The storage tier depends only on the small ``Embedder`` surface here. The
provider is **Voyage** (the embedding key the project reserved); OpenAI /
LangSmith belong to other tiers and are never used for this.

Embeddings are **optional and best-effort**: when no provider key is configured
the factory returns a ``DisabledEmbedder`` whose ``enabled`` is ``False``, and
the rest of the service runs exactly as before (keyword-only ``memory_search``,
no embedding columns written). Nothing here is a hard startup dependency.

Vector wire format: pgvector accepts a text literal like ``[0.1,0.2,...]`` cast
with ``::vector``, so we avoid the ``pgvector`` python package and any
per-connection type registration. ``to_vector_literal`` produces that string.
"""
from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


@runtime_checkable
class Embedder(Protocol):
    """An embedding provider. ``enabled`` lets callers skip the work entirely
    (no latency) when embeddings are off."""

    enabled: bool

    async def embed(
        self, texts: list[str], *, input_type: str = "document", max_retries: int = 0
    ) -> list[list[float]] | None: ...


class DisabledEmbedder:
    """No-op embedder used when no provider key is set. Search stays keyword-only."""

    enabled = False

    async def embed(
        self, texts: list[str], *, input_type: str = "document", max_retries: int = 0
    ) -> list[list[float]] | None:
        return None


class VoyageEmbedder:
    """Voyage embeddings over their REST API. ``httpx`` is imported lazily so the
    module never hard-requires it when embeddings are disabled."""

    enabled = True

    def __init__(self, api_key: str, model: str, dim: int, *, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout

    async def embed(
        self, texts: list[str], *, input_type: str = "document", max_retries: int = 0
    ) -> list[list[float]] | None:
        """Embed ``texts`` via Voyage.

        ``max_retries`` defaults to 0 so the live write/search path fails fast on
        a 429/5xx (embedding is best-effort and runs inline — it must not add
        seconds of latency to a save). Bulk callers that can afford to wait (the
        backfill) pass a budget to ride out rate limiting; retries use exponential
        backoff and honor a ``Retry-After`` header. Other 4xx raise immediately.
        """
        if not texts:
            return []
        import asyncio
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
            "input_type": input_type,        # 'document' on write, 'query' on search
            "output_dimension": self.dim,    # pin to the vector column's dimension
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(max_retries + 1):
                resp = await client.post(
                    VOYAGE_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
                if resp.status_code in self._RETRY_STATUS and attempt < max_retries:
                    await asyncio.sleep(self._retry_delay(resp, attempt))
                    continue
                resp.raise_for_status()
                break
            data = resp.json()["data"]
        # Preserve request order regardless of how the API returns them.
        return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]

    _RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
    _BACKOFF_BASE = 2.0          # seconds: 2, 4, 8, 16, 32 (capped)
    _BACKOFF_CAP = 32.0

    @classmethod
    def _retry_delay(cls, resp: Any, attempt: int) -> float:
        """Backoff before the next retry: prefer the server's Retry-After header,
        else exponential backoff capped at ``_BACKOFF_CAP``."""
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), cls._BACKOFF_CAP)
            except ValueError:
                pass
        return min(cls._BACKOFF_BASE * (2 ** attempt), cls._BACKOFF_CAP)


def build_embedder(settings: Any) -> Embedder:
    """Pick an embedder from config: Voyage when a key is present, else disabled.

    Takes ``settings`` as an argument so this module stays decoupled from
    ``config`` (only ``config.py`` reads the environment)."""
    if getattr(settings, "voyage_api_key", None):
        return VoyageEmbedder(
            settings.voyage_api_key,
            settings.embedding_model,
            settings.embedding_dim,
        )
    return DisabledEmbedder()


def embed_text(key: str, value: Any) -> str:
    """Build the text to embed for a memory entry: the key plus its value as text.

    The key carries intent (e.g. ``coord/auth-token-rotation``) and the value the
    content, so both contribute to recall."""
    if isinstance(value, str):
        body = value
    else:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return f"{key}\n{body}" if key else body


def to_vector_literal(vec: list[float]) -> str:
    """Render a vector as a pgvector text literal: ``[0.1,0.2,...]`` (cast ::vector)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
