"""Route: the alert conditions that an external monitor checks (contracts/http-api.md).

The monitor alerts on a 503 and sends a recovery notice on the next 200 (specs/004-pilot-deployment,
research R10). No session is needed: the bodies hold only the condition's numbers, and the paths
are rate-limited per client address.
"""

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from codeatlas.api.deps import RequestId
from codeatlas.api.errors import ApiError, error_body, not_found
from codeatlas.db import new_session
from codeatlas.status import CONDITIONS, read_condition

router = APIRouter()

# Every answer reflects the database at the time of the check.
NO_STORE = {"Cache-Control": "no-store"}


def _error_response(error: ApiError, request_id: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content=error_body(
            code=error.code,
            message=error.message,
            retryable=error.retryable,
            request_id=request_id,
        ),
        headers=NO_STORE,
    )


@router.get("/status/{condition}", include_in_schema=False)
def read_status(condition: str, request_id: RequestId) -> JSONResponse:
    if condition not in CONDITIONS:
        return _error_response(not_found(), request_id)
    # The session is opened here, not by a dependency, so an unreachable database is a 503 that
    # the monitor alerts on too.
    try:
        with new_session() as db:
            result = read_condition(db, condition, datetime.now(UTC))
    except SQLAlchemyError:
        unavailable = ApiError(503, "not_ready", "The database is unavailable.", retryable=True)
        return _error_response(unavailable, request_id)
    return JSONResponse(
        status_code=503 if result.failing else 200,
        content={
            "condition": result.name,
            "status": "failing" if result.failing else "ok",
            "value": result.value,
            "threshold": result.threshold,
        },
        headers=NO_STORE,
    )
