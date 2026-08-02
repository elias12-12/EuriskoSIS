"""Login and logout for the student portal.

Login is a student ID and nothing else -- the brief says that is sufficient
identity and requires no password infrastructure. What it buys is not secrecy
but a single, well-defined origin for the authenticated ID, which is what makes
the scoping rules in CLAUDE.md section 7 enforceable rather than aspirational.
See `app/auth.py` for the reasoning in full.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import auth
from app.db import get_session
from app.schemas import LoginRequest, LoginResponse, WhoAmI

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    session: Session = Depends(get_session),
) -> LoginResponse:
    """Exchange a student ID for a session token.

    Send the token back as `Authorization: Bearer <token>` on every `/me/*`
    request. In Swagger, paste it into the Authorize dialog once.
    """
    token, expires_at = auth.login(session, body.student_id)
    return LoginResponse(
        access_token=token, expires_at=expires_at, student_id=body.student_id
    )


@router.post("/logout", status_code=204)
def logout(
    authorization: str = Depends(auth.current_student_token),
    session: Session = Depends(get_session),
) -> None:
    """End the current session. Idempotent."""
    auth.logout(session, authorization)


@router.get("/me", response_model=WhoAmI)
def whoami(student_id: str = Depends(auth.current_student)) -> WhoAmI:
    """Who the current token authenticates as.

    Exists mostly for the Phase 4 exit check and the Phase 6 UI: it is the
    cheapest way to see that two sessions really are two different students.
    """
    return WhoAmI(student_id=student_id)
