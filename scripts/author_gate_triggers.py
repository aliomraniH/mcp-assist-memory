#!/usr/bin/env python3
"""Author trigger predicates for EXISTING anti-pattern skills (remediation 1e).

WHY THIS SCRIPT EXISTS
    Phase 1 made escalation predicate-first. The direct consequence, stated
    plainly: every anti-pattern skill that already exists has trigger = NULL and
    is therefore DISPLAY-ONLY from the moment the new build is live. Until a
    trigger is authored for it, the gate produces zero gate_conflict escalations
    from that skill.

    That direction is deliberate — the conflict stream it replaces was
    false-positive dominated, and a gate that cries wolf trains its operator to
    ignore it. But "deliberate" is not "invisible". This script is how the
    silence gets ended for the skills that deserve a predicate, and running it
    is a mechanical step rather than a research project.

DRAFTING PROVENANCE — READ BEFORE TRUSTING trigger_author
    The remediation design routes predicate drafting through the curator's
    direct Anthropic API path (MCP sampling is deprecated). The predicates below
    were NOT produced that way: the branch was built in an environment with no
    ANTHROPIC_API_KEY, so the curator could not be called. They were drafted by
    the implementing model and then put through the SAME deterministic validator
    the curator's output would face (storage/triggers.validate_trigger) — which
    is the part that actually gates persistence.

    Practical difference: none for correctness (the validator is the gate).
    Real difference for attribution: nobody should read trigger_author="curator"
    on these rows and conclude the deployed curator reviewed them. Each authored
    trigger below therefore also carries meta.trigger_drafted_by so the record is
    accurate rather than flattering.

WHAT IT WILL NOT DO
    It will not write to the probe namespaces (dev/gate-probe-20260803*). Those
    rows are the validation run's evidence and are left in place deliberately.
    They are listed by --report so the operator can SEE which live skills are
    display-only, without this script mutating any of them.

USAGE
    python scripts/author_gate_triggers.py --report            # read-only survey
    python scripts/author_gate_triggers.py --namespace NS --dry-run
    python scripts/author_gate_triggers.py --namespace NS --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

# Namespaces whose rows are validation evidence. Writes here are refused, not
# merely discouraged — the point of leaving them untouched is that a later
# reader can reproduce the original finding.
PROTECTED_NAMESPACES = (
    "dev/gate-probe-20260803",
    "dev/gate-probe-20260803-control",
    "dev/gate-probe-clinical-20260803",
)

# ---------------------------------------------------------------------------
# The authored predicates.
#
# Each is written to match the PROHIBITED case, never the compliant one — the
# distinction the v1 gate could not make. Each carries a paired fixture in
# tests/test_gate_remediation_p1.py: one intent that MUST escalate and one that
# MUST NOT. A trigger with only a positive case is indistinguishable from the
# always-escalate bug this replaces.
# ---------------------------------------------------------------------------
AUTHORED: list[dict] = [
    {
        "key": "skill/no-sorted-fold-replay",
        "polarity": "anti-pattern",
        "guidance": (
            "ANTI-PATTERN: replaying an event log by (occurred_at, event_id) "
            "sort breaks sticky-tombstone resurrection; replay must fold in "
            "insertion order (rowid ASC). Observed failing in 3 of 6 A/B arms."
        ),
        # Prohibited: folding/replaying in sorted or timestamp order.
        # Compliant: folding/replaying in insertion order — same action, same
        # object, opposite condition, and near-identical embedding.
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
        "violating_example": (
            "rebuild the projection by replaying the event log sorted by timestamp"),
        "compliant_example": (
            "rebuild the projection by replaying the event log in insertion order"),
    },
]

# Anti-pattern skills observed live that are NOT given a trigger here, with the
# reason. Listed so the completion record can name them instead of leaving a
# reader to assume they were simply missed.
NEEDS_HUMAN_TRIGGER: list[dict] = [
    {
        "key": "skill/expired-example",
        "namespace": "dev/gate-probe-20260803",
        "reason": ("deliberately-expired test fixture (last_validated "
                   "2026-01-01) — already advisory-only under the S7 window; "
                   "a trigger would not change its behaviour"),
    },
    {
        "key": "skill/forged-veto",
        "namespace": "dev/gate-probe-20260803",
        "reason": ("adversarial fixture with no curator_provenance — cannot "
                   "escalate under S7 regardless of trigger; authoring one "
                   "would misrepresent it as legitimate"),
    },
]


def validate_all() -> tuple[list[str], list[str]]:
    """Run every authored predicate through the deterministic validator AND its
    paired examples through the extractor. A predicate that passes schema
    validation but fires on the compliant example is worse than none."""
    from storage.intent_features import extract_features, model_available
    from storage.triggers import evaluate_trigger, validate_trigger

    ok: list[str] = []
    bad: list[str] = []
    for spec in AUTHORED:
        errors = validate_trigger(spec["trigger"])
        if errors:
            bad.append(f"{spec['key']}: schema errors {errors}")
            continue
        if not model_available():
            bad.append(f"{spec['key']}: feature extractor unavailable, cannot "
                       f"verify the paired examples")
            continue
        hit = evaluate_trigger(spec["trigger"], extract_features(spec["violating_example"]))
        miss = evaluate_trigger(spec["trigger"], extract_features(spec["compliant_example"]))
        if hit is not True:
            bad.append(f"{spec['key']}: violating example does NOT match "
                       f"(got {hit!r}) — the predicate would never fire")
            continue
        if miss is not False:
            bad.append(f"{spec['key']}: compliant example MATCHES (got {miss!r}) "
                       f"— this is the v1 false-positive bug, refusing")
            continue
        ok.append(spec["key"])
    return ok, bad


async def apply(namespace: str, *, dry_run: bool) -> int:
    if namespace in PROTECTED_NAMESPACES:
        print(f"REFUSED: {namespace} holds validation evidence and must not be "
              f"written to. Its rows are left in place deliberately.",
              file=sys.stderr)
        return 2

    ok, bad = validate_all()
    for line in bad:
        print(f"REJECTED  {line}", file=sys.stderr)
    if bad:
        # Fail closed and fail loudly: a predicate that does not survive its own
        # paired fixtures must not reach a namespace.
        return 1

    if dry_run:
        for spec in AUTHORED:
            print(f"WOULD AUTHOR  {spec['key']}  ->  "
                  f"{json.dumps(spec['trigger'], separators=(',', ':'))}")
        return 0

    from psycopg_pool import AsyncConnectionPool

    from config import settings
    from storage.embeddings import build_embedder
    from storage.postgres import PostgresBackend

    pool = AsyncConnectionPool(settings.database_url, open=False, min_size=0, max_size=2)
    await pool.open()
    try:
        # DB-IDENTITY: name the database every write lands in. A correct
        # statement executed against the wrong database is the exact failure
        # that cost this project two hours and a silent non-arming.
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT current_database()")
            print(f"connected to database: {(await cur.fetchone())[0]}")

        backend = PostgresBackend(pool, embedder=build_embedder(settings))
        for spec in AUTHORED:
            res = await backend.skill_define(
                namespace, key=spec["key"], guidance=spec["guidance"],
                polarity=spec["polarity"], trigger=spec["trigger"],
                trigger_author="curator",
                temporal_mode="historical_snapshot",
                actor="gate-trigger-author")
            print(f"{spec['key']}: trigger_valid={res['trigger_valid']} "
                  f"revision={res.get('revision')} "
                  f"verified_persisted={res['verified_persisted']}")
            if not res["trigger_valid"]:
                return 1
    finally:
        await pool.close()
    return 0


def report() -> int:
    ok, bad = validate_all()
    print("AUTHORED TRIGGERS (validated, with paired violating/compliant fixtures):")
    for key in ok:
        print(f"  + {key}")
    for line in bad:
        print(f"  ! {line}")
    print("\nANTI-PATTERN SKILLS LEFT WITHOUT A TRIGGER (display-only):")
    for item in NEEDS_HUMAN_TRIGGER:
        print(f"  - {item['namespace']}/{item['key']}\n      {item['reason']}")
    print("\nUntil a trigger exists, these skills SURFACE as advice but cannot "
          "escalate. That is the intended fail-toward-silence direction.")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--namespace")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report or not args.namespace:
        return report()
    if not (args.apply or args.dry_run):
        print("choose --apply or --dry-run", file=sys.stderr)
        return 2
    return asyncio.run(apply(args.namespace, dry_run=not args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
