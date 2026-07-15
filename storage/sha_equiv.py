"""sha_equiv — the single SHA-equivalence rule every consumer shares (v3 P0/1).

Settled by code read + live probe (validation/sha-attribution, 2026-07-15):
SHA comparison used to be DIVERGENT — coord_reconcile was prefix-aware
(``sha_match``), coord_health's stale projection was string-strict, and no
write path validated or canonicalized ``meta.repo_sha`` at all, so a 7-char
abbreviation written by one surface read `current` from reconcile and `stale`
from health at the same time. This module is the one place the rule lives:

* ``sha_match(a, b)``   — prefix-aware equivalence (case-insensitive, min 7
  hex chars, exactly git's abbreviation contract). Moved here verbatim from
  ``storage.reconcile``; reconcile and the R5 stale-pin advisory re-export it
  behavior-unchanged.
* ``equivalent(a, b)``  — ``sha_match`` plus a case-insensitive exact-equality
  short-circuit, so legacy short refs (< 7 chars, written before boundary
  validation) still compare equal to themselves. coord_health's stale
  projection uses this.
* ``validate_ref(ref)`` — the WRITE-BOUNDARY gate: a recorded sha ref must be
  hex, 7..40 chars (git's default abbreviation up to a full sha). Non-hex or
  out-of-range refs are rejected with ``invalid_sha`` instead of being stored
  verbatim into the projected ``repo_sha`` column (the rev-1
  claim/p2-build-completion defect shape).
* ``canonicalize(...)`` — best-effort write-time resolution of an abbreviated
  ref to the canonical 40-char sha when GitHub is reachable. The input ref is
  always preserved (``input_ref``) next to the resolution (``resolved_sha``);
  an ambiguous abbreviation raises ``AmbiguousShaRef`` → ``ambiguous_sha``.

Out of scope, deliberately: ``coord_drift_scan`` compares NO SHAs (it groups
live entries by content_hash only — storage/postgres.py) — there is no SHA
rule there to unify, so it does not adopt this module. Do not invent one.
"""
from __future__ import annotations

import asyncio
import string
from typing import Any

from errors import AppError

# Full git object name and the shortest abbreviation we trust (git's default).
FULL_SHA_LEN = 40
MIN_ABBREV_LEN = 7

_HEX = set(string.hexdigits)


class AmbiguousShaRef(Exception):
    """An abbreviated ref matched more than one commit upstream (GitHub 422)."""

    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(f"ambiguous sha abbreviation {ref!r}")


def is_hex_ref(ref: Any) -> bool:
    return isinstance(ref, str) and bool(ref) and all(c in _HEX for c in ref)


def sha_match(a: str | None, b: str | None) -> bool:
    """True if two commit SHAs refer to the same commit, tolerating abbreviation.

    Claims/humans record SHORT shas (e.g. ``6e942ca``); the GitHub API returns the
    FULL 40-char sha. Exact equality would mark almost every real merged claim
    stale, so — like git itself — we treat one as a match when it is a
    case-insensitive prefix of the other (min length 7 to avoid coincidences)."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    short, full = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= MIN_ABBREV_LEN and full.startswith(short)


def equivalent(a: str | None, b: str | None) -> bool:
    """``sha_match`` plus exact case-insensitive equality, so legacy refs shorter
    than the 7-char trust floor still compare equal to themselves (a pre-boundary
    row must not read as drifted from an identical pre-boundary row)."""
    if not a or not b:
        return False
    return a.lower() == b.lower() or sha_match(a, b)


def validate_ref(ref: Any, *, field: str = "repo_sha") -> str:
    """Validate a caller-supplied sha ref at the write boundary; returns the
    lowercased normal form. Raises ``invalid_sha`` for non-hex refs, refs longer
    than a full sha, or abbreviations below git's 7-char default."""
    if not is_hex_ref(ref):
        raise AppError(
            "invalid_sha",
            f"meta.{field} {ref!r} is not a hex commit sha",
        )
    if len(ref) > FULL_SHA_LEN:
        raise AppError(
            "invalid_sha",
            f"meta.{field} {ref!r} is longer than a full 40-char sha",
        )
    if len(ref) < MIN_ABBREV_LEN:
        raise AppError(
            "invalid_sha",
            f"meta.{field} {ref!r} is shorter than git's {MIN_ABBREV_LEN}-char "
            "default abbreviation",
        )
    return ref.lower()


async def canonicalize(
    ref: str,
    *,
    repo: str | None,
    resolver: Any,
    timeout_s: float = 2.0,
) -> tuple[str, str | None]:
    """Best-effort resolution of a VALIDATED ref to the canonical 40-char sha.

    Returns ``(canonical, resolved_sha)`` where ``canonical`` is what the write
    should project (the full sha when resolution succeeded, else the input) and
    ``resolved_sha`` is the upstream answer (None when resolution was skipped or
    failed). Never slows a write beyond ``timeout_s`` and never fails one —
    EXCEPT for an upstream "ambiguous abbreviation" answer, which is a real
    defect in the ref and raises ``ambiguous_sha``."""
    if len(ref) == FULL_SHA_LEN:
        return ref, ref
    if not repo or resolver is None or not getattr(resolver, "enabled", False):
        return ref, None
    commit_sha = getattr(resolver, "commit_sha", None)
    if commit_sha is None:
        return ref, None
    try:
        async with asyncio.timeout(timeout_s):
            full = await commit_sha(repo, ref)
    except AmbiguousShaRef as exc:
        raise AppError(
            "ambiguous_sha",
            f"meta.repo_sha {ref!r} matches more than one commit in {repo}",
        ) from exc
    except (TimeoutError, asyncio.TimeoutError):
        return ref, None
    except Exception:  # noqa: BLE001 - best-effort resolution, never blocks a write
        return ref, None
    if not is_hex_ref(full) or len(full) != FULL_SHA_LEN or not sha_match(ref, full):
        # An answer that doesn't extend the abbreviation is no answer.
        return ref, None
    return full.lower(), full.lower()
