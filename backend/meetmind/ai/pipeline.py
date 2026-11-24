"""The meeting pipeline as a LangGraph state machine.

    START -> transcribe -> analyze -> save_draft -> index --(AI found tasks)--> human_review -> finalize -> END
                                                          \\--(no tasks)---------------------> finalize

`index` adds the meeting to the RAG search index (ChromaDB). If that fails it is retried in the
background, so search problems never block a meeting.

`human_review` calls `interrupt()`: the graph stops, its state is checkpointed (Postgres),
and the meeting shows status "review". When an admin approves tasks in the UI, the API
resumes the same thread with `Command(resume=decision)` and `finalize` applies the decision.

Nodes get the database/AI helpers through `PipelineDeps`, so tests can pass fakes.
Graph state holds only small IDs and the analysis; the transcript itself lives in the DB.
"""
import logging
import uuid
from collections.abc import Callable
from datetime import date, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.orm import sessionmaker

from .. import notifications, task_queue
from ..config import Settings
from ..models import Meeting, Recording, Task, Transcript
from .analysis import (
    LLMAnalysis, MeetingContext, clean_title, evidence_supported, match_owner, parse_due_date, resolve_due_date,
    task_priority,
)
from .checkpoint import open_checkpointer
from .transcription import TranscriptionResult, format_transcript

log = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """Raised with a message that is safe to show users."""


class PipelineState(TypedDict, total=False):
    recording_id: str
    meeting_id: str
    analysis: dict
    ai_model: str
    draft_task_ids: list[str]
    decision: dict


@dataclass
class PipelineDeps:
    session_factory: sessionmaker
    settings: Settings
    transcribe: Callable[[Path, str | None], TranscriptionResult]
    analyze: Callable[[MeetingContext, str], tuple[LLMAnalysis, str]]
    rag: Callable[[], object] | None = None  # returns the RAGService; None disables indexing


def meeting_local_date(meeting: Meeting) -> date:
    """The calendar day of the meeting where it took place (UTC if no time zone was given)."""
    at = meeting.scheduled_at
    if at.tzinfo is None:  # SQLite returns naive UTC
        at = at.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(meeting.timezone) if meeting.timezone else timezone.utc
    except Exception:  # noqa: BLE001
        zone = timezone.utc
    return at.astimezone(zone).date()


def _participants(meeting: Meeting) -> list[tuple[str | None, str]]:
    return [(p.user.name if p.user else None, p.email) for p in meeting.participants]


def _whisper_hint(meeting: Meeting) -> str:
    names = ", ".join(name or email.split("@")[0] for name, email in _participants(meeting))
    return f"Meeting: {meeting.title}. Participants: {names}."


_NO_NAME = {"", "unknown", "n/a", "na", "none", "null", "someone", "speaker", "audience", "participant", "attendee"}


def _clean_follow_up(f: dict) -> dict:
    """Models sometimes write 'unknown' instead of null for who asked/answered."""
    for key in ("asked_by", "answered_by"):
        value = (f.get(key) or "").strip()
        f[key] = None if value.lower() in _NO_NAME else value
    f["question"] = f["question"].strip()
    f["answer"] = (f.get("answer") or "").strip() or None
    return f


def _notes_for_ai(meeting: Meeting) -> list[str]:
    """Notes typed in MeetMind, with their position in the recording and author, for the AI."""
    from .transcription import format_timestamp

    out = []
    for n in meeting.notes:
        at = f"[{format_timestamp(n.offset_seconds)}] " if n.offset_seconds is not None else ""
        who = (n.author.name or n.author.email) if n.author else "someone"
        if n.kind == "bookmark":
            out.append(f"{at}MOMENT MARKED IMPORTANT by {who}" + (f": {n.text}" if n.text and n.text != "Important moment" else ""))
        else:
            out.append(f"{at}{who} noted: {n.text}")
    return out


