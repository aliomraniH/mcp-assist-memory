"""Deterministic clinical PHI gate for the apply-worker (write side).

The curator's LLM is *told* never to emit PHI (see docs/memory-curator.md), but the
apply-worker must not trust that: this module is the deterministic, fail-closed
second line that runs on EVERY operation before it is written. A dropped memory is
recoverable; a leaked patient identifier is not — so when in doubt, drop.

``assert_no_phi(op) -> bool`` returns True when an op is safe to write and False
when any of its written-through fields (value, key, subjects, tags, meta, and the
two embedding strings) carry something that looks patient-identifying. It is
conservative on purpose: it flags identifier-shaped strings (SSN, email, phone,
long digit runs) and identifier-named keys (mrn, patient, dob, …). It never raises.
"""
from __future__ import annotations

import re
from typing import Any

# Identifier-shaped value patterns. These are deliberately broad: a false positive
# only drops a memory (recoverable); a false negative leaks PHI (not).
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"\b(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
# A standalone run of 9+ digits is identifier-shaped (MRN / SSN / account / member
# id). Short numbers (PR #, salience, years) are left alone; hex SHAs contain
# letters and never form a 9-digit pure-decimal run.
_LONG_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d)")

# Field/key names that, when present with any non-empty value, mark the op as
# carrying patient-identifying detail. Matched case-insensitively as a substring of
# the key so "patient_name", "ptMRN", "date_of_birth" all trip.
_PHI_KEY_TOKENS = (
    "patient",
    "mrn",
    "ssn",
    "dob",
    "date_of_birth",
    "birth_date",
    "birthdate",
    "first_name",
    "last_name",
    "full_name",
    "given_name",
    "family_name",
    "home_address",
    "street_address",
    "phone",
    "email",
    "insurance_id",
    "member_id",
    "medical_record",
    "diagnosis_code",
)

# Fields of an operation the worker writes through to the store. Only these are
# scanned; bookkeeping fields like ``op`` / ``reason`` are not written verbatim.
_SCANNED_OP_FIELDS = ("value", "key", "subjects", "tags", "meta", "embeddings")


def _value_looks_identifying(text: str) -> bool:
    return bool(
        _SSN.search(text)
        or _EMAIL.search(text)
        or _PHONE.search(text)
        or _LONG_DIGITS.search(text)
    )


def _key_looks_identifying(key: str) -> bool:
    low = key.lower()
    return any(tok in low for tok in _PHI_KEY_TOKENS)


def _walk_has_phi(node: Any) -> bool:
    """Recursively scan a JSON-ish node. True if any string value is
    identifier-shaped or any dict key is identifier-named (with a non-empty value)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and _key_looks_identifying(k) and v not in (None, "", [], {}):
                return True
            if _walk_has_phi(v):
                return True
        return False
    if isinstance(node, (list, tuple, set)):
        return any(_walk_has_phi(v) for v in node)
    if isinstance(node, str):
        return _value_looks_identifying(node)
    # Numeric scalars are identifier-shaped too: a raw int/float like 123456789 (MRN /
    # SSN / account) carries no quotes but is just as identifying. Stringify and apply
    # the same long-digit test. bool is an int subclass — exclude it (True/False aren't
    # identifiers). Decimals are scanned via str() as well.
    if isinstance(node, bool):
        return False
    if isinstance(node, (int, float)):
        return _value_looks_identifying(format(node, "f") if isinstance(node, float) else str(node))
    return False


def assert_no_phi(op: dict) -> bool:
    """Return True if ``op`` is safe to write, False if it carries (or might carry)
    patient-identifying detail. Fail-closed: a malformed op returns False."""
    if not isinstance(op, dict):
        return False
    try:
        for field in _SCANNED_OP_FIELDS:
            if field in op and _walk_has_phi(op[field]):
                return False
    except Exception:  # noqa: BLE001 - any scan failure is treated as unsafe
        return False
    return True
