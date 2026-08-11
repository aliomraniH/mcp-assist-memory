"""[CI] The shared retrieval guard — one floor for every semantic read.

The defect this pins: before `storage/retrieval.py` existed, `intent_open` ran
candidates through a 0.45 absolute floor and a 0.85 relative alpha while
`memory_search` ran the same store through no floor at all. Retrieval quality
therefore depended on WHICH TOOL the caller happened to reach for, which made
call ordering load-bearing. No prompt, skill, or tool description can fix that —
it is in the wrong place. These tests hold the guard in one place.

Pure-function tests here; the wiring tests (that search and the gate both go
through it) live in test_sequences.py where a real store is available.
"""
from __future__ import annotations

import pytest

from storage.retrieval import (
    ADMITTED,
    BELOW_ALPHA,
    BELOW_FLOOR,
    KEYWORD_ONLY,
    apply_guard,
    policy_block,
)

pytestmark = pytest.mark.ci

GUARD = {"gate_similarity_floor": 0.45, "gate_top_fraction_alpha": 0.85,
         "temporal_mode": "server_default", "calibration_ts": None}


def _rows(*scores):
    return [({"key": f"k{i}"}, s) for i, s in enumerate(scores)]


def test_absolute_floor_rejects_the_noise_band():
    """The measured case: a true match at 0.539 alongside seven unrelated
    latency notes at 0.369-0.376, all of which v1's 0.25 floor admitted."""
    out = apply_guard(_rows(0.539, 0.376, 0.369), GUARD)
    assert [r["key"] for r, _ in out.admitted] == ["k0"]
    assert all(v.reason == BELOW_FLOOR for _, v in out.rejected)


def test_relative_alpha_rejects_a_long_tail_behind_a_strong_hit():
    """Both clear the absolute floor. The floor answers "related at all"; alpha
    answers "as good as the best thing we found" — different questions."""
    out = apply_guard(_rows(0.90, 0.46), GUARD)
    assert len(out.admitted) == 1
    assert out.rejected[0][1].reason == BELOW_ALPHA


def test_a_keyword_row_is_admitted_and_never_floored():
    """A cosine floor is a statement about embedding proximity. A row that
    arrived by substring match was never ranked by one, so flooring it would be
    applying a threshold to a quantity that does not exist."""
    out = apply_guard([({"key": "kw"}, None)], GUARD)
    assert out.admitted[0][1].reason == KEYWORD_ONLY
    assert out.admitted[0][1].admitted is True
    assert out.top_score is None


def test_nothing_is_dropped_silently():
    """Every input row comes back somewhere, with a reason. A retrieval layer
    that discards rows without saying so cannot be falsified."""
    out = apply_guard(_rows(0.9, 0.5, 0.1), GUARD)
    assert len(out.all_scored) == 3
    assert {v.reason for _, v in out.all_scored} == {ADMITTED, BELOW_ALPHA, BELOW_FLOOR}


def test_empty_and_all_rejected_are_distinguishable():
    """"Nothing matched" and "nine things matched and all were noise" are very
    different answers, and a bare empty list conflates them."""
    empty = apply_guard([], GUARD).summary()
    noise = apply_guard(_rows(0.30, 0.28), GUARD).summary()
    assert empty["admitted"] == 0 and empty["rejected_below_floor"] == 0
    assert noise["admitted"] == 0 and noise["rejected_below_floor"] == 2
    assert empty != noise


def test_verdict_carries_the_thresholds_that_judged_it():
    """A score with no threshold attached is unreviewable after the fact."""
    verdict = apply_guard(_rows(0.9), GUARD).admitted[0][1].as_dict()
    assert verdict["absolute_floor"] == 0.45
    assert verdict["alpha"] == 0.85
    assert verdict["cosine"] == 0.9


def test_an_uncalibrated_floor_says_so():
    """A floor nobody has measured against this namespace is a server default
    wearing a number's clothes. It must not read as a measurement."""
    assert apply_guard(_rows(0.9), GUARD).admitted[0][1].as_dict()["calibrated"] is False
    calibrated = dict(GUARD, calibration_ts="2026-08-04T00:00:00Z",
                      temporal_mode="historical_snapshot")
    verdict = apply_guard(_rows(0.9), calibrated).admitted[0][1].as_dict()
    assert verdict["calibrated"] is True
    assert verdict["temporal_mode"] == "historical_snapshot"


def test_a_namespace_floor_overrides_the_default():
    lenient = dict(GUARD, gate_similarity_floor=0.20, gate_top_fraction_alpha=0.10)
    assert len(apply_guard(_rows(0.30, 0.25), lenient).admitted) == 2


def test_missing_guard_keys_fall_back_to_the_server_defaults():
    """Failing to the default is the only safe direction for a value whose job
    is suppression: an absent floor would silently restore v1 behaviour."""
    verdict = apply_guard(_rows(0.9), {}).admitted[0][1]
    assert verdict.absolute_floor == 0.45 and verdict.alpha == 0.85


def test_policy_block_names_every_surface_the_guard_governs():
    block = policy_block(GUARD)
    assert set(block["applies_to"]) == {"memory_search", "intent_open", "recall"}
    assert block["calibrated"] is False
    # The escalation rule is restated at every read, because conflating a
    # retrieval floor with a violation signal is the defect this work undoes.
    assert "DISPLAY-ONLY" in block["note"]


def test_a_verdict_cannot_be_edited_after_the_fact():
    verdict = apply_guard(_rows(0.9), GUARD).admitted[0][1]
    with pytest.raises(Exception):
        verdict.admitted = False  # type: ignore[misc]


def test_the_gate_uses_this_exact_implementation():
    """Not "agrees with" — IS. Two copies that agree today drift by the next
    release, and the drift is invisible until someone compares two tools."""
    from storage.gate import _guarded_candidates

    candidates = [{"key": "a", "similarity": 0.90}, {"key": "b", "similarity": 0.46},
                  {"key": "c", "similarity": 0.30}]
    assert [c["key"] for c in _guarded_candidates(candidates, GUARD)] == ["a"]
    assert _guarded_candidates([], GUARD) == []
