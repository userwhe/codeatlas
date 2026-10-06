"""API error type and the JSON error body from contracts/http-api.md."""

from typing import Any


class ApiError(Exception):
    """An error returned as `{"error": {code, message, retryable, request_id, details}}`."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def error_body(
    *,
    code: str,
    message: str,
    retryable: bool,
    request_id: str | None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
            "details": details or {},
        }
    }


def not_found() -> ApiError:
    """The single response for missing and inaccessible resources (FR-004)."""
    return ApiError(404, "not_found", "The requested resource does not exist.")


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "Sign in to continue.")
