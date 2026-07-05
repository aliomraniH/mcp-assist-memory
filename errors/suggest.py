"""Did-you-mean support for the R6 S-hint arm (Phase 7, T7.3)."""
from __future__ import annotations


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def did_you_mean(name: str, valid: list[str]) -> str:
    """S-hint text: a ≤2-edit suggestion when one exists, else the valid list."""
    best = min(valid, key=lambda v: levenshtein(name.lower(), v.lower()), default=None)
    if best is not None and levenshtein(name.lower(), best.lower()) <= 2:
        return f"unknown argument {name!r} — did you mean {best!r}?"
    return f"unknown argument {name!r}; valid arguments: {', '.join(sorted(valid))}"
