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
* ``compact_acks``     — v3 item 8 arm: on (save acks return the compact layered
  envelope by default; the full block stays available behind verbose:true) |
  off (control: the full pre-v3 ack shape, unchanged, plus the additive
  status/summary fields). Additive-schema constraint: old acks unchanged under
  the default profile, so compaction is opt-in per namespace.

Every dict-shaped tool response echoes the resolved profile
(self-describing transcripts); tool_events snapshots it per call.
"""
from __future__ import annotations

DEFAULT_PROFILE: dict = {
    "convention_stmt": "V1",   # the Phase 2/3 description sentences ARE deployed
    "advisory_mode": "off",
    "arg_strictness": "control",
    "remedy_errors": "on",
    "compact_acks": "off",
    # Intent Gate v1 (charter: claude/intent-gate/INTENT_GATE_CHARTER.md).
    # intent_gate gates Tier 0/1 per namespace (staged rollout, S4 — the
    # charter's "no opt-out" binds within gated namespaces; rollout is the
    # operator flipping profiles, exactly like compact_acks). tier2 arms the
    # LLM reasoning tier and ships OFF by default (S3: an always-on LLM gate
    # is a build failure; the operator flips it after the validator's baseline
    # efficacy numbers land).
    "intent_gate": "off",
    "tier2": "off",
}

# --------------------------------------------------------------------------
# Intent Gate Tier-1 retrieval guard (remediation Phase 1a). NUMERIC, so these
# live outside _VALID's enum machinery and are resolved by _resolve_numeric.
#
# gate_similarity_floor — absolute cosine floor. v1 shipped 0.25 and the
#   independent validation run measured true matches at 0.504-0.609 against a
#   noise ceiling of 0.392: a goal about event-log replay returned seven
#   unrelated latency-sample notes at 0.369-0.376. 0.45 sits in the measured
#   gap. It is a floor on RETRIEVAL only — it never escalates anything.
#
# gate_top_fraction_alpha — relative guard. A candidate must also score at
#   least alpha x the top score, so a namespace whose best match is weak does
#   not drag in a long tail just because everything cleared the absolute floor.
#
# CALIBRATION HONESTY: both numbers come from ONE namespace of ~45 entries.
# They are the best available evidence, not a law. Adaptive or conformal
# cutoffs are research-grade until roughly 1,000 matched-outcome observations
# exist in gate_match_log — do not ship one before then. Every match is logged
# with the floor and alpha that judged it precisely so that upgrade has data.
DEFAULT_SIMILARITY_FLOOR = 0.45
DEFAULT_TOP_FRACTION_ALPHA = 0.85

_NUMERIC: dict = {
    "gate_similarity_floor": (DEFAULT_SIMILARITY_FLOOR, 0.0, 1.0),
    "gate_top_fraction_alpha": (DEFAULT_TOP_FRACTION_ALPHA, 0.0, 1.0),
}

_VALID = {
    "convention_stmt": {"V0", "V1", "V2", "V3"},
    "advisory_mode": {"full", "minimal", "off"},
    "arg_strictness": {"hint", "plain", "control"},
    "remedy_errors": {"on", "off"},
    "compact_acks": {"on", "off"},
    "intent_gate": {"on", "off"},
    "tier2": {"on", "off"},
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
    for key in ("claim_staleness_hours", "clinical", "skill_validity_hours"):
        if raw and key in raw:
            resolved[key] = raw[key]
    return resolved


def resolve_gate_guard(raw: dict | None) -> dict:
    """Resolve the Tier-1 retrieval guard from the same stored profile row.

    DELIBERATELY SEPARATE from resolve_profile(). The resolved profile is echoed
    on every dict response and snapshotted into tool_events, and the additive
    constraint on this branch is that old acks stay unchanged under the default
    profile. Folding the guard numbers into that dict would change the shape of
    every ack the server has ever returned, for a value no caller asked for.
    The gate reads the guard; the transcript keeps its shape.

    Out-of-range or wrong-typed values fall back to the server default: a typo
    in a profile edit must never crash a tool call, and — more importantly —
    must never silently DISABLE the floor. Failing to the default is the only
    safe direction for a value whose job is suppression.
    """
    guard = {
        key: default for key, (default, _lo, _hi) in _NUMERIC.items()
    }
    for key, (_default, lo, hi) in _NUMERIC.items():
        val = (raw or {}).get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if lo <= float(val) <= hi:
                guard[key] = float(val)
    # Freshness provenance. A stored signal with no freshness marker becomes a
    # stale authority nobody re-examines — durable memory is an amplifier, not
    # a corrective. calibration_ts records when these numbers were last derived
    # from data; a read past the calibration window is surfaced as unverified
    # rather than silently trusted. Absent => never calibrated against this
    # namespace, which is itself worth knowing.
    guard["calibration_ts"] = (raw or {}).get("gate_calibration_ts")
    guard["temporal_mode"] = (raw or {}).get("gate_temporal_mode") or "server_default"
    return guard
