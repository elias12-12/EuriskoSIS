"""Student authentication, and the single place an authenticated ID comes from.

The brief allows a student ID as sufficient identity with no password
infrastructure. That makes login trivial, and makes it easy to miss the part
that is not trivial: CLAUDE.md section 7 rule 2 requires every personal data
access to be scoped by the *authenticated* ID, enforced in the data layer, never
supplied by the client and never by the model.

So logging in is cheap but it is still a login. `POST /auth/login` exchanges a
student ID for an opaque session token; every `/me/*` request presents that
token; `current_student` turns it back into an ID by looking it up in the
database. No endpoint under `/me` takes a student ID in its path, query or body,
and no agent tool accepts one as an argument. The guarantee is structural: there
is no code path that could read one from a request, so no reviewer has to check
whether some handler forgot.

Why a token rather than trusting an `X-Student-Id` header: with ID-only login
both are equally easy to forge, so this buys no secrecy. What it buys is that
the identity has exactly one shape and one origin, which is what makes "the
model can never supply the student ID" a fact about the code rather than a
promise about prompts.

Tokens are stored hashed. A database dump should not hand over live sessions,
and SHA-256 of a 256-bit random token needs no salt or work factor -- there is
nothing to brute-force.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import Student, StudentSession

# Sent as `Authorization: Bearer <token>`.
_BEARER_PREFIX = "bearer "


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def login(session: Session, student_id: str) -> tuple[str, datetime]:
    """Exchange a student ID for a session token. Raises 404 for an unknown ID.

    404 rather than 401: there is no secret to be wrong about, and "no such
    student" is the honest and more useful answer. Nothing is being protected by
    vagueness here -- pretending otherwise would be security theatre.
    """
    exists = session.scalar(
        select(Student.student_id).where(Student.student_id == student_id)
    )
    if exists is None:
        raise HTTPException(status_code=404, detail=f"No student {student_id}")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=get_settings().session_ttl_hours
    )
    session.add(
        StudentSession(
            token_hash=_hash(token),
            student_id=student_id,
            expires_at=expires_at,
        )
    )
    session.commit()
    return token, expires_at


def logout(session: Session, token: str) -> None:
    """Drop a session. Idempotent -- logging out twice is not an error."""
    session.execute(
        delete(StudentSession).where(StudentSession.token_hash == _hash(token))
    )
    session.commit()


def resolve(session: Session, token: str) -> str | None:
    """Token -> student ID, or None if unknown or expired.

    Expired sessions are deleted on sight rather than left to a sweep job. The
    corpus of sessions here is tiny, and a row that exists but must never be
    honoured is exactly the kind of thing a later query forgets to filter on.
    """
    record = session.get(StudentSession, _hash(token))
    if record is None:
        return None

    now = datetime.now(timezone.utc)
    if record.expires_at <= now:
        session.delete(record)
        session.commit()
        return None

    record.last_seen_at = now
    session.commit()
    return record.student_id


def current_student_token(
    authorization: str | None = Header(
        default=None,
        description="`Bearer <token>` from POST /auth/login.",
    ),
) -> str:
    """The raw bearer token, for logout. Does not prove the session is valid."""
    if not authorization or not authorization.lower().startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token. POST /auth/login with your student ID first.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization[len(_BEARER_PREFIX) :].strip()


def current_student(
    token: str = Depends(current_student_token),
    session: Session = Depends(get_session),
) -> str:
    """FastAPI dependency: the authenticated student ID, or 401.

    **This is the only function in the application that produces a student ID
    for a student-facing request.** Everything under `/me` and every scoped agent
    tool traces back to it. If a second source ever appears, the scoping
    guarantee stops being checkable by reading one file.
    """
    student_id = resolve(session, token)
    if student_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is invalid or has expired. Log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return student_id
