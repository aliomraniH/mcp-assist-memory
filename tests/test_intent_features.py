"""[CI] Deterministic intent-feature extraction and trigger validation.

These are the two halves of the predicate-first escalation rule. They run
against local Postgres-free pure functions, so they are genuinely [CI]-class:
nothing here depends on Neon topology or the deployed server.
"""
from __future__ import annotations

import pytest

from storage.intent_features import extract_features, model_available
from storage.triggers import (
    ALLOWED_OPERATORS,
    ALLOWED_VARS,
    evaluate_trigger,
    trigger_is_valid,
    validate_trigger,
)

pytestmark = pytest.mark.ci

requires_model = pytest.mark.skipif(
    not model_available(),
    reason="[CI] spaCy en_core_web_sm not installed in this environment",
)

# The trigger authored for skill/no-sorted-fold-replay. It matches the
# PROHIBITED case (replaying in sorted/timestamp order), never the compliant
# one — which is the entire lesson of validation FINDING-4.
NO_SORTED_FOLD_REPLAY_TRIGGER = {
    "and": [
        {"in": [{"var": "action"}, ["replay", "rebuild", "fold", "reduce"]]},
        {
            "or": [
                {"in": ["timestamp", {"var": "condition"}]},
                {"in": ["sorted", {"var": "condition"}]},
                {"in": ["sort", {"var": "condition"}]},
                {"in": ["chronological", {"var": "condition"}]},
            ]
        },
    ]
}


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------
@requires_model
def test_extract_features_always_returns_the_full_key_set():
    """A predicate must never see a missing variable and take a different
    branch than the fixture that pinned it."""
    for goal in ("Rebuild the projection", "", "   ", "asdf"):
        f = extract_features(goal)
        assert set(f) >= {"action", "object", "condition", "raw", "negated"}


@requires_model
def test_condition_separates_compliant_from_violating_replay():
    """The distinction cosine cannot make. Both goals are near-identical in
    embedding space (documented negation-blindness, NegBench arXiv:2501.09425);
    they differ only inside the prepositional scope."""
    compliant = extract_features(
        "Rebuild the projection by replaying the event log in insertion order")
    violating = extract_features(
        "Rebuild the projection by replaying the event log in timestamp order")

    assert "insertion" in compliant["condition"]
    assert "timestamp" not in compliant["condition"]
    assert "timestamp" in violating["condition"]
    assert "insertion" not in violating["condition"]


@requires_model
def test_action_and_object_are_lemmas():
    f = extract_features("Rebuild the projections by replaying the event logs")
    assert f["action"] == "rebuild"
    assert f["object"] == "projection"  # lemma, not "projections"


@requires_model
def test_clinical_extraction_withholds_raw_goal_text():
    """PHI hard gate: no free text may reach a predicate, a match-log row, or
    an audit block in a clinical namespace."""
    goal = "Rebuild the projection for patient Jane Doe in timestamp order"
    f = extract_features(goal, clinical=True)
    assert f["raw"] is None
    # Structured lemma features still work, so the gate keeps functioning.
    assert f["action"] == "rebuild"
    assert "timestamp" in f["condition"]


@requires_model
def test_negation_flag_is_extracted():
    assert extract_features("Do not replay the log in timestamp order")["negated"]
    assert not extract_features("Replay the log in insertion order")["negated"]


def test_extraction_degrades_to_display_only_without_the_model(monkeypatch):
    """A failed model load must not take the gate down. Without features every
    trigger simply fails to match — fail toward silence, never toward a
    spurious escalation."""
    import storage.intent_features as ifeat

    monkeypatch.setattr(ifeat, "_NLP", None)
    monkeypatch.setattr(ifeat, "_NLP_FAILED", True)
    f = ifeat.extract_features("Replay the log in timestamp order")
    assert f["action"] is None and f["condition"] == ""
    assert f["extractor"] == "unavailable"
    assert evaluate_trigger(NO_SORTED_FOLD_REPLAY_TRIGGER, f) is False


@requires_model
def test_oversized_goal_is_truncated_not_parsed_whole():
    f = extract_features("replay the log " * 5000)
    assert len(f["raw"]) <= 2000


