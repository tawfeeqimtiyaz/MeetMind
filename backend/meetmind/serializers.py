"""Model -> JSON. Kept in one place so the API shape is consistent."""
from .models import Meeting, Recording, Task, User


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
        "created_at": _iso(r.created_at),
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


def meeting_out(m: Meeting, tasks: list[Task]) -> dict:
    """Full form for the meeting page. `tasks` is pre-filtered for the viewer."""
    return {
        **meeting_summary_out(m),
        "agenda": m.agenda,
        "summary": m.summary,
        "strategy_shifts": m.strategy_shifts or [],
        "open_questions": m.open_questions or [],
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
