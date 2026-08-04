#!/usr/bin/env python3
"""Deploy closeout GATE — the final mandatory step of a deploy.

WHY THIS IS A SCRIPT AND NOT A CHECKLIST ITEM
    The Intent Gate v1 deploy went live and its closeout was never written:
    deploy/intent-gate-p1 returned null and baton/intent-gate-deploy stayed
    unconsumed, while the build was demonstrably running in production. The
    likely mechanism was the split-brain database — the operator's SQL landed
    somewhere the server never reads — but the deeper problem is that
    "write the deploy record" was a CONVENTION. Conventions are what get skipped
    when a deploy runs long, and a skipped closeout leaves a stale artifact that
    misleads every future reader. Verdict decay is the default end-state of any
    namespace not actively closed out.

    So it becomes a gate. Non-zero exit means the deploy may NOT be declared
    successful.

WHAT IT DOES, ATOMICALLY
    ONE transaction writes the deploy_record row AND marks the deploy baton
    consumed:

        UPDATE ... SET consumed = true WHERE consumed = false RETURNING 1

    Zero rows returned means the baton was already consumed (or never existed),
    and the whole transaction aborts. That is deliberate: a second closeout of
    the same baton is either a double-deploy or a copy-paste, and both deserve
    to fail loudly rather than silently overwrite the first record.

    Because it is one transaction, there is no window in which the record exists
    without the baton being consumed, or vice versa.

DB IDENTITY IS CHECKED FIRST
    Before writing anything the script asserts the connection's identity against
    --expect-database / --expect-fingerprint. Writing a deploy record into the
    wrong database is precisely the failure this whole exercise exists to
    prevent, and it would be especially poetic here.

USAGE (operator, at deploy time)
    python scripts/deploy_closeout_gate.py \
        --namespace dev/mcp-assist-memory \
        --baton-key baton/replit-deploy-gate-remediation \
        --record-key deploy/gate-remediation-p1 \
        --sha <40-char> \
        --expect-database neondb

    Exit 0 = closed out. Non-zero = DO NOT declare the deploy successful.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

EXIT_OK = 0
EXIT_BATON_NOT_CONSUMABLE = 3
EXIT_DB_IDENTITY_MISMATCH = 4
EXIT_BAD_ARGS = 2
EXIT_ERROR = 1


def _valid_sha(sha: str) -> bool:
    """40 hex chars. An abbreviated sha is not a deploy identifier: it is
    ambiguous by construction and this record is meant to be the thing future
    sessions reconcile against."""
    return isinstance(sha, str) and len(sha) == 40 and all(
        c in "0123456789abcdef" for c in sha.lower())


async def run(args) -> int:
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg_pool import AsyncConnectionPool

    from config import settings
    from storage.postgres import PostgresBackend

    pool = AsyncConnectionPool(settings.database_url, open=False, min_size=0, max_size=2)
    await pool.open()
    try:
        backend = PostgresBackend(pool)

        # ---- 1. DB identity, before any write. -----------------------------
        identity = await backend.db_identity()
        print(json.dumps({"db_identity": identity}, indent=2))

        if args.expect_database and identity["current_database"] != args.expect_database:
            print(f"FAIL: connected to {identity['current_database']!r} but the "
                  f"deploy expects {args.expect_database!r}. This is the "
                  f"split-brain failure mode — refusing to write a deploy "
                  f"record into the wrong database.", file=sys.stderr)
            return EXIT_DB_IDENTITY_MISMATCH
        if (args.expect_fingerprint
                and identity["boot_connection_fingerprint"] != args.expect_fingerprint):
            print(f"FAIL: boot_connection_fingerprint "
                  f"{identity['boot_connection_fingerprint']} does not match the "
                  f"expected {args.expect_fingerprint}. Something moved "
                  f"underneath the server.", file=sys.stderr)
            return EXIT_DB_IDENTITY_MISMATCH

        # ---- 2. Record + baton consumption, in ONE transaction. ------------
        async with pool.connection() as conn:
            conn.row_factory = dict_row
            async with conn.transaction():
                # Consume FIRST so the cheap check aborts before the write.
                cur = await conn.execute(
                    """
                    UPDATE memory_entry SET meta = COALESCE(meta, '{}'::jsonb)
                        || jsonb_build_object(
                            'consumed', true,
                            'consumed_at', now()::text,
                            'consumed_by', %s::text)
                    WHERE id = (
                        SELECT id FROM memory_entry
                        WHERE namespace = %s AND key = %s AND NOT tombstone
                        ORDER BY revision DESC LIMIT 1)
                      AND COALESCE((meta ->> 'consumed')::boolean, false) = false
                    RETURNING id
                    """,
                    (args.actor, args.namespace, args.baton_key))
                consumed = await cur.fetchall()
                if not consumed:
                    print(
                        f"FAIL: {args.baton_key} is absent or already consumed — "
                        f"0 rows updated. A second closeout of the same baton is "
                        f"either a double-deploy or a copy-paste; both fail loudly "
                        f"rather than overwrite the first record.", file=sys.stderr)
                    raise _Abort(EXIT_BATON_NOT_CONSUMABLE)

                value = (f"DEPLOY CLOSEOUT. sha {args.sha} deployed to "
                         f"{identity['endpoint_host'] or 'unknown-host'} / "
                         f"database {identity['current_database']}. "
                         f"Written by scripts/deploy_closeout_gate.py in the same "
                         f"transaction that consumed {args.baton_key}.")
                meta = {
                    "repo_sha": args.sha,
                    "milestone_sha": args.sha,
                    "temporal_mode": "historical_snapshot",
                    "db_identity": identity,
                    "consumed_baton": args.baton_key,
                }
                await conn.execute(
                    """
                    INSERT INTO memory_entry
                        (namespace, key, revision, kind, value, meta, actor,
                         origin, repo_sha)
                    VALUES (%s, %s,
                            COALESCE((SELECT max(revision) + 1 FROM memory_entry
                                      WHERE namespace = %s AND key = %s), 1),
                            'knowledge', %s, %s, %s, 'tool', %s)
                    """,
                    (args.namespace, args.record_key, args.namespace,
                     args.record_key, Jsonb(value), Jsonb(meta), args.actor,
                     args.sha))

        print(f"OK: {args.record_key} written and {args.baton_key} consumed "
              f"atomically in {identity['current_database']}.")
        return EXIT_OK
    except _Abort as abort:
        return abort.code
    finally:
        await pool.close()


class _Abort(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"abort {code}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--baton-key", required=True)
    ap.add_argument("--record-key", required=True)
    ap.add_argument("--sha", required=True, help="full 40-char commit sha")
    ap.add_argument("--actor", default="replit-deploy-agent")
    ap.add_argument("--expect-database",
                    help="abort unless current_database() matches (e.g. neondb)")
    ap.add_argument("--expect-fingerprint",
                    help="abort unless boot_connection_fingerprint matches")
    args = ap.parse_args()

    if not _valid_sha(args.sha):
        print(f"FAIL: --sha must be the full 40-char commit sha, got {args.sha!r}. "
              f"An abbreviated sha is ambiguous and this record is what future "
              f"sessions reconcile against.", file=sys.stderr)
        return EXIT_BAD_ARGS

    try:
        return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 - operator-facing, must not traceback-spam
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
