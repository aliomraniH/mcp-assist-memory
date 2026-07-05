"""Per-namespace variant profiles (Phase 7, T7.0) — Workstream E's mechanism.

Profile keys (all optional; every namespace is on CONTROL until the Phase 10
decision protocol is committed and experiment namespaces are flipped):

* ``convention_stmt``  — R1 arm: V0 (nowhere) | V1 (description sentence) |
  V2 (write-time advisory only) | V3 (both). Tool descriptions can't vary per
  request (clients cache at initialize), so the description-sentence half is
  REDEPLOY-scoped: it is a deploy-time constant, and this key exists so every
  event's profile snapshot records which text was live. Accept the granularity.
* ``advisory_mode``    — R5 arm: full (structured + remediation prose) |
  minimal (structured fields only) | off (control).
* ``arg_strictness``   — R6 arm: hint (reject + did-you-mean) | plain (reject,
  name the arg) | control (the framework's own rejection text, unchanged).
* ``remedy_errors``    — R9 arm: on | off — whether the standardized error
  payload's remedy field is populated (T2.5 always supplies it; the tool layer
  strips it when off, so the effect is measurable).
* ``claim_staleness_hours`` — Phase 6 trust-decay window (not an experiment).
* ``clinical``         — PHI hard gate: free-text channels disabled/warned.

Every dict-shaped tool response echoes the resolved profile
(self-describing transcripts); tool_events snapshots it per call.
"""
from __future__ import annotations

DEFAULT_PROFILE: dict = {
    "convention_stmt": "V1",   # the Phase 2/3 description sentences ARE deployed
    "advisory_mode": "off",
    "arg_strictness": "control",
    "remedy_errors": "on",
}

_VALID = {
    "convention_stmt": {"V0", "V1", "V2", "V3"},
    "advisory_mode": {"full", "minimal", "off"},
    "arg_strictness": {"hint", "plain", "control"},
    "remedy_errors": {"on", "off"},
}


def resolve_profile(raw: dict | None) -> dict:
    """Merge a stored profile over the defaults; unknown values fall back to
    the default (a typo in an experiment profile must never crash a tool call)."""
    resolved = dict(DEFAULT_PROFILE)
    for key, valid in _VALID.items():
        val = (raw or {}).get(key)
        if isinstance(val, str) and val in valid:
            resolved[key] = val
    # pass-through, non-experiment keys
    for key in ("claim_staleness_hours", "clinical"):
        if raw and key in raw:
            resolved[key] = raw[key]
    return resolved
