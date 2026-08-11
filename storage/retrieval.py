"""The single retrieval guard every semantic read passes through.

WHY THIS MODULE EXISTS
    The Intent Gate remediation put a similarity floor (0.45) and a relative
    top-fraction guard (alpha 0.85) on Tier-1 candidate selection. It put them
    in `storage/gate.py`, on the gate's own retrieval path, and nowhere else.

    So `memory_search` — the tool an agent actually reaches for when it wants to
    know something — kept ranking by raw cosine distance and returning top-N with
    no floor at all. The same store, queried two ways, disagreed about what
    counts as a match. An agent that called `intent_open` saw a guarded view; an
    agent that called `memory_search` saw the 0.25-0.39 noise band that
    validation FINDING-3 was written about.

    That is not a tuning problem and no amount of prompt guidance fixes it. If
    retrieval quality depends on WHICH tool the model happened to call, then the
    model's call ordering is load-bearing, and call ordering is the least
    reliable thing in the system. The guard has to live below the tools, where
    no caller can route around it.

    Every semantic read now funnels through `apply_guard`. One floor, one alpha,
    one definition of "admitted", shared by search, the gate, and anything added
    later.

WHAT TRAVELS WITH EVERY ROW
    A `RetrievalVerdict`, not just a boolean. The caller gets the cosine, the
    floor and alpha that judged it, whether it cleared each, and the calibration
    provenance of those numbers. Three consequences:

      * A sub-floor row can be RETURNED rather than dropped, carrying
        `admitted=False` and its reason. Nothing disappears silently; the caller
        sees the boundary and can decide. Dropping rows without saying so is how
        a retrieval layer becomes unfalsifiable.
      * "Nothing matched" is distinguishable from "matches existed but were all
        below the floor" — operationally very different answers to the same
        question.
      * The floor's own freshness rides along, so a caller can tell a calibrated
        threshold from a server default nobody has ever measured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Reasons a candidate did or did not make it. Closed vocabulary: this is a
# telemetry dimension and a caller-visible field, so it must not drift.
ADMITTED = "admitted"
BELOW_FLOOR = "below_floor"
BELOW_ALPHA = "below_alpha"
KEYWORD_ONLY = "keyword_only"
REASONS = (ADMITTED, BELOW_FLOOR, BELOW_ALPHA, KEYWORD_ONLY)


@dataclass(frozen=True)
class RetrievalVerdict:
    """Why this row is (or is not) in the result set.

    Frozen because a verdict is a record of a decision already made. Anything
    that wants a different answer must re-run the guard with different inputs,
    not edit the finding.
    """

    admitted: bool
    reason: str
    cosine: float | None
    top_score: float | None
    absolute_floor: float
    alpha: float
    temporal_mode: str
    calibration_ts: str | None

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "cosine": round(self.cosine, 3) if self.cosine is not None else None,
            "top_score": round(self.top_score, 3) if self.top_score is not None else None,
            "absolute_floor": self.absolute_floor,
            "alpha": self.alpha,
            "temporal_mode": self.temporal_mode,
            "calibration_ts": self.calibration_ts,
            # A floor nobody has calibrated against this namespace is a server
            # default wearing a number's clothes. Say so at every read rather
            # than letting the digits imply a measurement that never happened.
            "calibrated": self.calibration_ts is not None,
        }


@dataclass
class GuardOutcome:
    """The whole result of one guarded retrieval, not just the survivors."""

    admitted: list[tuple[Any, RetrievalVerdict]] = field(default_factory=list)
    rejected: list[tuple[Any, RetrievalVerdict]] = field(default_factory=list)
    top_score: float | None = None

    @property
    def all_scored(self) -> list[tuple[Any, RetrievalVerdict]]:
        return self.admitted + self.rejected

    def summary(self) -> dict:
        """Caller-facing counts. `rejected_below_floor > 0` with `admitted == 0`
        is the case worth surfacing: the store had neighbours and every one of
        them was noise. That is a real answer, and a different one from an empty
        namespace."""
        return {
            "admitted": len(self.admitted),
            "rejected_below_floor": sum(
                1 for _, v in self.rejected if v.reason == BELOW_FLOOR),
            "rejected_below_alpha": sum(
                1 for _, v in self.rejected if v.reason == BELOW_ALPHA),
            "top_score": round(self.top_score, 3) if self.top_score is not None else None,
        }


def apply_guard(
    candidates: Iterable[tuple[Any, float | None]],
    guard: dict,
    *,
    keyword_keys: set | None = None,
) -> GuardOutcome:
    """Judge every candidate against the namespace's floor and alpha.

    `candidates` is (row, cosine) pairs. A None cosine means the row arrived by
    a non-semantic route (keyword leg, deterministic trigger overlap, explicit
    structured scan). Those are NOT floored — the floor is a statement about
    embedding proximity and has no meaning for a row that was never ranked by
    it. They are admitted with reason `keyword_only` so the distinction stays
    visible instead of being laundered into a fake score.

    Both guards are RETRIEVAL controls. Neither has ever meant "this is a
    violation" — that is the trigger predicate's job, and conflating the two is
    the defect this whole line of work exists to undo.
    """
    floor = float(guard.get("gate_similarity_floor", 0.45))
    alpha = float(guard.get("gate_top_fraction_alpha", 0.85))
    temporal_mode = str(guard.get("temporal_mode") or "server_default")
    calibration_ts = guard.get("calibration_ts")

    pairs = list(candidates)
    scored = [c for c in pairs if c[1] is not None]
    top = max((c[1] for c in scored), default=None)
    relative = (alpha * top) if top is not None else 0.0

    out = GuardOutcome(top_score=top)
    for row, cosine in pairs:
        if cosine is None:
            verdict = RetrievalVerdict(
                admitted=True, reason=KEYWORD_ONLY, cosine=None, top_score=top,
                absolute_floor=floor, alpha=alpha, temporal_mode=temporal_mode,
                calibration_ts=calibration_ts)
            out.admitted.append((row, verdict))
            continue

        if cosine < floor:
            reason, admitted = BELOW_FLOOR, False
        elif cosine < relative:
            # Cleared the absolute bar but is a long-tail straggler behind a much
            # stronger top hit. The floor answers "related at all"; alpha answers
            # "as good as the best thing we found". Different questions.
            reason, admitted = BELOW_ALPHA, False
        else:
            reason, admitted = ADMITTED, True

        verdict = RetrievalVerdict(
            admitted=admitted, reason=reason, cosine=cosine, top_score=top,
            absolute_floor=floor, alpha=alpha, temporal_mode=temporal_mode,
            calibration_ts=calibration_ts)
        (out.admitted if admitted else out.rejected).append((row, verdict))

    return out


def policy_block(guard: dict) -> dict:
    """The active retrieval policy, echoed on every guarded response.

    Present so a caller never has to infer the thresholds from the results, and
    so two surfaces returning different rows for the same query can be compared
    on the policy that produced them rather than on vibes.
    """
    return {
        "absolute_floor": float(guard.get("gate_similarity_floor", 0.45)),
        "alpha": float(guard.get("gate_top_fraction_alpha", 0.85)),
        "temporal_mode": str(guard.get("temporal_mode") or "server_default"),
        "calibration_ts": guard.get("calibration_ts"),
        "calibrated": guard.get("calibration_ts") is not None,
        "applies_to": ["memory_search", "intent_open", "recall"],
        "note": ("One floor for every semantic read. cosine is DISPLAY-ONLY and "
                 "never escalates; escalation is decided by a skill's trigger "
                 "predicate. Rows below the floor are returned marked "
                 "admitted:false, never silently dropped."),
    }