# ---------------------------------------------------------------------------
# Trigger validation — fail closed
# ---------------------------------------------------------------------------
def test_authored_trigger_validates():
    assert validate_trigger(NO_SORTED_FOLD_REPLAY_TRIGGER) == []


@pytest.mark.parametrize("bad,reason", [
    (None, "null"),
    ({}, "empty"),
    ("replay in timestamp order", "prose is not a predicate"),
    ([{"var": "action"}], "top level must be an object"),
    ({"+": [1, 2]}, "arithmetic is not whitelisted"),
    ({"method": ["os", "system"]}, "method is not whitelisted"),
    ({"==": [{"var": "namespace"}, "x"]}, "var not in whitelist"),
    ({"==": [{"var": "__class__"}, "x"]}, "dunder var not in whitelist"),
    ({"==": [{"var": "action"}, "<script>alert(1)</script>"]}, "executable literal"),
    ({"==": [{"var": "action"}, "ignore all previous instructions"]}, "injection literal"),
    ({"==": [{"var": "action"}, "x"], "!=": [{"var": "object"}, "y"]}, "two operator keys"),
])
def test_forged_predicate_is_rejected(bad, reason):
    """forged_predicate + predicate_injection fixtures. Every one of these must
    fail CLOSED — the skill stays display-only rather than escalating."""
    assert validate_trigger(bad), f"should have been rejected: {reason}"
    assert not trigger_is_valid(bad)
    assert evaluate_trigger(bad, {"action": "x"}) is None


def test_deeply_nested_trigger_is_rejected():
    node: dict = {"==": [{"var": "action"}, "replay"]}
    for _ in range(12):
        node = {"and": [node]}
    assert any("depth" in e or "nodes" in e for e in validate_trigger(node))


def test_whitelists_are_closed_sets():
    """Pinned so widening the trigger language is a deliberate, reviewed act
    rather than a drive-by import."""
    assert ALLOWED_OPERATORS == {"==", "!=", "in", "and", "or", "!", "var"}
    assert ALLOWED_VARS == {"action", "object", "condition", "raw"}


def test_evaluate_distinguishes_false_from_unevaluable():
    """None is not False. 'the predicate says compliant' and 'there is no usable
    predicate' are different facts, and only the first is a decision."""
    features = {"action": "schedule", "object": "catering", "condition": "", "raw": ""}
    assert evaluate_trigger(NO_SORTED_FOLD_REPLAY_TRIGGER, features) is False
    assert evaluate_trigger({"+": [1, 2]}, features) is None


def test_trigger_cannot_read_features_outside_the_whitelist():
    """`negated` and `extractor` are extracted but are not part of the trigger
    vocabulary; a predicate referencing them fails validation rather than
    silently reading them."""
    assert validate_trigger({"==": [{"var": "negated"}, True]})
    assert validate_trigger({"==": [{"var": "extractor"}, "spacy/en_core_web_sm"]})


# ---------------------------------------------------------------------------
# The two live failures from validation FINDING-4, as pure-function fixtures.
# ---------------------------------------------------------------------------
@requires_model
def test_catering_false_positive_predicate_does_not_match():
    """[CI] catering_false_positive (predicate half). The goal that produced a
    live gate_conflict at cosine 0.288 against an event-log skill."""
    features = extract_features("Schedule the quarterly workshop catering")
    assert evaluate_trigger(NO_SORTED_FOLD_REPLAY_TRIGGER, features) is False


@requires_model
def test_compliant_insertion_order_predicate_does_not_match():
    """[CI] compliant_insertion_order (predicate half). The goal that OBEYS the
    skill and was conflicted against it anyway."""
    features = extract_features(
        "Rebuild the projection by replaying the event log in insertion order")
    assert evaluate_trigger(NO_SORTED_FOLD_REPLAY_TRIGGER, features) is False


@requires_model
def test_violating_timestamp_order_predicate_matches():
    """The case the skill actually prohibits — this one MUST escalate."""
    features = extract_features(
        "Rebuild the projection by replaying the event log in timestamp order")
    assert evaluate_trigger(NO_SORTED_FOLD_REPLAY_TRIGGER, features) is True
