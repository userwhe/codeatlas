"""String helpers."""

import re

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase `value` and join its words with dashes."""
    return _NON_WORD.sub("-", value.lower()).strip("-")


def truncate(value: str, limit: int) -> str:
    """Shorten `value` to at most `limit` characters, ending with an ellipsis."""
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
