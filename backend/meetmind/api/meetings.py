"""Meetings, participants, recordings, transcripts and meeting tasks."""
import logging
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from .. import task_queue
from ..ai import default_deps, qa
from ..ai.llm import AIUnavailableError
from ..ai.pipeline import PipelineError, resume_review
from ..config import ALLOWED_RECORDING_EXTENSIONS, Settings
from ..deps import AdminUser, CurrentUser, DbSession, SettingsDep, get_rag
from ..errors import APIError
from ..models import Meeting, MeetingParticipant, Recording, Task, User
from ..ratelimit import rate_limit
from ..schemas import AskIn, MeetingIn, MeetingPatch, ReviewIn, TaskIn
from ..serializers import meeting_out, meeting_summary_out, recording_out, task_out
from .common import get_meeting_for, org_user_by_email, visible_meetings

router = APIRouter()
log = logging.getLogger(__name__)

MeetingStatus = Literal["scheduled", "recording", "processing", "review", "completed", "failed"]


def _set_participants(db: Session, meeting: Meeting, emails: list[str]) -> None:
    existing = {p.email: p for p in meeting.participants}
    meeting.participants = [
        existing.get(email)
        or MeetingParticipant(email=email, user=org_user_by_email(db, meeting.organization_id, email))
        for email in emails
    ]


def _tasks_visible_to(user: User, meeting: Meeting) -> list[Task]:
    """Admins see every task; members only see tasks an admin has approved."""
    if user.role == "admin":
        return list(meeting.tasks)
    return [t for t in meeting.tasks if t.approved]


# ---------- meetings ----------

@router.get("")
def list_meetings(
    user: CurrentUser,
    db: DbSession,
    status: MeetingStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    q = visible_meetings(user)
    if status:
        q = q.where(Meeting.status == status)

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    meetings = db.scalars(
        q.options(selectinload(Meeting.participants), selectinload(Meeting.tasks))  # no N+1 queries
        .order_by(Meeting.scheduled_at.desc()).limit(limit).offset(offset)
    ).all()
    return {"items": [meeting_summary_out(m) for m in meetings], "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create_meeting(data: MeetingIn, admin: AdminUser, db: DbSession):
    meeting = Meeting(
        organization_id=admin.organization_id,
        created_by_id=admin.id,
        title=data.title,
        agenda=data.agenda,
        scheduled_at=data.scheduled_at,
        timezone=data.timezone,
    )
    _set_participants(db, meeting, data.participants)
    db.add(meeting)
    db.commit()
    return {"meeting": meeting_out(meeting, [])}


@router.get("/{meeting_id}")
def get_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: SettingsDep):
    from .slides import slide_token

    meeting = get_meeting_for(db, user, meeting_id)
    return {"meeting": meeting_out(meeting, _tasks_visible_to(user, meeting), slide_token(settings, user, meeting.id))}


@router.patch("/{meeting_id}")
def update_meeting(meeting_id: uuid.UUID, data: MeetingPatch, admin: AdminUser, db: DbSession):
    meeting = get_meeting_for(db, admin, meeting_id)
    for field in ("title", "agenda", "scheduled_at", "timezone"):
        if field in data.model_fields_set:
            value = getattr(data, field)
            if value is None and field not in {"agenda", "timezone"}:
                raise APIError(422, "validation_error", f"{field} cannot be empty")
            setattr(meeting, field, value)
    if data.participants is not None:
        _set_participants(db, meeting, data.participants)
    db.commit()
    return {"meeting": meeting_out(meeting, _tasks_visible_to(admin, meeting))}


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: uuid.UUID, request: Request, admin: AdminUser, db: DbSession, settings: SettingsDep):
    meeting = get_meeting_for(db, admin, meeting_id)
    paths = [r.storage_path for r in meeting.recordings]
    db.delete(meeting)
    db.commit()
    try:
        get_rag(request).remove_meeting(str(meeting_id))  # keep the search index in sync
    except Exception:  # noqa: BLE001
        log.warning("Could not remove meeting %s from the search index", meeting_id)
    for rel in paths:  # remove files only after the DB delete succeeded
        try:
            (Path(settings.upload_dir) / rel).unlink(missing_ok=True)
        except OSError:
            log.warning("Could not delete recording file %s", rel)
    # the meeting's folder also holds live-recording pieces and attached documents
    shutil.rmtree(Path(settings.upload_dir) / str(admin.organization_id) / str(meeting_id), ignore_errors=True)
    return Response(status_code=204)


# ---------- recordings & transcript ----------

_UPLOAD_DOC = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["file"],
            "properties": {"file": {"type": "string", "format": "binary"}},
        }}},
    }
}


