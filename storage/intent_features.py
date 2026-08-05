"""Deterministic intent-feature extraction for Intent Gate Tier 1.

WHY THIS IS NOT AN LLM CALL
    The gate must decide, on the hot path, whether a declared intent VIOLATES a
    stored anti-pattern. The v1 gate tried to answer that with embedding
    proximity and got it wrong in both directions: an unrelated catering goal
    was conflicted against an event-log skill at cosine 0.288, and a goal that
    explicitly OBEYED that skill was conflicted against it as well. That second
    failure is not a tuning problem. Dense encoders are documented to be
    negation-blind (NegBench, CVPR 2025, arXiv:2501.09425), so "replay in
    insertion order" and "replay in timestamp order" sit next to each other in
    embedding space while meaning opposite things. No threshold separates them.

    So escalation is decided by a structured predicate over structured features,
    and this module produces the features. It is spaCy, not a model call:
    deterministic, ~1ms, no network, no token cost, and auditable — the same
    goal always yields the same features, which is what makes a gate_conflict
    explainable and a fixture stable.

THE FEATURE SET is deliberately small. Four variables are exposed to trigger
predicates (see storage/triggers.py for the whitelist):

    action     ROOT-verb lemma            "rebuild", "replay", "schedule"
    object     direct-object lemma        "projection", "log", "catering"
    condition  prepositional-phrase and adverbial scope, lemmatised and
               space-joined — this is where the distinction that actually
               matters usually lives ("insertion order" vs "timestamp order")
    raw        the normalised goal text, for predicates that need a substring
               escape hatch

`negated` is extracted too and surfaced in the verbose gate_audit, but it is
NOT in the predicate variable whitelist: the trigger vocabulary stays exactly
the documented {action, object, condition, raw} so a skill author reading the
tool description sees the whole language.

PHI: this module returns lemmas and a normalised copy of the goal. Callers in
clinical namespaces must use `extract_features(..., clinical=True)`, which
withholds `raw` so no free text can reach a predicate, a log row, or an audit
block. The extracted lemma labels are the same class of data the existing gate
already persists in gate_intent.labels.
"""
from __future__ import annotations

import re
import threading
from typing import Any

# spaCy's model load is ~0.5s and thread-unsafe to race, so it happens once and
# is reused. A failed load must NOT take the gate down: without features every
# trigger simply fails to match, which degrades to display-only — the same
# fail-toward-silence direction the rest of this design takes.
_NLP: Any = None
_NLP_LOCK = threading.Lock()
_NLP_FAILED = False

MODEL_NAME = "en_core_web_sm"

# Parsing a pathological 100KB "goal" on the hot path is a denial-of-service
# shape, not a feature. Goals are one sentence by contract.
MAX_GOAL_CHARS = 2000

_WS = re.compile(r"\s+")

# Function words carry no discriminating power in a condition string and only
# create accidental substring hits ("in order" matching "in insertion order").
_CONDITION_STOP = {
    "the", "a", "an", "this", "that", "these", "those", "its", "their",
    "be", "is", "are", "was", "were", "do", "does", "did",
}


def _nlp() -> Any:
    """Load the model once. Returns None if unavailable, never raises."""
    global _NLP, _NLP_FAILED
    if _NLP is not None or _NLP_FAILED:
        return _NLP
    with _NLP_LOCK:
        if _NLP is not None or _NLP_FAILED:
            return _NLP
        try:
            import spacy
            # Only the tagger/parser/lemmatiser are needed. NER is the
            # expensive component and nothing here reads entities, so disabling
            # it is most of the hot-path budget.
            _NLP = spacy.load(MODEL_NAME, disable=["ner", "textcat"])
        except Exception:  # pragma: no cover - environment-dependent
            _NLP_FAILED = True
            _NLP = None
    return _NLP


def model_available() -> bool:
    """True when feature extraction can actually run. Surfaced in the audit so
    an operator can tell 'no trigger matched' from 'the extractor is down'."""
    return _nlp() is not None


