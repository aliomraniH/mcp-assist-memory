from evlog.replay import rebuild


def test_rebuild_returns_projection():
    projection = rebuild()
    assert isinstance(projection, dict)
    assert projection["b"] == 10
