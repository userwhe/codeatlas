"""Routes: GitHub sign-in and sign-out (contracts/http-api.md, "Authentication")."""

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from codeatlas.api.deps import DbSession, RequestId
from codeatlas.auth.crypto import DecryptionError
from codeatlas.auth.github_login import (
    STATE_COOKIE,
    STATE_TTL,
    NotInvited,
    SignInError,
    complete_login,
    start_login,
)
from codeatlas.auth.sessions import (
    COOKIE_NAME,
    clear_session_cookie,
    get_valid_session,
    revoke_session,
    set_session_cookie,
)
from codeatlas.github.gateway import GitHubError, get_gateway
from codeatlas.workspace.audit import record

logger = logging.getLogger(__name__)
router = APIRouter()

STATE_COOKIE_PATH = "/auth/github"


@router.get("/auth/github/login", include_in_schema=False)
def login() -> RedirectResponse:
    start = start_login(get_gateway())
    response = RedirectResponse(start.redirect_url, status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        start.state_cookie,
        max_age=int(STATE_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path=STATE_COOKIE_PATH,
    )
    return response


@router.get("/auth/github/callback", include_in_schema=False)
def callback(
    request: Request, db: DbSession, request_id: RequestId, code: str = "", state: str = ""
) -> RedirectResponse:
    try:
        token = complete_login(
            db,
            get_gateway(),
            code=code,
            state=state,
            state_cookie=request.cookies.get(STATE_COOKIE),
            request_id=request_id,
        )
        db.commit()
    except NotInvited as exc:
        # Nothing was written for this user; only the denial is recorded (FR-003).
        db.rollback()
        logger.info("sign-in refused: not on the access list")
        record(
            db,
            action="sign_in",
            outcome="denied",
            request_id=request_id,
            detail={"reason": "not_invited", "github_login": exc.github_login},
        )
        db.commit()
        refused = RedirectResponse("/?error=not_invited", status_code=302)
        refused.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
        return refused
    except (SignInError, DecryptionError, GitHubError) as exc:
        db.rollback()
        logger.warning("sign-in failed: %s", type(exc).__name__)
        record(
            db,
            action="sign_in",
            outcome="failure",
            request_id=request_id,
            detail={"reason": type(exc).__name__},
        )
        db.commit()
        failed = RedirectResponse("/?error=sign_in_failed", status_code=302)
        failed.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
        return failed

    response = RedirectResponse("/repositories", status_code=302)
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    set_session_cookie(response, token)
    return response


@router.post("/auth/logout", status_code=204, include_in_schema=False)
def logout(request: Request, db: DbSession, request_id: RequestId) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session = get_valid_session(db, token)
        revoke_session(db, token)
        if session is not None:
            record(
                db,
                action="sign_out",
                outcome="success",
                actor_user_id=session.user_id,
                request_id=request_id,
            )
        db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response