def normalise_goal(goal: str) -> str:
    return _WS.sub(" ", (goal or "").strip())[:MAX_GOAL_CHARS]


def extract_features(goal: str, *, clinical: bool = False) -> dict:
    """Extract {action, object, condition, raw, negated} from a declared intent.

    Always returns the full key set — a predicate must never see a missing
    variable and take a different branch than the fixture that pinned it.
    Unextractable fields are None (or "" for condition), which no whitelisted
    operator can match by accident.
    """
    text = normalise_goal(goal)
    features: dict = {
        "action": None,
        "object": None,
        "condition": "",
        # PHI hard gate: clinical namespaces never expose free text to a
        # predicate, a match-log row, or an audit block.
        "raw": None if clinical else text.lower(),
        "negated": False,
        "extractor": "spacy/" + MODEL_NAME,
    }
    if not text:
        return features
    nlp = _nlp()
    if nlp is None:
        features["extractor"] = "unavailable"
        return features

    doc = nlp(text)

    root = None
    for token in doc:
        if token.dep_ == "ROOT":
            root = token
            break
    if root is None:
        return features

    # ACTION.
    #
    # Declared intents are imperatives by contract ("Rebuild the projection",
    # "Schedule the catering"), and the leading verb of an imperative IS the
    # action. Anchoring on that is not just a convenience — en_core_web_sm
    # mis-roots a noticeable fraction of imperatives, especially when the
    # sentence ends in a plural noun it can read as a verb. "Rebuild the
    # projections by replaying the event logs" parses with ROOT="logs" tagged
    # VERB, which would yield action="log" and silently miss the trigger. The
    # failure direction is safe (no escalation) but it is still wrong, and a
    # gate whose predicates fire on singulars and not plurals is not
    # deterministic in any useful sense.
    #
    # So: leading verb wins when the goal is imperative-shaped; otherwise fall
    # back to the ROOT verb, then to the first verb anywhere ("We should replay
    # the log" -> replay).
    action_token = None
    if doc[0].pos_ == "VERB":
        action_token = doc[0]
    elif root.pos_ in ("VERB", "AUX"):
        action_token = root
    else:
        action_token = next((t for t in doc if t.pos_ == "VERB"), root)
    features["action"] = action_token.lemma_.lower()

    # OBJECT. Prefer the direct object of the action verb; fall back to any
    # dobj, then to the ROOT itself when the ROOT is nominal.
    dobj = next((c for c in action_token.children if c.dep_ in ("dobj", "obj")), None)
    if dobj is None:
        dobj = next((t for t in doc if t.dep_ in ("dobj", "obj")), None)
    if dobj is not None:
        features["object"] = dobj.lemma_.lower()
    elif root.pos_ in ("NOUN", "PROPN"):
        features["object"] = root.lemma_.lower()

    # CONDITION. Everything inside prepositional and adverbial modifiers, in
    # document order, lemmatised. This is the scope where the compliant/
    # violating distinction almost always lives: the difference between
    # "replay ... in insertion order" and "replay ... in timestamp order" is
    # invisible to cosine and obvious here.
    condition_tokens: list[str] = []
    for token in doc:
        if token.dep_ not in ("prep", "agent", "advmod", "prt", "npadvmod"):
            continue
        for sub in token.subtree:
            if sub.is_punct or sub.is_space:
                continue
            if sub.dep_ == "prep" or sub.pos_ in ("ADP", "PART"):
                continue
            lemma = sub.lemma_.lower()
            if lemma in _CONDITION_STOP:
                continue
            condition_tokens.append(lemma)
    # De-duplicate while preserving order: a repeated lemma adds no signal but
    # would change a substring predicate's behaviour unpredictably.
    seen: set[str] = set()
    ordered = [t for t in condition_tokens if not (t in seen or seen.add(t))]
    features["condition"] = " ".join(ordered)

    # NEGATION. Audit-only (see module docstring) but genuinely load-bearing
    # for a human reading why a predicate did or did not fire.
    features["negated"] = any(t.dep_ == "neg" for t in doc)

    return features
