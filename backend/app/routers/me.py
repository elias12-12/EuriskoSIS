"""The student-facing surface. **This is what a student's browser calls.**

CLAUDE.md carries this forward from Phase 2 as a correctness issue, not a
nice-to-have: `/students/{id}/*` takes the ID as a path parameter, which is right
for the admin panel's browsers and is exactly what section 7 rule 2 forbids for a
student reading their own record. These routes take the ID from the authenticated
session instead, and there is no path, query or body parameter through which a
client could name a different student.

Every handler delegates to the same `records` / `academics` / `eligibility`
functions the Phase 2 endpoints use, which filter on `student_id` in SQL. That
was the point of building them to take the ID as an argument: this router is a
routing change, not a second implementation, so the two surfaces cannot drift
into disagreeing about a student's record.
"""

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app import chat as chat_service
from app import conversations
from app.academics import get_academic_summary, get_degree_progress
from app.auth import current_student
from app.config import get_settings
from app.db import get_session
from app.eligibility import check_eligibility
from app.records import get_course_history, get_profile, get_schedule
from app.schemas import (
    ChatRequest,
    ChatResponse,
    CourseHistory,
    DegreeProgress,
    Eligibility,
    Profile,
    Schedule,
    Transcript,
    TranscriptMessage,
)

router = APIRouter(prefix="/me", tags=["me (authenticated student)"])


@router.get("/profile", response_model=Profile)
def my_profile(
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
) -> Profile:
    profile = get_profile(session, student_id)
    if profile is None:
        # The session authenticated, so the student existed at login. Reaching
        # here means the record was deleted mid-session -- a 404 on /me is the
        # honest answer, and the CASCADE on student_sessions makes it rare.
        raise HTTPException(status_code=404, detail="Your record was not found")
    return Profile(**profile, academics=get_academic_summary(session, student_id))


@router.get("/schedule", response_model=Schedule)
def my_schedule(
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
) -> Schedule:
    """Your current-term timetable. Empty means registered for nothing."""
    term = get_settings().current_term
    classes = get_schedule(session, student_id, term)
    return Schedule(
        student_id=student_id,
        term_code=term,
        total_credits=sum(c["credits"] for c in classes),
        classes=classes,
    )


@router.get("/courses", response_model=CourseHistory)
def my_courses(
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
) -> CourseHistory:
    """Your full academic history, newest term first, including in-progress."""
    return CourseHistory(
        student_id=student_id,
        academics=get_academic_summary(session, student_id),
        courses=get_course_history(session, student_id),
    )


@router.get("/degree-progress", response_model=DegreeProgress)
def my_degree_progress(
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
) -> DegreeProgress:
    """Your progress per requirement category.

    `all_categories_satisfied` is the graduation credit test, not
    `credits_earned >= total_credits_required`: surplus credits in one category
    never offset a shortfall in another.
    """
    profile = get_profile(session, student_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Your record was not found")

    categories = get_degree_progress(session, student_id)
    summary = get_academic_summary(session, student_id)
    return DegreeProgress(
        student_id=student_id,
        program_code=profile["program_code"],
        program_name=profile["program_name"],
        total_credits_required=profile["total_credits_required"],
        credits_earned=summary["credits_earned"],
        credits_in_progress=summary["credits_in_progress"],
        categories=categories,
        all_categories_satisfied=bool(categories)
        and all(c["is_satisfied"] for c in categories),
        unsatisfied_categories=[
            c["category_name"] for c in categories if not c["is_satisfied"]
        ],
    )


@router.get("/eligibility/{course_code:path}", response_model=Eligibility)
def my_eligibility(
    course_code: str = Path(
        description="Course code. Contains a space, so URL-encode it as e.g. MECH%20310.",
        examples=["MECH 310"],
    ),
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
) -> Eligibility:
    """Whether *you* may register for a course this term, and why."""
    result = check_eligibility(session, student_id, course_code)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No course {course_code}")
    return Eligibility(**result)


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
) -> ChatResponse:
    """Ask the assistant a question.

    Omit `conversation_id` to start a thread; pass the one you get back to
    continue it, which is what makes "what about next term?" resolve without
    repeating the question.

    Note what is *not* a parameter here: the student ID. It comes from the
    session, is placed in the agent's dependency object, and reaches the tools
    from there. The model is never asked for it and has no argument through
    which to supply one.
    """
    try:
        turn = chat_service.send(
            session,
            student_id=student_id,
            message=body.message,
            conversation_id=body.conversation_id,
        )
    except conversations.ConversationNotFound:
        # 404 rather than 403: confirming that the conversation exists but
        # belongs to someone else is itself a small disclosure.
        raise HTTPException(
            status_code=404, detail="No such conversation"
        ) from None

    return ChatResponse(
        conversation_id=turn.conversation_id,
        reply=turn.reply,
        model_name=turn.model_name,
        tool_calls=turn.tool_calls,
    )


@router.get("/conversations/{conversation_id}", response_model=Transcript)
def my_conversation(
    conversation_id: int,
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
) -> Transcript:
    """A thread of yours. Another student's thread is a 404, not a 403."""
    try:
        conversations.load_for_student(session, conversation_id, student_id)
    except conversations.ConversationNotFound:
        raise HTTPException(status_code=404, detail="No such conversation") from None

    return Transcript(
        conversation_id=conversation_id,
        messages=[
            TranscriptMessage(
                role=message.role,
                content=message.content,
                created_at=message.created_at,
            )
            for message in conversations.transcript(session, conversation_id)
        ],
    )
