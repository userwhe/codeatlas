"""Cursor pagination helpers (contracts/http-api.md, "Conventions").

Cursors are opaque to clients; internally they are offsets, which is enough at pilot scale.
"""

import base64
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

from codeatlas.api.errors import ApiError

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


class PageParams(BaseModel):
    offset: int
    limit: int


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode()


def page_params(
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> PageParams:
    if cursor is None:
        return PageParams(offset=0, limit=limit)
    try:
        prefix, value = base64.urlsafe_b64decode(cursor.encode()).decode().split(":", 1)
        offset = int(value)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid_cursor", "The cursor is invalid.") from exc
    if prefix != "o" or offset < 0:
        raise ApiError(422, "invalid_cursor", "The cursor is invalid.")
    return PageParams(offset=offset, limit=limit)


def next_cursor(params: PageParams, returned: int, has_more: bool) -> str | None:
    return encode_cursor(params.offset + returned) if has_more else None
