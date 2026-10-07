"""Repository operations."""

from app.auth.permissions import User, can_write


def rename_repository(user: User, repository_id: int, name: str) -> str:
    """Return the new name of the repository, after checking that the user may change it."""
    if not can_write(user, repository_id):
        raise PermissionError(f"user {user.id} may not change repository {repository_id}")
    return name.strip()
