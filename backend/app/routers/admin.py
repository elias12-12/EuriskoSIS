"""Admin panel endpoints -- Phase 6.

Every route here depends on `auth.current_admin`. Nothing here is reachable with
a student token: they are separate tables and separate dependencies, so an
endpoint cannot accidentally accept the wrong kind of principal.

Two things this surface deliberately does **not** offer:

- **No student-record writes.** The dataset is frozen ("Do not edit. Any change
  invalidates comparison across teams"), so the browsers are read-only. An admin
  panel that could edit a transcript would also be a way to make the five test
  students disagree with the verification scripts.
- **No arbitrary document upload.** See `POST /admin/documents/{filename}` --
  only the two registered documents can be replaced, because `ingestion/parse.py`
  refuses filenames it has no chunker for, and a generic chunker is precisely
  what Phase 3 argued against.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app import auth, browse
from app.config import get_settings
from app.db import get_session
from app.retrieval import corpus_status
from app.schemas import (
    AdminLoginRequest,
    AssistantSettingsResponse,
    AssistantSettingsUpdate,
    BrowsePage,
    DocumentStatus,
    FilterOptions,
    IngestReport,
    LoginResponse,
)
from ingestion.embed import MissingAPIKey
from ingestion.parse import CHUNKERS
from ingestion.pipeline import ingest_all, ingest_document

router = APIRouter(prefix="/admin", tags=["admin"])

# Applied to every route below except login. Declared once so a new route cannot
# be added unauthenticated by forgetting a dependency.
AdminOnly = Depends(auth.current_admin)


@router.post("/login", response_model=LoginResponse)
def login(
    body: AdminLoginRequest,
    session: Session = Depends(get_session),
) -> LoginResponse:
    """Exchange the shared administrator password for a session token."""
    token, expires_at = auth.admin_login(session, body.password)
    return LoginResponse(access_token=token, expires_at=expires_at, student_id=None)


@router.post("/logout", status_code=204)
def logout(
    token: str = Depends(auth.current_student_token),
    session: Session = Depends(get_session),
) -> None:
    auth.admin_logout(session, token)


# --- behaviour configuration -------------------------------------------------


@router.get("/settings", response_model=AssistantSettingsResponse)
def read_settings(
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> AssistantSettingsResponse:
    from app import assistant_config

    config = assistant_config.load(session)
    return AssistantSettingsResponse(
        tone=config.tone,
        model_name=config.model_name,
        response_length=config.response_length,
        temperature=config.temperature,
    )


@router.put("/settings", response_model=AssistantSettingsResponse)
def update_settings(
    body: AssistantSettingsUpdate,
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> AssistantSettingsResponse:
    """Change how the assistant behaves, from the very next request.

    Nothing is cached and nothing restarts: `assistant_config.load` runs at the
    start of every chat turn, so the next message sent by any student uses these
    values. That is PROJECT_PLAN Phase 6's exit check, and it is a property of
    where the read happens rather than of anything this handler does.
    """
    from app.models import AssistantSettings

    row = session.get(AssistantSettings, 1)
    if row is None:
        raise HTTPException(
            status_code=500,
            detail="assistant_settings row 1 is missing; re-run alembic upgrade head",
        )

    row.tone = body.tone
    row.model_name = body.model_name
    row.response_length = body.response_length
    row.temperature = body.temperature
    session.commit()

    return AssistantSettingsResponse(
        tone=row.tone,
        model_name=row.model_name,
        response_length=row.response_length,
        temperature=row.temperature,
    )


# --- documents ---------------------------------------------------------------


@router.get("/documents", response_model=list[DocumentStatus])
def list_documents(
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> list[DocumentStatus]:
    return [DocumentStatus(**row) for row in corpus_status(session)]


@router.post("/documents/reingest", response_model=list[IngestReport])
def reingest(
    force: bool = Query(
        default=False,
        description=(
            "Re-embed even if the file is unchanged. Needed after a chunker "
            "change, when the PDF is identical but the chunks are not."
        ),
    ),
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> list[IngestReport]:
    """Re-run ingestion over the registered documents.

    This is the button PROJECT_PLAN Phase 3 said the pipeline should have
    something real to call. It is the same `ingest_all` the CLI script uses, in
    process -- which is why `ingestion/` is on the backend image at all.
    """
    settings = get_settings()
    try:
        results = ingest_all(
            session,
            settings.data_dir,
            force=force,
            upload_dir=settings.upload_dir,
        )
    except MissingAPIKey as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return [
        IngestReport(
            filename=result.filename,
            status=result.status,
            chunk_count=result.chunk_count,
            page_count=result.page_count,
            unchanged=result.unchanged,
            error=result.error,
        )
        for result in results
    ]


@router.post("/documents/{filename}", response_model=IngestReport)
def replace_document(
    filename: str,
    file: UploadFile = File(description="A PDF replacing the named document."),
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> IngestReport:
    """Replace one of the registered documents and re-ingest it.

    Only the two filenames in `ingestion/parse.py` are accepted. This is not an
    arbitrary-upload endpoint, and that is the point: CLAUDE.md section 4 names
    the only files the assistant may know anything from, and a document with no
    registered chunker would either be rejected downstream or, worse, be run
    through a generic chunker and put ungrounded text behind a citation.

    The upload is written to `uploads/`, which the pipeline prefers over the
    read-only `data/` mount. The frozen dataset is never overwritten.
    """
    if filename not in CHUNKERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{filename!r} has no registered chunker. Replaceable documents: "
                f"{sorted(CHUNKERS)}"
            ),
        )

    settings = get_settings()
    upload_dir = settings.upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    destination = upload_dir / filename

    contents = file.file.read()
    if not contents.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400, detail="That file is not a PDF (no %PDF header)."
        )
    destination.write_bytes(contents)

    try:
        # force=True: the uploaded bytes differ from the previous ones, but so
        # would a re-upload of an identical file, and an admin who just uploaded
        # something expects it to be processed.
        result = ingest_document(session, destination, force=True)
    except MissingAPIKey as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return IngestReport(
        filename=result.filename,
        status=result.status,
        chunk_count=result.chunk_count,
        page_count=result.page_count,
        unchanged=result.unchanged,
        error=result.error,
    )


# --- read-only browsers ------------------------------------------------------


@router.get("/filters", response_model=FilterOptions)
def filters(
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> FilterOptions:
    """Distinct values for the browsers' dropdowns, queried rather than hardcoded."""
    return FilterOptions(**browse.filter_options(session))


@router.get("/students", response_model=BrowsePage)
def list_students(
    search: str | None = None,
    program_code: str | None = None,
    academic_status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> BrowsePage:
    page = browse.students(
        session,
        search=search,
        program_code=program_code,
        academic_status=academic_status,
        offset=offset,
        limit=limit,
    )
    return BrowsePage(total=page.total, items=page.items)


@router.get("/courses", response_model=BrowsePage)
def list_courses(
    search: str | None = None,
    subject: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> BrowsePage:
    page = browse.courses(
        session, search=search, subject=subject, offset=offset, limit=limit
    )
    return BrowsePage(total=page.total, items=page.items)


@router.get("/enrollments", response_model=BrowsePage)
def list_enrollments(
    student_id: str | None = None,
    course_code: str | None = None,
    term_code: str | None = None,
    grade: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    _: bool = AdminOnly,
    session: Session = Depends(get_session),
) -> BrowsePage:
    page = browse.enrollments(
        session,
        student_id=student_id,
        course_code=course_code,
        term_code=term_code,
        grade=grade,
        offset=offset,
        limit=limit,
    )
    return BrowsePage(total=page.total, items=page.items)
