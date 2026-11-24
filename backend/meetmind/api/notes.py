"""Notes and important-moment markers, typed during or after a meeting by anyone who attends it.

Notes are given to the AI together with the transcript (they can add names, decisions or tasks the
audio missed) and are searchable in Ask MeetMind.
"""
import logging
import uuid

from fastapi import APIRouter, Response
from sqlalchemy import select

from .. import task_queue
from ..deps import CurrentUser, DbSession, SettingsDep
from ..errors import APIError
from ..models import MeetingNote
from ..schemas import NoteIn
from ..serializers import note_out
from .common import get_meeting_for

router = APIRouter()
log = logging.getLogger(__name__)


def _reindex_later(settings, meeting) -> None:
    """Notes added after processing should become searchable too (best effort)."""
    if meeting.transcript is None:
        return  # indexed together with the transcript when the recording is processed
    try:
        task_queue.enqueue(settings, "meetmind.jobs.index_meeting", str(meeting.id))
    except Exception:  # noqa: BLE001
        log.warning("Could not queue re-indexing of meeting %s", meeting.id)


@router.get("/meetings/{meeting_id}/notes")
def list_notes(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession):
    meeting = get_meeting_for(db, user, meeting_id)
    return {"items": [note_out(n) for n in meeting.notes]}


@router.post("/meetings/{meeting_id}/notes", status_code=201)
def add_note(meeting_id: uuid.UUID, data: NoteIn, user: CurrentUser, db: DbSession, settings: SettingsDep):
    meeting = get_meeting_for(db, user, meeting_id)
    note = MeetingNote(meeting=meeting, author_id=user.id, kind=data.kind, text=data.text,
                       offset_seconds=data.offset_seconds)
    db.add(note)
    db.commit()
    db.refresh(note)
    _reindex_later(settings, meeting)
    return {"note": note_out(note)}


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: SettingsDep):
    note = db.scalar(select(MeetingNote).where(MeetingNote.id == note_id))
    if note is None:
        raise APIError(404, "not_found", "Note not found")
    meeting = get_meeting_for(db, user, note.meeting_id)
    if note.author_id != user.id and user.role != "admin":
        raise APIError(403, "forbidden", "You can only delete your own notes")
    db.delete(note)
    db.commit()
    _reindex_later(settings, meeting)
    return Response(status_code=204)
