"""Model -> JSON. Kept in one place so the API shape is consistent."""
from .models import Document, Meeting, MeetingNote, Recording, Slide, Task, User


def _iso(dt):
    return dt.isoformat() if dt else None


def user_out(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "status": u.status,
        "organization": {"id": str(u.organization_id), "name": u.organization.name},
    }


def recording_out(r: Recording) -> dict:
    return {
        "id": str(r.id),
        "original_filename": r.original_filename,
        "size_bytes": r.size_bytes,
        "status": r.status,
        "error": r.error,
        "mode": r.mode,
        "duration_seconds": r.duration_seconds,
        "important": r.important,
        "chunks_received": r.chunks_received,
        "stage": r.stage,
        "content_type": r.content_type,
        "created_at": _iso(r.created_at),
    }


def note_out(n: MeetingNote) -> dict:
    return {
        "id": str(n.id),
        "kind": n.kind,
        "text": n.text,
        "offset_seconds": n.offset_seconds,
        "author": {"id": str(n.author.id), "name": n.author.name, "email": n.author.email} if n.author else None,
        "created_at": _iso(n.created_at),
    }


def document_out(d: Document) -> dict:
    return {
        "id": str(d.id),
        "filename": d.filename,
        "size_bytes": d.size_bytes,
        "content_type": d.content_type,
        "status": d.status,
        "error": d.error,
        "pages": len(d.pages or []),
        "uploaded_by": (d.uploaded_by.name or d.uploaded_by.email) if d.uploaded_by else None,
        "created_at": _iso(d.created_at),
    }


def slide_out(s: Slide, image_token: str | None = None) -> dict:
    """`image_token`: a short-lived link token (an <img> tag can't send the login header)."""
    return {
        "id": str(s.id),
        "source": s.source,
        "offset_seconds": s.offset_seconds,
        "status": s.status,
        "error": s.error,
        "title": s.title,
        "text": s.text,
        "ai_model": s.ai_model,
        "added_by": (s.added_by.name or s.added_by.email) if s.added_by else None,
        "added_by_id": str(s.added_by_id) if s.added_by_id else None,
        "image_url": f"/api/slides/{s.id}/image?token={image_token}" if image_token else None,
        "created_at": _iso(s.created_at),
    }


def task_out(t: Task) -> dict:
    return {
        "id": str(t.id),
        "meeting_id": str(t.meeting_id),
        "meeting_title": t.meeting.title if t.meeting else None,
        "title": t.title,
        "description": t.description,
        "assignee": (
            {"id": str(t.assignee.id), "email": t.assignee.email, "name": t.assignee.name}
            if t.assignee else ({"email": t.assignee_email} if t.assignee_email else None)
        ),
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "status": t.status,
        "source": t.source,
        "approved": t.approved,
        "evidence": t.evidence,
        "priority": t.priority,
        "suggested_owner": t.suggested_owner,
        "created_at": _iso(t.created_at),
    }


def meeting_summary_out(m: Meeting) -> dict:
    """Short form for lists."""
    return {
        "id": str(m.id),
        "title": m.title,
        "scheduled_at": _iso(m.scheduled_at),
        "timezone": m.timezone,
        "status": m.status,
        "participant_count": len(m.participants),
        "draft_task_count": sum(1 for t in m.tasks if t.source == "ai" and not t.approved),
    }


def meeting_out(m: Meeting, tasks: list[Task], slide_token: str | None = None) -> dict:
    """Full form for the meeting page. `tasks` is pre-filtered for the viewer; `slide_token` signs slide images."""
    return {
        **meeting_summary_out(m),
        "agenda": m.agenda,
        "summary": m.summary,
        "strategy_shifts": m.strategy_shifts or [],
        "open_questions": m.open_questions or [],
        "follow_ups": m.follow_ups or [],
        "notes": [note_out(n) for n in m.notes],
        "documents": [document_out(d) for d in m.documents],
        "slides": [slide_out(s, slide_token) for s in sorted(
            m.slides, key=lambda s: (s.offset_seconds is None, s.offset_seconds or 0, s.created_at))],
        "ai_model": m.ai_model,
        "participants": [
            {"email": p.email, "name": p.user.name if p.user else None, "is_user": p.user_id is not None}
            for p in m.participants
        ],
        "recordings": [recording_out(r) for r in m.recordings],
        "has_transcript": m.transcript is not None,
        "tasks": [task_out(t) for t in tasks],
        "created_at": _iso(m.created_at),
    }