def build_graph(deps: PipelineDeps, checkpointer):
    sf = deps.session_factory

    def transcribe(state: PipelineState) -> PipelineState:
        with sf() as db:
            rec = db.get(Recording, uuid.UUID(state["recording_id"]))
            if rec is None:
                raise PipelineError("The recording was deleted before it could be processed.")
            meeting = rec.meeting
            rec.status, rec.error = "processing", None
            meeting.status = "processing"
            db.commit()
            audio_path = Path(deps.settings.upload_dir) / rec.storage_path
            pieces = Path(deps.settings.upload_dir) / Path(rec.storage_path).parent / f"live-{rec.id}"
            if pieces.exists():  # recorded in the browser: join the pieces into one file first
                from ..media import assemble_pieces

                rec.size_bytes = assemble_pieces(pieces, audio_path)
                db.commit()
            hint, meeting_id = _whisper_hint(meeting), str(meeting.id)

        result = deps.transcribe(audio_path, hint)  # slow: keep no DB connection open meanwhile
        if not result.text.strip():
            raise PipelineError("No speech was found in the recording.")

        with sf() as db:
            meeting = db.get(Meeting, uuid.UUID(meeting_id))
            t = meeting.transcript or Transcript(meeting=meeting)
            t.recording_id = uuid.UUID(state["recording_id"])
            t.text, t.segments = result.text, result.segments
            t.language, t.provider = result.language, result.provider
            db.add(t)
            db.commit()
        return {"meeting_id": meeting_id}

    def analyze(state: PipelineState) -> PipelineState:
        with sf() as db:
            meeting = db.get(Meeting, uuid.UUID(state["meeting_id"]))
            ctx = MeetingContext(
                title=meeting.title,
                meeting_date=meeting_local_date(meeting),
                agenda=meeting.agenda,
                participants=_participants(meeting),
                notes=_notes_for_ai(meeting),
            )
            text = format_transcript(meeting.transcript.segments, meeting.transcript.text)
        analysis, model = deps.analyze(ctx, text)
        return {"analysis": analysis.model_dump(), "ai_model": model}

    def save_draft(state: PipelineState, config: RunnableConfig) -> PipelineState:
        analysis = LLMAnalysis.model_validate(state["analysis"])
        with sf() as db:
            meeting = db.get(Meeting, uuid.UUID(state["meeting_id"]))
            # Re-processing: throw away earlier AI drafts nobody approved
            for old in [t for t in meeting.tasks if t.source == "ai" and not t.approved]:
                meeting.tasks.remove(old)  # delete-orphan cascade deletes the row
            db.flush()

            meeting.summary = analysis.summary.strip()
            meeting.strategy_shifts = [s.strip() for s in analysis.strategy_shifts if s.strip()]
            meeting.open_questions = [q.strip() for q in analysis.open_questions if q.strip()]
            meeting.follow_ups = [_clean_follow_up(f.model_dump()) for f in analysis.follow_ups if f.question.strip()]
            meeting.ai_model = state.get("ai_model")

            participants = _participants(meeting)
            meeting_date = meeting_local_date(meeting)
            users_by_email = {p.email: p.user for p in meeting.participants}
            # tasks may come from the audio or from notes typed during the meeting
            transcript_text = "\n".join([meeting.transcript.text, *(n.text for n in meeting.notes)])
            drafts, seen = [], set()
            for item in analysis.tasks:
                title = clean_title(item.title, item.due_text)
                key = title.lower()
                if not key or key in seen:
                    continue
                if not evidence_supported(item.evidence, transcript_text):
                    log.info("Dropping task without matching transcript evidence: %r", item.title)
                    continue
                seen.add(key)
                email = match_owner(item.owner, participants)
                task = Task(
                    organization_id=meeting.organization_id,
                    meeting=meeting,
                    title=title[:300],
                    assignee_email=email,
                    assignee=users_by_email.get(email) if email else None,
                    # computed from the spoken words; the model's own date only as a fallback
                    due_date=resolve_due_date(item.due_text, meeting_date, deps.settings.asap_working_days)
                    or parse_due_date(item.due_date),
                    priority=task_priority(item.priority, item.due_text),
                    source="ai",
                    approved=False,
                    evidence=item.evidence.strip()[:1000],
                    suggested_owner=None if email else (item.owner or "").strip()[:120] or None,
                )
                db.add(task)
                drafts.append(task)

            rec = db.get(Recording, uuid.UUID(state["recording_id"]))
            if rec is not None:
                rec.status = "done"
            meeting.status = "review" if drafts else "completed"
            meeting.ai_thread_id = config["configurable"]["thread_id"] if drafts else None
            db.commit()
            log.info("Meeting %s analysed by %s: %d draft task(s)", meeting.id, meeting.ai_model, len(drafts))
            if drafts:
                notifications.notify_review_ready(deps.settings, db, meeting, len(drafts))
            return {"draft_task_ids": [str(t.id) for t in drafts]}

    def index(state: PipelineState) -> PipelineState:
        if deps.rag is None:
            return {}
        meeting_id = state["meeting_id"]
        try:
            with sf() as db:
                deps.rag().index_meeting(db.get(Meeting, uuid.UUID(meeting_id)))
        except Exception as exc:  # noqa: BLE001 - search is a bonus, never a blocker
            log.warning("Search indexing failed for meeting %s (%s); retrying in the background", meeting_id, exc)
            try:
                from rq import Retry

                task_queue.enqueue(deps.settings, "meetmind.jobs.index_meeting", meeting_id,
                                   retry=Retry(max=5, interval=[30, 120, 600, 1800, 3600]))
            except Exception:  # noqa: BLE001
                log.exception("Could not queue indexing retry for meeting %s", meeting_id)
        return {}

    def human_review(state: PipelineState) -> PipelineState:
        # Pauses here. The value passed to Command(resume=...) becomes `decision`.
        decision = interrupt({"meeting_id": state["meeting_id"], "draft_task_ids": state["draft_task_ids"]})
        return {"decision": decision}

    def finalize(state: PipelineState) -> PipelineState:
        approved = set((state.get("decision") or {}).get("approved_task_ids", []))
        with sf() as db:
            meeting = db.get(Meeting, uuid.UUID(state["meeting_id"]))
            newly_approved = []
            for task_id in state.get("draft_task_ids", []):
                task = db.get(Task, uuid.UUID(task_id))
                if task is None or task.meeting_id != meeting.id:
                    continue  # deleted/moved by an admin meanwhile
                if task_id in approved:
                    if not task.approved:  # already approved (e.g. by the task assistant): no second email
                        newly_approved.append(task)
                    task.approved = True
                elif not task.approved:
                    db.delete(task)  # rejected draft
            meeting.status = "completed"
            meeting.ai_thread_id = None
            db.commit()
            notifications.notify_tasks_assigned(deps.settings, meeting, newly_approved)
        return {}

    def after_save(state: PipelineState) -> str:
        return "human_review" if state.get("draft_task_ids") else "finalize"

    graph = StateGraph(PipelineState)
    graph.add_node("transcribe", transcribe)
    graph.add_node("analyze", analyze)
    graph.add_node("save_draft", save_draft)
    graph.add_node("index", index)
    graph.add_node("human_review", human_review)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "transcribe")
    graph.add_edge("transcribe", "analyze")
    graph.add_edge("analyze", "save_draft")
    graph.add_edge("save_draft", "index")
    graph.add_conditional_edges("index", after_save, ["human_review", "finalize"])
    graph.add_edge("human_review", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer, name="meetmind-pipeline")


