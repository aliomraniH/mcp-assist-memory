"""v3 P0/1 item 7 — role recording: role ∈ author|observer|verifier|curator|
approver on every write. RECORDING ONLY this phase — validated and stored,
nothing gated on it.
"""
from __future__ import annotations

import pytest

from errors import AppError

REPO = "acme/widget"


async def test_role_recorded_and_returned_on_every_write_path(backend, ns):
    saved = await backend.memory_save(ns, "k", {"v": 1}, role="author")
    assert saved["role"] == "author"
    assert (await backend.memory_get(ns, "k"))["role"] == "author"

    handoff = await backend.handoff_save(ns, "baton", {"next": "x"}, role="observer")
    assert handoff["role"] == "observer"

    deleted = await backend.memory_delete(ns, "k", role="approver")
    assert deleted["role"] == "approver"


async def test_role_defaults_to_null_and_invalid_is_rejected(backend, ns):
    out = await backend.memory_save(ns, "k", {"v": 1})
    assert out["role"] is None
    with pytest.raises(AppError) as err:
        await backend.memory_save(ns, "k", {"v": 2}, role="dictator")
    assert err.value.code == "invalid_role"


async def test_recording_only_no_enforcement(backend, ns):
    """An 'observer' can still write/supersede anything — the field records
    capacity, it does not gate behavior in this phase."""
    await backend.memory_save(ns, "k", {"v": 1}, role="author")
    out = await backend.memory_save(ns, "k", {"v": 2}, role="observer")
    assert out["revision"] == 2 and out["verified_persisted"] is True


async def test_machine_writers_stamp_their_roles(reconcile_backend, curate_backend, ns):
    # Reconcile verdicts are written as verifier...
    b = reconcile_backend
    b.resolver.pulls[(REPO, 5)] = {"merged": False, "merge_sha": None}
    await b.memory_save(ns, "claim/x", {"c": 1}, kind="claim",
                        meta={"repo": REPO, "pr": 5})
    await b.coord_reconcile(ns)
    verdict = await b.memory_get(ns, "coord/_reconcile/claim/x")
    assert verdict["role"] == "verifier" and verdict["actor"] == "reconciler"

    # ...and curator ops as curator.
    import uuid
    ns2 = f"proj-test-{uuid.uuid4().hex[:12]}"
    s = await curate_backend.session_create(ns2)
    curate_backend.curator.result = {"operations": [
        {"op": "ADD", "key": "lesson/x", "kind": "note", "value": {"L": 1}}]}
    await curate_backend.coord_curate(ns2, s["session_id"])
    lesson = await curate_backend.memory_get(ns2, "lesson/x")
    assert lesson["role"] == "curator" and lesson["actor"] == "curator"
