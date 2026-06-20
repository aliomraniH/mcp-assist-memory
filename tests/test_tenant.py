"""Tenant separation: namespace == project. No implicit cross-project reads.

Every per-project read filters on namespace, so data written under one project
namespace is invisible to another. (Under a single shared MCP_AUTH_TOKEN this is
a SOFT boundary — honest isolation, not enforced against a client that passes a
foreign namespace. Per-project tokens are the v2 fix; see REUSABILITY.md.)
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def two_projects():
    a = f"proj-test-{uuid.uuid4().hex[:8]}"
    b = f"proj-test-{uuid.uuid4().hex[:8]}"
    return a, b


async def test_memory_is_isolated_by_namespace(backend, two_projects):
    a, b = two_projects
    await backend.memory_save(a, "secret", {"v": "alpha-only"})
    # b cannot see a's key
    assert await backend.memory_get(b, "secret") is None
    assert await backend.memory_list(b) == []
    # same key in b is an independent revision-1 entry
    saved_b = await backend.memory_save(b, "secret", {"v": "beta-only"})
    assert saved_b["revision"] == 1
    assert "alpha-only" not in str(await backend.memory_get(b, "secret"))


async def test_search_does_not_cross_projects(backend, two_projects):
    a, b = two_projects
    await backend.memory_save(a, "k", {"note": "needle-xyz"})
    assert await backend.memory_search(b, "needle-xyz") == []
    assert any("needle-xyz" in str(h["value"]) for h in await backend.memory_search(a, "needle-xyz"))


async def test_handoff_is_isolated_by_namespace(backend, two_projects):
    a, b = two_projects
    await backend.handoff_save(a, "h", {"summary": "from-a"})
    assert await backend.handoff_load(b, "h") is None
    assert await backend.handoff_list(b) == []


async def test_session_reads_require_owning_namespace(backend, two_projects):
    a, b = two_projects
    s = await backend.session_create(a, surface="cli")
    sid = s["session_id"]
    await backend.session_append_event(a, sid, "start", {"n": 1})
    # b cannot read a's session or its events, and cannot append to it
    assert await backend.session_get(b, sid) is None
    assert await backend.session_events(b, sid) == []
    assert await backend.session_list(b) == []
    with pytest.raises(ValueError):
        await backend.session_append_event(b, sid, "evil", {"n": 99})