def _config(thread_id: str, **metadata) -> RunnableConfig:
    # thread_id = which checkpoint to use; tags/metadata show up in LangSmith traces
    return {"configurable": {"thread_id": thread_id}, "tags": ["meetmind"], "metadata": metadata}


def run_pipeline(deps: PipelineDeps, recording_id: str) -> str:
    """Process a recording until done or paused for review. Returns the LangGraph thread id."""
    thread_id = f"recording-{recording_id}-{uuid.uuid4().hex[:8]}"  # fresh thread per attempt
    with open_checkpointer(deps.settings.database_url) as cp:
        build_graph(deps, cp).invoke(
            {"recording_id": recording_id}, _config(thread_id, recording_id=recording_id),
        )
    return thread_id


def resume_review(deps: PipelineDeps, thread_id: str, approved_task_ids: list[str]) -> None:
    """Continue a paused pipeline with the admin's decision."""
    with open_checkpointer(deps.settings.database_url) as cp:
        graph = build_graph(deps, cp)
        config = _config(thread_id)
        if not graph.get_state(config).next:
            raise PipelineError("This review was already completed.")
        graph.invoke(Command(resume={"approved_task_ids": approved_task_ids}), config)


def mark_failed(deps: PipelineDeps, recording_id: str, message: str) -> None:
    with deps.session_factory() as db:
        rec = db.get(Recording, uuid.UUID(recording_id))
        if rec is None:
            return
        rec.status, rec.error = "failed", message
        rec.meeting.status = "failed"
        db.commit()
        notifications.notify_processing_failed(deps.settings, db, rec.meeting, message)


def user_message(exc: Exception) -> str:
    from .llm import AIUnavailableError
    from .transcription import TranscriptionError

    if isinstance(exc, (PipelineError, TranscriptionError, AIUnavailableError)):
        return str(exc)
    return "Processing failed unexpectedly. An admin can retry; details are in the worker logs."
