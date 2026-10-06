"""Entry point of the sample application."""

from app.auth.access import Membership, check_repository_access

MEMBERSHIPS = [
    Membership(user_id=1, repository_id=7, role="owner"),
    Membership(user_id=2, repository_id=7, role="guest"),
]


def describe_access(user_id: int, repository_id: int) -> str:
    """Return a sentence saying whether the user may read the repository."""
    allowed = check_repository_access(user_id, repository_id, MEMBERSHIPS)
    verb = "can" if allowed else "cannot"
    return f"user {user_id} {verb} read repository {repository_id}"


if __name__ == "__main__":
    print(describe_access(1, 7))
