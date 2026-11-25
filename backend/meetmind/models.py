"""Database models.

Multi-tenancy: every row belongs (directly or through its meeting) to an Organization,
and every query in the API is filtered by the current user's organization.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# JSONB on Postgres, plain JSON elsewhere (SQLite in tests)
JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))

    users: Mapped[list["User"]] = relationship(back_populates="organization")


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="member")        # admin | member
    status: Mapped[str] = mapped_column(String(20), default="invited")     # invited | active | disabled
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship(back_populates="users")


class Meeting(TimestampMixin, Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200))
    agenda: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # IANA zone where the meeting happens (e.g. "Asia/Kolkata"): "by Friday" means Friday *there*
    timezone: Mapped[str | None] = mapped_column(String(64))
    # scheduled -> processing -> review (AI done, awaiting admin approval) -> completed | failed
    status: Mapped[str] = mapped_column(String(20), default="scheduled")

    # Filled by the AI pipeline (Phase 3)
    summary: Mapped[str | None] = mapped_column(Text)
    strategy_shifts: Mapped[list | None] = mapped_column(JSONType)
    open_questions: Mapped[list | None] = mapped_column(JSONType)
    # Questions raised during the meeting and the answers given: [{"question", "answer", "asked_by", "answered_by"}]
    follow_ups: Mapped[list | None] = mapped_column(JSONType)
    ai_model: Mapped[str | None] = mapped_column(String(120))  # which model wrote the analysis
    # LangGraph thread waiting for the admin's review (resumed by POST /meetings/{id}/review)
    ai_thread_id: Mapped[str | None] = mapped_column(String(100))

    participants: Mapped[list["MeetingParticipant"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="MeetingParticipant.email"
    )
    recordings: Mapped[list["Recording"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="Recording.created_at"
    )
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", uselist=False
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="Task.created_at"
    )
    notes: Mapped[list["MeetingNote"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="MeetingNote.created_at"
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="Document.created_at"
    )


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"
    __table_args__ = (UniqueConstraint("meeting_id", "email", name="uq_participant_meeting_email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    # Linked when the participant is a MeetMind user in the same organization
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)

    meeting: Mapped[Meeting] = relationship(back_populates="participants")
    user: Mapped[User | None] = relationship()


class Recording(TimestampMixin, Base):
    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # recording (live, still receiving pieces) | queued | processing | done | failed
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    # upload | room (in-person, microphone) | online (other meeting app's audio + mic) | screen (with video)
    mode: Mapped[str] = mapped_column(String(20), default="upload")
    duration_seconds: Mapped[float | None] = mapped_column()
    important: Mapped[bool] = mapped_column(default=False)  # starred by an admin: keep and highlight
    chunks_received: Mapped[int] = mapped_column(default=0)  # live recordings arrive in numbered pieces
    # while processing: preparing (joining live pieces) | transcribing | analysing; None otherwise
    stage: Mapped[str | None] = mapped_column(String(20))

    meeting: Mapped[Meeting] = relationship(back_populates="recordings")


class Transcript(TimestampMixin, Base):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), unique=True)
    recording_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recordings.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text)
    # [{"start": 0.0, "end": 4.2, "speaker": "SPEAKER_1", "text": "..."}]
    segments: Mapped[list | None] = mapped_column(JSONType)
    language: Mapped[str | None] = mapped_column(String(20))
    provider: Mapped[str | None] = mapped_column(String(100))  # which model produced it

    meeting: Mapped[Meeting] = relationship(back_populates="transcript")


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    assignee_email: Mapped[str | None] = mapped_column(String(320))
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")    # open | in_progress | done
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | ai
    # AI-extracted tasks wait for admin approval before members are notified (human-in-the-loop)
    approved: Mapped[bool] = mapped_column(default=True)
    # AI tasks: the transcript quote the task came from, and the owner's name as spoken
    # when it could not be matched to a participant
    evidence: Mapped[str | None] = mapped_column(Text)
    suggested_owner: Mapped[str | None] = mapped_column(String(120))
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # low | normal | high | urgent
    # Reminder emails already sent (so each is sent once)
    due_reminder_sent_on: Mapped[date | None] = mapped_column(Date)
    overdue_reminder_sent_on: Mapped[date | None] = mapped_column(Date)

    meeting: Mapped[Meeting] = relationship(back_populates="tasks")
    assignee: Mapped[User | None] = relationship()


class MeetingNote(TimestampMixin, Base):
    """Notes typed during or after a meeting. `offset_seconds` = position in the recording (live notes)."""
    __tablename__ = "meeting_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20), default="note")  # note | bookmark (an important moment)
    text: Mapped[str] = mapped_column(Text)
    offset_seconds: Mapped[float | None] = mapped_column()

    meeting: Mapped[Meeting] = relationship(back_populates="notes")
    author: Mapped[User | None] = relationship()


class Document(TimestampMixin, Base):
    """Files attached to a meeting (specs, documentation, bug reports) that the AI can answer from."""
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | ready | failed
    error: Mapped[str | None] = mapped_column(Text)
    # Extracted text, one entry per page (or one for plain text files)
    pages: Mapped[list | None] = mapped_column(JSONType)

    meeting: Mapped[Meeting] = relationship(back_populates="documents")
    uploaded_by: Mapped[User | None] = relationship()


class AgentRun(TimestampMixin, Base):
    """One instruction to the task assistant: the plan it proposed and what happened (audit log)."""
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed | done | cancelled | failed
    plan: Mapped[dict | None] = mapped_column(JSONType)
    result: Mapped[dict | None] = mapped_column(JSONType)
    ai_model: Mapped[str | None] = mapped_column(String(120))
