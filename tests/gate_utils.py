"""Shared helpers for the intent-gate test modules (not a test file).

The gate is rolled out per-namespace via variant_profiles (the codebase's own
staged-rollout mechanism, S4) — these helpers flip a test namespace's profile
and build gated backends over the same real-Postgres fixtures as conftest.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb


async def set_profile(backend, ns: str, profile: dict) -> None:
    async with backend.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO variant_profiles (namespace, profile) VALUES (%s, %s) "
            "ON CONFLICT (namespace) DO UPDATE SET profile = EXCLUDED.profile",
            (ns, Jsonb(profile)),
        )
    backend._profile_cache.pop(ns, None)


GATE_ON = {"intent_gate": "on"}


def unwrap(value):
    """Strip the untrusted-data markers for exact-match assertions."""
    if isinstance(value, str):
        return (value.replace("<<<UNTRUSTED_DATA>>>", "")
                     .replace("<<<END>>>", ""))
    return value


# Seed entries mirroring claude/intent-gate/tests/fixtures/gate_fixtures.json.
SKILL_ANTI_PATTERN = {
    "key": "skill/no-sorted-fold-replay",
    "kind": "knowledge",
    "value": ("ANTI-PATTERN: replaying an event log by (occurred_at, event_id) "
              "sort breaks sticky-tombstone resurrection; replay must fold in "
              "insertion order (rowid ASC). Observed failing in 3 of 6 A/B arms."),
    "meta": {
        "polarity": "anti-pattern",
        "trigger_intent": "implement event log replay / projection rebuild / reconcile fold order",
        "efficacy": {"applied": 0, "prevented_error": 0, "false_positive": 0},
        "last_validated": "2026-07-25T00:00:00Z",
        "origin_model_family": "claude",
        "curator_provenance": True,
        # AUTHORED TRIGGER (remediation 1e). Written to match the PROHIBITED
        # case, never the compliant one — the distinction the v1 gate could not
        # make. The skill's guidance is "replay must fold in insertion order";
        # the violation is folding in sorted/timestamp order, and that lives in
        # the prepositional scope the feature extractor exposes as `condition`:
        #
        #   "...replaying the event log sorted by timestamp"
        #        -> condition "replay event log sort timestamp"   MATCHES
        #   "...replaying the event log in insertion order"
        #        -> condition "replay event log insertion order"  DOES NOT
        #
        # Both goals sit at effectively the same cosine distance from this
        # skill, which is exactly why escalation cannot be a similarity
        # question.
        "trigger": {
            "and": [
                {"in": [{"var": "action"},
                        ["replay", "rebuild", "fold", "reduce", "reconcile"]]},
                {"or": [
                    {"in": ["timestamp", {"var": "condition"}]},
                    {"in": ["sort", {"var": "condition"}]},
                    {"in": ["chronological", {"var": "condition"}]},
                    {"in": ["occurred_at", {"var": "condition"}]},
                ]},
            ]
        },
        "trigger_author": "curator",
        "trigger_temporal_mode": "historical_snapshot",
        "trigger_calibration_ts": "2026-08-04T00:00:00Z",
    },
}

# Same skill, no trigger — the state EVERY existing anti-pattern skill is in on
# deploy until one is authored. Pins the deliberate fail-toward-silence
# behaviour so it can never regress back to escalating on proximity.
SKILL_ANTI_PATTERN_NO_TRIGGER = {
    "key": "skill/no-trigger-antipattern",
    "kind": "knowledge",
    "value": ("ANTI-PATTERN: replaying an event log by (occurred_at, event_id) "
              "sort breaks sticky-tombstone resurrection; replay must fold in "
              "insertion order (rowid ASC)."),
    "meta": {
        "polarity": "anti-pattern",
        "trigger_intent": "implement event log replay projection rebuild fold order",
        "last_validated": "2026-07-25T00:00:00Z",
        "curator_provenance": True,
    },
}

SKILL_EXPIRED = {
    "key": "skill/expired-example",
    "kind": "knowledge",
    "value": "ANTI-PATTERN (STALE FIXTURE): do not use Postgres jsonb round-trip for fingerprints.",
    "meta": {
        "polarity": "anti-pattern",
        "trigger_intent": "compute idempotency fingerprint",
        "last_validated": "2026-01-01T00:00:00Z",
        "curator_provenance": True,
    },
}

DECISION_TARGET_BRANCH = {
    "key": "decision/target-branch",
    "kind": "knowledge",
    "value": "All gate-phase commits land on feat/intent-gate-p1; main is release-only.",
    "meta": {"structured": {"allowed_branch": "feat/intent-gate-p1"}},
}


async def seed(backend, ns: str, entry: dict) -> dict:
    return await backend.memory_save(
        ns, entry["key"], entry["value"], kind=entry["kind"], meta=entry["meta"],
        actor="seed-writer", origin="tool",
    )