def _copy_limited(src: BinaryIO, dest: Path, max_bytes: int) -> int:
    """Copy the upload to its final place in chunks; stop if it exceeds the size limit."""
    size = 0
    with dest.open("wb") as out:
        while chunk := src.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                break
            out.write(chunk)
    return size


def _store_recording(
    db: Session, settings: Settings, admin: User, meeting: Meeting, upload: UploadFile,
) -> Recording:
    """Blocking part of the upload (disk + database + queue) — runs in a worker thread."""
    # Never trust the client's filename for the path: store under a generated name
    original_name = PurePosixPath(upload.filename.replace("\\", "/")).name
    ext = PurePosixPath(original_name).suffix.lower()
    if ext not in ALLOWED_RECORDING_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_RECORDING_EXTENSIONS))
        raise APIError(400, "unsupported_file_type", f"Unsupported file type. Allowed: {allowed}")

    rel_path = Path(str(meeting.organization_id)) / str(meeting.id) / f"{uuid.uuid4()}{ext}"
    abs_path = Path(settings.upload_dir) / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    size = _copy_limited(upload.file, abs_path, settings.max_upload_bytes)
    if size == 0 or size > settings.max_upload_bytes:
        abs_path.unlink(missing_ok=True)
        if size == 0:
            raise APIError(400, "empty_file", "The uploaded file is empty")
        raise APIError(413, "file_too_large", f"Recordings can be at most {settings.max_upload_mb} MB")

    recording = Recording(
        meeting=meeting,
        uploaded_by_id=admin.id,
        original_filename=original_name[:255],
        storage_path=rel_path.as_posix(),
        content_type=upload.content_type,
        size_bytes=size,
        status="queued",
    )
    db.add(recording)
    _queue_processing(db, settings, meeting, recording)
    return recording


def _queue_processing(db: Session, settings: Settings, meeting: Meeting, recording: Recording) -> None:
    """Hand the recording to the background worker (LangGraph pipeline)."""
    recording.status, recording.error = "queued", None
    meeting.status = "processing"
    meeting.ai_thread_id = None  # a new run replaces any review still waiting
    db.commit()
    try:
        task_queue.enqueue(settings, "meetmind.jobs.process_recording", str(recording.id))
    except Exception:
        log.exception("Could not enqueue recording %s", recording.id)
        recording.status = "failed"
        recording.error = "Could not queue the recording for processing. Please try again."
        meeting.status = "failed"
        db.commit()
        raise APIError(503, "queue_unavailable", recording.error)


@router.post("/{meeting_id}/recordings", status_code=202, openapi_extra=_UPLOAD_DOC)
async def upload_recording(
    meeting_id: uuid.UUID, request: Request, admin: AdminUser, db: DbSession, settings: SettingsDep,
):
    """Upload a recording (multipart field `file`). Processing happens in the background worker.

    The body is read *inside* the handler on purpose: FastAPI would otherwise accept the whole
    file before checking the login, letting anyone fill the disk with uploads.
    """
    meeting = await run_in_threadpool(get_meeting_for, db, admin, meeting_id)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + 1024 * 1024:
        raise APIError(413, "file_too_large", f"Recordings can be at most {settings.max_upload_mb} MB")

    async with request.form(max_files=1, max_fields=2) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise APIError(400, "no_file", "Attach the recording as form field 'file'")
        recording = await run_in_threadpool(_store_recording, db, settings, admin, meeting, upload)

    return {"recording": recording_out(recording)}


@router.get("/{meeting_id}/transcript")
def get_transcript(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession):
    meeting = get_meeting_for(db, user, meeting_id)
    t = meeting.transcript
    if t is None:
        raise APIError(404, "not_found", "No transcript yet for this meeting")
    return {"transcript": {
        "text": t.text, "segments": t.segments or [], "language": t.language, "provider": t.provider,
    }}


# ---------- tasks of a meeting ----------

@router.get("/{meeting_id}/tasks")
def list_meeting_tasks(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession):
    meeting = get_meeting_for(db, user, meeting_id)
    return {"items": [task_out(t) for t in _tasks_visible_to(user, meeting)]}


@router.post("/{meeting_id}/tasks", status_code=201)
def create_task(meeting_id: uuid.UUID, data: TaskIn, admin: AdminUser, db: DbSession):
    meeting = get_meeting_for(db, admin, meeting_id)
    task = Task(
        organization_id=meeting.organization_id,
        meeting=meeting,
        title=data.title,
        description=data.description,
        assignee_email=data.assignee_email,
        assignee=org_user_by_email(db, meeting.organization_id, data.assignee_email),
        due_date=data.due_date,
        source="manual",
        approved=True,
        priority=data.priority,
    )
    db.add(task)
    db.commit()
    return {"task": task_out(task)}


# ---------- AI: review, retry, Q&A ----------

