"""Memory Curator — the asynchronous *write-side* of the coordination spine.

This is to *writing* what ``embeddings`` is to search and ``reconcile`` is to
verification: an **optional, best-effort, injected** dependency. With no Anthropic
key the factory returns a ``DisabledCurator`` (``enabled = False``); ``coord_curate``
becomes a clean no-op and the server boots and behaves identically.

Design contract (see docs/memory-curator.md — the canonical LLM prompt):
* The curator reads a session's execution trace + similar memories and emits a
  structured set of memory operations. It never writes memory itself; a
  deterministic apply-worker (storage/postgres.apply_curation) applies them.
* It runs off the working agent's hot path and never blocks a write.
* It **fails closed**: a response that isn't a single valid JSON object yields
  ZERO operations, never a crash. A dropped memory is recoverable.

The Anthropic SDK is imported lazily so the module never hard-requires it when
curation is disabled.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Protocol, runtime_checkable

# The canonical, versioned curator prompt lives in docs/ — load it at call time so
# the prompt and the code stay a single source of truth (bump its version there).
_PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "docs" / "memory-curator.md"

_EMPTY: dict[str, Any] = {"operations": []}

# Curator outcome status — lets a caller tell a *deliberate* empty result (the
# model ran and chose to persist nothing: a valid, good NOOP) apart from a
# *fail-closed* empty result (SDK missing, API/auth/rate-limit failure, or an
# unparseable/wrong-shape response). The write behavior is identical either way —
# zero operations means zero writes — but the two are no longer observationally
# identical at the surface.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"


def _empty(status: str, *, error: str | None = None) -> dict:
    """A zero-operations result carrying its outcome status (and, for errors, a
    short structural reason — never model prose or secrets)."""
    out: dict[str, Any] = {"operations": [], "curator_status": status}
    if error:
        out["curator_error"] = error
    return out


@runtime_checkable
class Curator(Protocol):
    """Decides what is worth persisting from a session trace. ``enabled`` lets
    callers short-circuit (no LLM call) when curation is off."""

    enabled: bool

    async def curate(self, envelope: dict) -> dict: ...


class DisabledCurator:
    """No-op curator used when no Anthropic key is set. Curation is a clean no-op."""

    enabled = False

    async def curate(self, envelope: dict) -> dict:
        return _empty(STATUS_DISABLED)


def _extract_json(text: str) -> tuple[dict, bool]:
    """Parse the single JSON object the curator must return — fail closed.

    Tolerates stray prose or markdown fences by extracting the outermost
    ``{...}`` span. Returns ``(result, parsed_ok)``: ``parsed_ok`` is False when
    the model produced nothing usable (blank, unparseable, or a non-object shape),
    so the caller can tell a genuine empty-operations NOOP apart from a malformed
    response. Either way the result is a safe ``{"operations": [...]}`` — a bad
    model response can never break a write path."""
    if text:
        spans = [text]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            spans.append(text[start : end + 1])
        for span in spans:
            try:
                obj = json.loads(span)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                return _coerce(obj), True
            # parsed, but not the required object shape → malformed, not a NOOP
            break
    return dict(_EMPTY), False


def _coerce(obj: Any) -> dict:
    """Normalize a parsed object into ``{operations: [...], ...}``; fail closed."""
    if not isinstance(obj, dict):
        return dict(_EMPTY)
    ops = obj.get("operations")
    if not isinstance(ops, list):
        obj = {**obj, "operations": []}
    return obj


class AnthropicCurator:
    """Real curator backed by Anthropic. Best-effort: any failure (network, auth,
    rate-limit, malformed output) yields zero operations — never a wrong write and
    never a crash. The SDK is imported lazily inside ``curate``."""

    enabled = True

    def __init__(self, api_key: str, *, model: str, max_output_tokens: int = 4096) -> None:
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._system_prompt: str | None = None

    def _load_prompt(self) -> str:
        if self._system_prompt is None:
            try:
                self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
            except OSError:
                # The prompt doc should always ship with the repo; if it's somehow
                # absent, fall back to a minimal instruction rather than crashing.
                self._system_prompt = (
                    "You are the Memory Curator. Read the session trace and emit ONLY "
                    "a single JSON object {\"operations\": [...]} per the contract. "
                    "Never invent facts; never write PHI. JSON only."
                )
        return self._system_prompt

    async def curate(self, envelope: dict) -> dict:
        try:
            import anthropic
        except Exception:  # noqa: BLE001 - SDK missing ⇒ fail closed for this call
            return _empty(STATUS_ERROR, error="sdk_unavailable")
        try:
            client = anthropic.AsyncAnthropic(api_key=self._api_key)
            resp = await client.messages.create(
                model=self._model,
                max_tokens=self._max_output_tokens,
                system=self._load_prompt(),
                messages=[{"role": "user", "content": json.dumps(envelope, default=str)}],
            )
            text = "".join(
                getattr(block, "text", "")
                for block in (resp.content or [])
                if getattr(block, "type", None) == "text"
            )
        except Exception as exc:  # noqa: BLE001 - best-effort: any failure ⇒ zero ops
            # Structural reason only (the exception class name) — never model prose
            # or a message that could carry a key/PHI.
            return _empty(STATUS_ERROR, error=type(exc).__name__)
        result, parsed_ok = _extract_json(text)
        if not parsed_ok:
            return _empty(STATUS_ERROR, error="unparseable_response")
        result["curator_status"] = STATUS_OK
        return result


def build_curator(settings: Any) -> Curator:
    """Pick a curator from config (only config.py reads the environment). Returns
    the real ``AnthropicCurator`` only when an Anthropic key is present; otherwise a
    ``DisabledCurator`` so curation is a no-op and the server runs identically."""
    api_key = getattr(settings, "anthropic_api_key", None)
    if api_key:
        return AnthropicCurator(
            api_key,
            model=getattr(settings, "curator_model", "claude-opus-4-1"),
            max_output_tokens=getattr(settings, "curator_max_output_tokens", 4096),
        )
    return DisabledCurator()
