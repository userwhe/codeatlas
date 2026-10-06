"""FastAPI application: error handling, request IDs, Origin check, and routers."""

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from codeatlas.api.errors import ApiError, error_body
from codeatlas.api.routes import (
    analysis_runs,
    auth,
    github,
    jobs,
    me,
    repositories,
    search,
    snapshots,
    usage,
    webhooks,
)
from codeatlas.config import get_settings
from codeatlas.logging import configure_logging, request_id_var

logger = logging.getLogger(__name__)

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# GitHub calls these paths, and their signature check replaces the Origin check
# (specs/002-push-reindexing, research R2).
WEBHOOK_PATH_PREFIX = "/webhooks/"


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _error_response(
    request: Request, status: int, code: str, message: str, *, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_body(
            code=code, message=message, retryable=retryable, request_id=_request_id(request)
        ),
    )


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="CodeAtlas API", version="0.1.0")

    @app.middleware("http")
    async def origin_check(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # CSRF protection for state-changing requests (research R6).
        exempt = request.url.path.startswith(WEBHOOK_PATH_PREFIX)
        if request.method in STATE_CHANGING_METHODS and not exempt:
            if request.headers.get("origin") != get_settings().app_origin:
                return _error_response(
                    request, 403, "origin_mismatch", "The request origin is not allowed."
                )
        return await call_next(request)

    @app.middleware("http")
    async def request_ids(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=error_body(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                request_id=_request_id(request),
                details=exc.details,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        return JSONResponse(
            status_code=422,
            content=error_body(
                code="invalid_request",
                message="The request is invalid.",
                retryable=False,
                request_id=_request_id(request),
                details={"fields": fields},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return _error_response(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return _error_response(
            request, 500, "internal_error", "Something went wrong.", retryable=True
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(webhooks.router)
    for module in (me, usage, jobs, github, repositories, snapshots, search, analysis_runs):
        app.include_router(module.router, prefix="/v1")
    return app


app = create_app()
