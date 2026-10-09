from app.auth.permissions import User, can_write


def test_an_owner_may_write() -> None:
    owner = User(id=1, role="owner", repository_ids=frozenset({7}))
    assert can_write(owner, 7)


def test_a_viewer_may_not_write() -> None:
    viewer = User(id=2, role="viewer", repository_ids=frozenset({7}))
    assert not can_write(viewer, 7)


def test_an_owner_of_another_repository_may_not_write() -> None:
    owner = User(id=3, role="owner", repository_ids=frozenset({8}))
    assert not can_write(owner, 7)
