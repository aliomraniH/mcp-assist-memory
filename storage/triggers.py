"""Skill trigger predicates: validation (fail-closed) and evaluation.

WHAT A TRIGGER IS
    A JSON-Logic object stored on an anti-pattern skill that answers one
    question deterministically: does this declared intent COMMIT the prohibited
    thing? It is evaluated against the features extracted in
    storage/intent_features.py — {action, object, condition, raw} — and its
    truth is the ONLY thing that can escalate an intent to gate_conflict.

WHY IT REPLACED THE EMBEDDING LEG
    v1 escalated on cosine proximity. That conflicts an unrelated catering goal
    against an event-log skill (measured: 0.288) and, worse, conflicts a goal
    that OBEYS the skill, because compliance and violation are near-identical
    in embedding space. A predicate distinguishes them because it reads the
    scope where the difference lives: `condition` contains "insertion order" in
    one case and "timestamp order" in the other.

THE TRUST BOUNDARY
    A trigger arrives from stored memory. Stored memory is untrusted data —
    that is the whole premise of this server's read-time wrapper. A trigger is
    therefore DATA THAT SELECTS A BRANCH, never code that runs. Three
    restrictions enforce that, and all three fail CLOSED (invalid trigger =>
    the skill is display-only and can never escalate, which is strictly safer
    than the v1 behaviour of escalating on proximity):

      1. Operator whitelist. Only the comparison/boolean operators below. No
         arithmetic, no `method`, no variable-arity data access beyond `var`.
      2. Variable whitelist. Only the four documented feature names. A trigger
         cannot reach for `__class__`, a namespace, or anything not extracted.
      3. Structural limits. Bounded depth and node count, and string literals
         are rejected if they look like embedded executable content.

    Note what is NOT claimed here: this is a whitelist over a data language, not
    a sandbox for arbitrary code, and json-logic evaluation of whitelisted
    operators over string/bool literals has no I/O surface to escape through.
    The security property rests on the whitelist being closed, which is why
    unknown operators are an error rather than a skip.

DEPENDENCY NOTE: panzi-json-logic is the maintained fork. nadirizr/json-logic-py
is discontinued and must not be substituted for it.
"""
from __future__ import annotations

import re
from typing import Any

# The complete trigger language. Deliberately tiny: everything needed to say
# "this action, on this object, under this condition" and nothing else.
#
#   ==  !=        equality on extracted lemmas
#   in            substring test against `condition` / `raw`, or membership in
#                 a literal list — the workhorse operator
#   and or !      composition
#   if            NOT included: a predicate returns a verdict, it does not
#                 branch, and excluding it keeps every trigger readable as a
#                 single boolean sentence.
ALLOWED_OPERATORS = frozenset({"==", "!=", "in", "and", "or", "!", "var"})

# The trigger vocabulary, verbatim from the skill_define tool description.
ALLOWED_VARS = frozenset({"action", "object", "condition", "raw"})

MAX_DEPTH = 8
MAX_NODES = 100
MAX_STRING_LEN = 200

# A trigger literal is a lemma or a short phrase. Anything that looks like a
# script tag, a template expression, a prompt injection, or a python dunder is
# rejected outright — not because the evaluator would run it, but because a
# literal shaped like code means the trigger was authored by something that
# misunderstood what a trigger is, and that is worth failing on loudly.
_SUSPICIOUS = re.compile(
    r"(<\s*script|javascript:|\{\{|\}\}|\$\{|__[a-z]+__|\beval\b|\bexec\b"
    r"|\bimport\b|\bignore\s+(all\s+)?previous\b|\bsystem\s+prompt\b)",
    re.IGNORECASE,
)


class TriggerError(ValueError):
    """A trigger failed validation. Carries the machine-readable reasons."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_trigger(trigger: Any) -> list[str]:
    """Return a list of schema errors. Empty list == valid.

    Never raises on malformed input — a curator-drafted trigger is expected to
    be wrong sometimes, and the caller records the errors rather than crashing.
    """
    errors: list[str] = []
    if trigger is None:
        return ["trigger is null"]
    if not isinstance(trigger, dict):
        return [f"trigger must be a JSON object, got {type(trigger).__name__}"]
    if not trigger:
        return ["trigger is an empty object"]

    counter = {"nodes": 0}
    _walk(trigger, 0, errors, counter)
    if counter["nodes"] > MAX_NODES:
        errors.append(f"trigger exceeds {MAX_NODES} nodes")
    return errors


def _walk(node: Any, depth: int, errors: list[str], counter: dict) -> None:
    counter["nodes"] += 1
    if counter["nodes"] > MAX_NODES:
        return
    if depth > MAX_DEPTH:
        errors.append(f"trigger exceeds maximum depth {MAX_DEPTH}")
        return

    if isinstance(node, dict):
        if len(node) != 1:
            errors.append(
                "each JSON-Logic node must have exactly one operator key, "
                f"got {sorted(node)!r}")
            return
        op, args = next(iter(node.items()))
        if op not in ALLOWED_OPERATORS:
            errors.append(f"operator {op!r} is not whitelisted")
            return
        if op == "var":
            _check_var(args, errors)
            return
        for arg in (args if isinstance(args, list) else [args]):
            _walk(arg, depth + 1, errors, counter)
        return

    if isinstance(node, str):
        if len(node) > MAX_STRING_LEN:
            errors.append(f"string literal exceeds {MAX_STRING_LEN} chars")
        if _SUSPICIOUS.search(node):
            errors.append(f"string literal looks executable/injected: {node[:60]!r}")
        return

    if isinstance(node, list):
        for arg in node:
            _walk(arg, depth + 1, errors, counter)
        return

    if isinstance(node, (bool, int, float)) or node is None:
        return

    errors.append(f"unsupported literal type {type(node).__name__}")


def _check_var(args: Any, errors: list[str]) -> None:
    name = args[0] if isinstance(args, list) and args else args
    if not isinstance(name, str):
        errors.append(f"var name must be a string, got {type(name).__name__}")
        return
    if name not in ALLOWED_VARS:
        errors.append(
            f"var {name!r} is not in the whitelist {sorted(ALLOWED_VARS)}")


def trigger_is_valid(trigger: Any) -> bool:
    return not validate_trigger(trigger)


def evaluate_trigger(trigger: Any, features: dict) -> bool | None:
    """Evaluate a validated trigger against extracted features.

    Returns True/False, or None when the trigger could not be evaluated at all
    (invalid, or the evaluator raised). None is NOT False: the caller
    distinguishes "the predicate says this intent is compliant" from "there is
    no usable predicate here", and only the former is a decision.

    Fail-closed direction: an unevaluable trigger never escalates.
    """
    if validate_trigger(trigger):
        return None
    # Only whitelisted variables are exposed. Passing the raw feature dict would
    # leak `negated` and `extractor` into a language whose documented vocabulary
    # is four names.
    data = {k: features.get(k) for k in ALLOWED_VARS}
    # `in` against None would raise inside the evaluator; normalise the two
    # string-valued features so a missing extraction is simply "no match".
    for key in ("condition", "raw"):
        if data.get(key) is None:
            data[key] = ""
    try:
        from json_logic import jsonLogic
        return bool(jsonLogic(trigger, data))
    except Exception:
        return None
