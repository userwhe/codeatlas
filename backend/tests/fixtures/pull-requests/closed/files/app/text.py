"""Text helpers."""

import re

_SEPARATORS = re.compile(r"[^a-z0-9_]+")


def slugify(value: str) -> str:
    """Lowercase the value, join its words with `-`, and trim `-` at both ends."""
    return _SEPARATORS.sub("-", value.lower()).strip("-")