@router.post("/{meeting_id}/review")
def review_meeting(
    meeting_id: uuid.UUID, data: ReviewIn, request: Request, admin: AdminUser, db: DbSession, settings: SettingsDep,
):
    """Approve AI-drafted tasks (listed IDs) and discard the rest; resumes the paused LangGraph run."""
    meeting = get_meeting_for(db, admin, meeting_id)
    thread_id = meeting.ai_thread_id
    if meeting.status != "review" or not thread_id:
        raise APIError(409, "not_in_review", "This meeting has no AI results waiting for review")

    task_ids = {t.id for t in meeting.tasks}
    unknown = [str(i) for i in data.approved_task_ids if i not in task_ids]
    if unknown:
        raise APIError(422, "validation_error", "Some tasks do not belong to this meeting", {"task_ids": unknown})

    # Claim the review atomically so two admins clicking at once can't resume it twice
    claimed = db.execute(
        update(Meeting)
        .where(Meeting.id == meeting.id, Meeting.ai_thread_id == thread_id, Meeting.status == "review")
        .values(ai_thread_id=None)
    ).rowcount
    db.commit()
    if claimed != 1:
        raise APIError(409, "not_in_review", "This review was already completed")

    deps = default_deps(settings, request.app.state.session_factory)
    try:
        resume_review(deps, thread_id, [str(i) for i in data.approved_task_ids])
    except PipelineError as exc:
        raise APIError(409, "not_in_review", str(exc))
    except Exception:
        log.exception("Resuming review %s failed", thread_id)
        db.execute(update(Meeting).where(Meeting.id == meeting.id).values(ai_thread_id=thread_id))
        db.commit()
        raise APIError(503, "review_failed", "Could not complete the review. Please try again.")

    db.expire_all()
    meeting = get_meeting_for(db, admin, meeting_id)
    return {"meeting": meeting_out(meeting, _tasks_visible_to(admin, meeting))}


@router.post("/{meeting_id}/reprocess", status_code=202)
def reprocess_meeting(meeting_id: uuid.UUID, admin: AdminUser, db: DbSession, settings: SettingsDep):
    """Run the AI pipeline again on the latest recording (e.g. after adding an API key)."""
    meeting = get_meeting_for(db, admin, meeting_id)
    if not meeting.recordings:
        raise APIError(409, "no_recording", "Upload a recording first")
    recording = meeting.recordings[-1]
    if recording.status in {"queued", "processing"}:
        raise APIError(409, "already_processing", "This recording is already being processed")
    _queue_processing(db, settings, meeting, recording)
    return {"recording": recording_out(recording)}


@router.post("/{meeting_id}/ask", dependencies=[rate_limit("20/minute", "ask")])
def ask_meeting(
    meeting_id: uuid.UUID, data: AskIn, request: Request, user: CurrentUser, db: DbSession, settings: SettingsDep,
):
    """Answer a question about this meeting. The multi-tool assistant searches the transcript, notes,
    attached documents, tasks and other meetings; if no AI model can run it, a simpler direct answer is used."""
    meeting = get_meeting_for(db, user, meeting_id)
    has_docs = any(d.status == "ready" for d in meeting.documents)
    if meeting.transcript is None and not meeting.summary and not meeting.notes and not has_docs:
        raise APIError(409, "not_ready", "Ask again once the recording has been processed (or add notes/documents)")

    from ..ai.agents import run_assistant, scope_for

    try:
        scope = scope_for(request.app.state.session_factory, db, settings, get_rag(request), user, str(meeting.id))
        result = run_assistant(scope, data.question, [t.model_dump() for t in data.history])
        return {"answer": result.answer, "model": result.model, "sources": result.sources, "tools": result.tools_used}
    except AIUnavailableError:
        log.warning("Assistant unavailable for meeting %s; using the direct answer", meeting.id)

    tasks = _tasks_visible_to(user, meeting)
    context = qa.build_context(meeting, tasks)
    if len(context) > settings.rag_full_context_chars:
        # Long meeting: send only the transcript parts that match the question (RAG)
        try:
            hits = get_rag(request).search(data.question, str(meeting.organization_id), [str(meeting.id)])
            excerpts = "\n\n".join(h.text for h in hits if h.metadata.get("kind") == "transcript")
            context = qa.build_context(meeting, tasks, transcript_override=excerpts or "(no matching excerpts)")
        except Exception:  # noqa: BLE001 - fall back to the full record
            log.warning("Retrieval failed for meeting %s; using the full transcript", meeting.id, exc_info=True)
    try:
        answer, model = qa.answer_question(settings, context, data.question)
    except AIUnavailableError as exc:
        raise APIError(503, "ai_unavailable", str(exc))
    return {"answer": answer, "model": model, "sources": [], "tools": []}
