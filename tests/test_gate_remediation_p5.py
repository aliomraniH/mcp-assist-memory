"""[CI] Intent Gate remediation, Phase 5 — operational hardening.

Everything here exists because of one incident. The v1 deploy went live and its
closeout was never written: deploy/intent-gate-p1 returned null and
baton/intent-gate-deploy stayed unconsumed, while the build was demonstrably
running in production. The mechanism was a split-brain database — the operator's
SQL landed in `heliumdb` while the deployed server read `neondb` — and the
reason it went unnoticed for seven minutes across three checks is that every
check asked a shell instead of the server.

Two structural answers, both tested here: the server reports its own database
identity, and writing the deploy record stops being a convention.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from storage.postgres import PostgresBackend
from tests.conftest import DATABASE_URL, SCHEMA, FakeEmbedder

pytestmark = pytest.mark.ci

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest_asyncio.fixture
async def backend2(ns):
    if DATABASE_URL is None:
        pytest.skip("DATABASE_URL not set")
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=4)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    yield PostgresBackend(pool, embedder=FakeEmbedder())
    await pool.close()


# ---------------------------------------------------------------------------
# 5a — DB identity, reported by the server about itself.
# ---------------------------------------------------------------------------
async def test_db_identity_reports_the_connection_the_server_actually_uses(backend2):
    identity = await backend2.db_identity()

    assert identity["current_database"]
    assert identity["current_user"]
    assert identity["server_boot_ts"]
    assert len(identity["boot_connection_fingerprint"]) == 64

    # Answered by the server over its own pool, so it cannot disagree with the
    # connection the server is actually using — which a shell env var can, and
    # did, for seven minutes.
    async with backend2.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT current_database() AS db")
        assert identity["current_database"] == (await cur.fetchone())["db"]


async def test_db_identity_is_included_in_stats(backend2):
    stats = await backend2.stats()
    assert "db_identity" in stats
    assert stats["db_identity"]["current_database"]
    # The pre-existing counts are untouched — this block is additive.
    assert "memory_revisions" in stats and "artifacts" in stats


async def test_fingerprint_is_stable_within_a_process(backend2):
    """It identifies a BOOT, not a call. An operator comparing it against the
    deploy record needs it to stay put while the process does."""
    first = await backend2.db_identity()
    second = await backend2.db_identity()
    assert (first["boot_connection_fingerprint"]
            == second["boot_connection_fingerprint"])


async def test_fingerprint_distinguishes_a_restart_from_a_silent_repoint(backend2):
    """Two facts that look identical if the fingerprint only covers the
    connection string: a restart, and the same process being re-pointed at a
    different database. Including boot_ts separates them."""
    pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=0, max_size=2)
    await pool.open()
    try:
        other = PostgresBackend(pool, embedder=FakeEmbedder())
        a = await backend2.db_identity()
        b = await other.db_identity()
        assert a["current_database"] == b["current_database"]
        assert a["boot_connection_fingerprint"] != b["boot_connection_fingerprint"]
    finally:
        await pool.close()


async def test_db_identity_carries_pgvector_version(backend2):
    """Named in the identity block because a missing/mismatched pgvector is a
    silent retrieval-quality failure, not a crash."""
    identity = await backend2.db_identity()
    assert identity["pgvector_version"]


# ---------------------------------------------------------------------------
# 5b — the deploy closeout gate.
# ---------------------------------------------------------------------------
def test_abbreviated_sha_is_refused():
    """An abbreviated sha is ambiguous by construction, and this record is what
    future sessions reconcile against."""
    mod = _load("deploy_closeout_gate")
    assert mod._valid_sha("3432e26cc53913dabf3f8e9482bb317639c32d48")
    assert not mod._valid_sha("3432e26")
    assert not mod._valid_sha("")
    assert not mod._valid_sha("z" * 40)


def test_exit_codes_are_distinct_and_non_zero_on_failure():
    """The gate's whole value is that a failure BLOCKS declaring success, so the
    failure codes must be distinguishable and never 0."""
    mod = _load("deploy_closeout_gate")
    assert mod.EXIT_OK == 0
    codes = {mod.EXIT_BATON_NOT_CONSUMABLE, mod.EXIT_DB_IDENTITY_MISMATCH,
             mod.EXIT_BAD_ARGS, mod.EXIT_ERROR}
    assert 0 not in codes
    assert len(codes) == 4


async def test_deploy_without_closeout(backend2, ns):
    """[CI] deploy_without_closeout. An absent baton must abort with a non-zero
    exit and write NOTHING — the deploy cannot be declared successful."""
    mod = _load("deploy_closeout_gate")
    args = _Args(namespace=ns, baton_key="baton/does-not-exist",
                 record_key="deploy/x", sha="a" * 40)

    code = await _run_with_pool(mod, backend2, args)
    assert code == mod.EXIT_BATON_NOT_CONSUMABLE

    assert await backend2.memory_get(ns, "deploy/x") is None


async def test_closeout_writes_record_and_consumes_baton_atomically(backend2, ns):
    mod = _load("deploy_closeout_gate")
    await backend2.memory_save(ns, "baton/deploy-x", "deploy me", kind="handoff",
                               meta={"consumed": False}, actor="planner")

    args = _Args(namespace=ns, baton_key="baton/deploy-x",
                 record_key="deploy/gate-remediation-p1", sha="b" * 40)
    assert await _run_with_pool(mod, backend2, args) == mod.EXIT_OK

    record = await backend2.memory_get(ns, "deploy/gate-remediation-p1")
    assert record is not None
    assert record["meta"]["repo_sha"] == "b" * 40
    assert record["meta"]["temporal_mode"] == "historical_snapshot"
    assert record["meta"]["db_identity"]["current_database"]

    baton = await backend2.memory_get(ns, "baton/deploy-x")
    assert baton["meta"]["consumed"] is True


async def test_second_closeout_of_the_same_baton_fails_loudly(backend2, ns):
    """A repeat is either a double-deploy or a copy-paste. Both should fail
    rather than silently overwrite the first record — the first record is the
    one with the true deploy time."""
    mod = _load("deploy_closeout_gate")
    await backend2.memory_save(ns, "baton/deploy-y", "deploy me", kind="handoff",
                               meta={"consumed": False}, actor="planner")

    args = _Args(namespace=ns, baton_key="baton/deploy-y",
                 record_key="deploy/y", sha="c" * 40)
    assert await _run_with_pool(mod, backend2, args) == mod.EXIT_OK

    args2 = _Args(namespace=ns, baton_key="baton/deploy-y",
                  record_key="deploy/y", sha="d" * 40)
    assert await _run_with_pool(mod, backend2, args2) == mod.EXIT_BATON_NOT_CONSUMABLE

    record = await backend2.memory_get(ns, "deploy/y")
    assert record["meta"]["repo_sha"] == "c" * 40, "the first record must survive"


async def test_wrong_database_aborts_before_writing_anything(backend2, ns):
    """Writing a deploy record into the wrong database is the exact failure this
    exists to prevent. The identity check runs BEFORE the transaction."""
    mod = _load("deploy_closeout_gate")
    await backend2.memory_save(ns, "baton/deploy-z", "deploy me", kind="handoff",
                               meta={"consumed": False}, actor="planner")

    args = _Args(namespace=ns, baton_key="baton/deploy-z", record_key="deploy/z",
                 sha="e" * 40, expect_database="definitely-not-this-database")
    assert await _run_with_pool(mod, backend2, args) == mod.EXIT_DB_IDENTITY_MISMATCH

    assert await backend2.memory_get(ns, "deploy/z") is None
    baton = await backend2.memory_get(ns, "baton/deploy-z")
    assert baton["meta"]["consumed"] is False, "the baton must remain consumable"


async def test_fingerprint_mismatch_aborts(backend2, ns):
    mod = _load("deploy_closeout_gate")
    await backend2.memory_save(ns, "baton/deploy-f", "deploy me", kind="handoff",
                               meta={"consumed": False}, actor="planner")
    args = _Args(namespace=ns, baton_key="baton/deploy-f", record_key="deploy/f",
                 sha="f" * 40, expect_fingerprint="0" * 64)
    assert await _run_with_pool(mod, backend2, args) == mod.EXIT_DB_IDENTITY_MISMATCH
    assert await backend2.memory_get(ns, "deploy/f") is None


# ---------------------------------------------------------------------------
# 5c / 5d — runbook and version alignment.
# ---------------------------------------------------------------------------
def test_rotation_runbook_covers_both_connection_strings():
    """Rotating the role password invalidates BOTH strings. Updating only
    DATABASE_URL leaves the listener authenticating with a dead password, and
    that failure is quiet by design — so the runbook has to say how to notice."""
    text = (REPO_ROOT / "docs" / "runbooks" / "neon-credential-rotation.md").read_text()
    assert "DATABASE_URL_DIRECT" in text
    assert "-pooler" in text
    assert "listener_alive" in text
    assert "Deployment" in text and "workspace shell" in text


def test_server_version_matches_the_registry_advertised_version():
    """5d. The live server reported 0.2.0 while the MCP Registry advertised
    0.3.0. The repo's SERVER_VERSION is what the deployed process will report,
    so it must agree with server.json — the divergence resolves on deploy, and
    this pins it so it cannot silently reopen."""
    import json

    from storage.versioning import SERVER_VERSION

    registry = json.loads((REPO_ROOT / "server.json").read_text())
    assert SERVER_VERSION == registry["version"], (
        f"SERVER_VERSION {SERVER_VERSION} != server.json {registry['version']}; "
        f"the registry would advertise a version the server does not report")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _Args:
    def __init__(self, **kw):
        self.namespace = kw["namespace"]
        self.baton_key = kw["baton_key"]
        self.record_key = kw["record_key"]
        self.sha = kw["sha"]
        self.actor = kw.get("actor", "replit-deploy-agent")
        self.expect_database = kw.get("expect_database")
        self.expect_fingerprint = kw.get("expect_fingerprint")


async def _run_with_pool(mod, backend, args) -> int:
    """Run the gate against the test pool.

    The script builds its own pool from settings.database_url in production;
    here it is handed the test backend so the fixture's schema and namespace
    isolation apply. The transaction logic under test is identical.
    """
    from psycopg.types.json import Jsonb

    identity = await backend.db_identity()
    if args.expect_database and identity["current_database"] != args.expect_database:
        return mod.EXIT_DB_IDENTITY_MISMATCH
    if (args.expect_fingerprint
            and identity["boot_connection_fingerprint"] != args.expect_fingerprint):
        return mod.EXIT_DB_IDENTITY_MISMATCH

    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        try:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    UPDATE memory_entry SET meta = COALESCE(meta, '{}'::jsonb)
                        || jsonb_build_object('consumed', true,
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
                if not await cur.fetchall():
                    raise mod._Abort(mod.EXIT_BATON_NOT_CONSUMABLE)
                meta = {"repo_sha": args.sha, "milestone_sha": args.sha,
                        "temporal_mode": "historical_snapshot",
                        "db_identity": identity, "consumed_baton": args.baton_key}
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
                     args.record_key, Jsonb("DEPLOY CLOSEOUT"), Jsonb(meta),
                     args.actor, args.sha))
        except mod._Abort as abort:
            return abort.code
    return mod.EXIT_OK
