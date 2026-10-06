"""Repository access checks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Membership:
    user_id: int
    repository_id: int
    role: str


class AccessPolicy:
    """Decides which membership roles may read a repository."""

    def __init__(self, readable_roles: set[str]) -> None:
        self.readable_roles = readable_roles

    def can_read(self, membership: Membership) -> bool:
        return membership.role in self.readable_roles


DEFAULT_POLICY = AccessPolicy({"owner", "member"})


def check_repository_access(
    user_id: int, repository_id: int, memberships: list[Membership]
) -> bool:
    """Return True when the user holds a readable membership in the repository."""
    for membership in memberships:
        if membership.user_id == user_id and membership.repository_id == repository_id:
            return DEFAULT_POLICY.can_read(membership)
    return False
