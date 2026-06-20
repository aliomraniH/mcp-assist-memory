"""health() backs /healthz. (The HTTP 200/503 mapping lives in app.py.)"""
from __future__ import annotations


async def test_health_true_when_db_up(backend):
    assert await backend.health() is True
