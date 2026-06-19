"""Idempotent save: the same event_id never appends a second revision."""

from uuid import uuid4


async def test_same_event_id_yields_one_revision(pg_backend):
    eid = str(uuid4())

    first = await pg_backend.save_revision(
        "default", "k", "v1", False, "note", [], "cli", event_id=eid
    )
    second = await pg_backend.save_revision(
        "default", "k", "v2", False, "note", [], "cli", event_id=eid
    )

    assert first.revision == 1
    assert second.revision == 1          # deduped: returns the existing revision
    assert second.value == "v1"          # original value, not the replay's "v2"

    history = await pg_backend.get_history("default", "k")
    assert len(history) == 1


async def test_distinct_event_ids_append(pg_backend):
    r1 = await pg_backend.save_revision(
        "default", "k", "v1", False, "note", [], "cli", event_id=str(uuid4())
    )
    r2 = await pg_backend.save_revision(
        "default", "k", "v2", False, "note", [], "cli", event_id=str(uuid4())
    )
    assert (r1.revision, r2.revision) == (1, 2)


async def test_no_event_id_always_appends(pg_backend):
    r1 = await pg_backend.save_revision("default", "k", "v1", False, "note", [], "cli")
    r2 = await pg_backend.save_revision("default", "k", "v2", False, "note", [], "cli")
    assert (r1.revision, r2.revision) == (1, 2)
