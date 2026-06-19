"""/healthz: 200 ok when the DB probe succeeds, 503 degraded when it fails."""

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from assist_memory.app import _healthz, create_pool


def _health_app() -> Starlette:
    return Starlette(routes=[Route("/healthz", _healthz, methods=["GET"])])


def test_healthz_degraded_without_pool():
    app = _health_app()
    app.state.pool = None
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "db": "error"}


def test_healthz_degraded_when_select_fails():
    # A pool pointed at an unreachable DB: open() returns immediately (min_size=0),
    # but the SELECT 1 probe fails -> 503.
    app = _health_app()
    bad = create_pool("postgresql://127.0.0.1:1/nope?connect_timeout=1")
    app.state.pool = bad
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["db"] == "error"


def test_healthz_ok_when_db_reachable(pg_pool):
    # Sync test (TestClient runs its own loop) consuming the live pool fixture.
    app = _health_app()
    app.state.pool = pg_pool
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}
