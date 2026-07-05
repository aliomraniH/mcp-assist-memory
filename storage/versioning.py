"""Version stamping (global ground rule 3).

``SERVER_VERSION`` tracks the deployed code; ``SCHEMA_VERSION`` tracks the
highest applied migration number. Both are stamped on every persisted revision
(columns on ``memory_entry``) and on every dict-shaped tool response (via
``stamp`` in the tool layer), starting Phase 1. Bare-list responses gain their
stamp when Phase 4 gives them envelopes.
"""
from __future__ import annotations

SERVER_VERSION = "0.2.0"   # bump per released phase batch
SCHEMA_VERSION = 6         # == highest applied migrations/NNNN_*.sql


def stamp(response: dict) -> dict:
    """Add version stamps to a dict-shaped tool response (idempotent, additive)."""
    response.setdefault("server_version", SERVER_VERSION)
    response.setdefault("schema_version", SCHEMA_VERSION)
    return response
