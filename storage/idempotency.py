"""Idempotency fingerprint (v3 item 4) — content-addressed replay detection.

Per IETF draft-ietf-httpapi-idempotency-key-header rev -07 (an Internet-Draft,
NOT an RFC; text dated 2026-07-13): reusing an idempotency key across DIFFERENT
payloads is MUST NOT, and the server answers it with 422 Unprocessable Content —
mirrored here as the in-band ``idempotency_conflict`` tool error. The draft's
409 case (a concurrent retry while the original request is still processing)
does not exist in this server — writes are synchronous and the unique index
resolves races by returning the landed winner — so no ``idempotency_in_flight``
vocabulary is introduced. The draft explicitly permits "an idempotency
fingerprint … in conjunction with an idempotency key", which is exactly this.

Canonicalization is RFC 8785 (JCS) via the compliant ``rfc8785`` library —
Python's ``json.dumps`` is NOT compliant (ECMA-262 shortest-round-trip number
serialization, UTF-16 code-unit key sort). Two consequences surface as
validation errors instead of being skipped, as the RFC requires:

* NaN / Infinity anywhere in a fingerprinted payload → ``unrepresentable_number``
  (JCS MUST hard-error; never silently dropped).
* Integers beyond 2^53 → ``unrepresentable_number`` with the documented remedy:
  represent them as JSON strings in fingerprinted payloads.

CRITICAL placement rule (validated in V4): the fingerprint is computed at the
API boundary over the incoming payload, NEVER over a Postgres jsonb round-trip
— jsonb re-canonicalizes numbers and key order, so a read-back fingerprint
would not match the write-time one.
"""
from __future__ import annotations

import hashlib
from typing import Any

import rfc8785

from errors import AppError


def idem_fingerprint(
    *,
    tool: str,
    namespace: str,
    key: str,
    kind: str,
    payload: Any,
    meta: dict | None,
) -> str:
    """sha256 over the JCS canonicalization of (tool, namespace, key, kind,
    payload, meta) — the write's identity for replay comparison."""
    envelope = {
        "tool": tool,
        "namespace": namespace,
        "key": key,
        "kind": kind,
        "payload": payload,
        "meta": meta,
    }
    try:
        canon = rfc8785.dumps(envelope)
    except rfc8785.FloatDomainError as exc:
        raise AppError(
            "unrepresentable_number",
            "payload contains NaN or Infinity — JCS (RFC 8785) cannot represent "
            "them and they must hard-error, never be skipped",
        ) from exc
    except rfc8785.IntegerDomainError as exc:
        raise AppError(
            "unrepresentable_number",
            "payload contains an integer beyond 2^53 — represent it as a JSON "
            "string in fingerprinted payloads (RFC 8785 number domain)",
        ) from exc
    except rfc8785.CanonicalizationError as exc:
        raise AppError(
            "unrepresentable_number",
            f"payload cannot be canonicalized per RFC 8785: {exc}",
        ) from exc
    return hashlib.sha256(canon).hexdigest()
