"""Pydantic response models.

Explicit schemas rather than returning raw dicts, for two reasons that pay off
later: Swagger becomes a usable manual test surface (the Phase 2 exit check is
"hit every endpoint for every student ID"), and these same shapes are what the
PydanticAI tools will return in Phase 4, so the agent gets typed results instead
of loose dictionaries.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from pydantic import BaseModel, Field


class AcademicSummary(BaseModel):
    gpa: Decimal | None = Field(
        None,
        description=(
            "Cumulative GPA over graded attempts, or null when nothing is graded "
            "yet. Null rather than 0.00 -- a first-term student has no GPA, and "
            "0.00 would read as failing."
        ),
    )
    quality_points: Decimal
    gpa_credits: int = Field(description="Credits counted in the GPA denominator.")
    credits_earned: int
    credits_in_progress: int


class Profile(BaseModel):
    student_id: str
    first_name: str
    last_name: str
    email: str
    program_code: str
    program_name: str
    total_credits_required: int
    entry_term: str
    entry_term_name: str
    expected_graduation_term: str
    academic_status: str
    advisor_name: str
    scenario_note: str | None = None
    academics: AcademicSummary


class ScheduledClass(BaseModel):
    course_code: str
    title: str
    credits: int
    days: str
    start_time: time
    end_time: time
    room: str
    instructor: str


class Schedule(BaseModel):
    student_id: str
    term_code: str
    total_credits: int
    classes: list[ScheduledClass]


class CourseHistoryEntry(BaseModel):
    term_code: str
    term_name: str
    start_date: date
    course_code: str
    title: str
    credits: int
    grade: str | None = Field(None, description="Null while the course is in progress.")
    grade_points: Decimal | None
    earns_credit: bool | None
    included_in_gpa: bool | None
    status: str


class CourseHistory(BaseModel):
    student_id: str
    academics: AcademicSummary
    courses: list[CourseHistoryEntry]


class CategoryProgress(BaseModel):
    category_id: str
    category_name: str
    credits_required: int
    selection_rule: str = Field(
        description="'ALL' (every course required) or 'ANY_N' (any N of them)."
    )
    courses_required: int | None = Field(
        None, description="N, for ANY_N categories; null for ALL."
    )
    min_grade_points: Decimal | None = Field(
        None,
        description=(
            "Minimum grade points for a course to count toward this category "
            "(1.70 for Major Core), or null if any credit-earning grade counts."
        ),
    )
    courses_offered: int
    courses_counted: int
    credits_counted: int = Field(
        description="Credits held in this category before capping."
    )
    credits_applied: int = Field(
        description=(
            "Credits that count toward the requirement, capped at credits_required. "
            "Surplus never offsets a shortfall in another category."
        )
    )
    credits_remaining: int
    is_satisfied: bool
    courses_in_progress: int
    credits_in_progress: int


class DegreeProgress(BaseModel):
    student_id: str
    program_code: str
    program_name: str
    total_credits_required: int
    credits_earned: int
    credits_in_progress: int
    categories: list[CategoryProgress]
    all_categories_satisfied: bool = Field(
        description=(
            "True only when every category is individually satisfied. This, not "
            "the credit total, is the graduation credit test."
        )
    )
    unsatisfied_categories: list[str]


class PrerequisiteCheck(BaseModel):
    course_code: str
    title: str
    best_grade: str | None
    best_grade_points: Decimal | None
    satisfied: bool
    currently_taking: bool


class EligibilityReason(BaseModel):
    rule: str
    satisfied: bool
    detail: str


class Eligibility(BaseModel):
    student_id: str
    course_code: str
    title: str
    credits: int
    term_code: str
    eligible: bool
    requires_advisor_approval: bool
    offered_this_term: bool
    prerequisites: list[PrerequisiteCheck]
    attempts_used: int
    already_registered: bool
    has_credit: bool
    academic_status: str
    registered_credits: int
    prospective_credits: int
    credit_cap: int
    reasons: list[EligibilityReason] = Field(
        description=(
            "Every rule that produced a finding, with satisfied=false for each "
            "blocker. Empty means nothing stood in the way."
        )
    )
    notes: list[str]


class SearchHit(BaseModel):
    content: str
    document_title: str
    document_filename: str
    chunk_kind: str
    section_ref: str | None = None
    section_title: str | None = None
    page: int | None = None
    similarity: float = Field(
        description="1 - cosine distance. 1.0 is identical, 0.0 unrelated."
    )
    citation: str = Field(
        description=(
            "Ready-to-quote source, e.g. 'Student Handbook 2026-2027, section "
            "2.3 (Adding, dropping and withdrawing), page 2'. Every "
            "document-based answer must carry one (CLAUDE.md section 7 rule 5)."
        )
    )


class SearchResults(BaseModel):
    query: str
    hits: list[SearchHit]


class DocumentStatus(BaseModel):
    filename: str
    title: str
    status: str = Field(description="pending, ingesting, ready or failed.")
    page_count: int | None = None
    uploaded_at: datetime
    chunk_count: int
    embedded_count: int = Field(
        description=(
            "Chunks with an embedding. Lower than chunk_count means a partial "
            "ingestion -- 'ingested but not searchable' is its own failure."
        )
    )
    error: str | None = None


# --- auth and chat (Phase 4) ------------------------------------------------


class LoginRequest(BaseModel):
    student_id: str = Field(
        description="University student ID.", examples=["S2023011"]
    )


class LoginResponse(BaseModel):
    access_token: str = Field(
        description="Send as `Authorization: Bearer <token>` on every /me/* request."
    )
    token_type: str = "bearer"
    expires_at: datetime
    student_id: str


class WhoAmI(BaseModel):
    student_id: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, examples=["What's my schedule this term?"])
    conversation_id: int | None = Field(
        None,
        description=(
            "Omit to start a new thread; pass the id you got back to continue "
            "one. There is deliberately no student_id field -- identity comes "
            "from the session."
        ),
    )


class ChatResponse(BaseModel):
    conversation_id: int
    reply: str
    model_name: str = Field(
        description="Which model answered, as configured in assistant_settings."
    )
    tool_calls: list[str] = Field(
        description=(
            "Tools the agent called, in order. Surfaced because 'did it actually "
            "look anything up?' is the first question when an answer looks wrong."
        )
    )


class TranscriptMessage(BaseModel):
    role: str
    content: str
    created_at: datetime


class Transcript(BaseModel):
    conversation_id: int
    messages: list[TranscriptMessage]


class Appointment(BaseModel):
    id: int
    advisor_name: str
    proposed_time: datetime
    reason: str
    status: str = Field(
        description=(
            "'confirmed' or 'cancelled'. There is no 'proposed' state -- a "
            "proposal is never persisted, so every row here was agreed to."
        )
    )
    confirmed_at: datetime
    conversation_id: int | None = None
