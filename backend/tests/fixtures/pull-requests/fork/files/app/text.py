"""Text helpers."""

import re

_SEPARATORS = re.compile(r"[^a-z0-9]+")
MAX_SLUG_LENGTH = 40


def slugify(value: str) -> str:
    """Lowercase the value, join its words with `-`, and trim `-` at both ends."""
    slug = _SEPARATORS.sub("-", value.lower()).strip("-")
    return slug[:MAX_SLUG_LENGTH].rstrip("-")
