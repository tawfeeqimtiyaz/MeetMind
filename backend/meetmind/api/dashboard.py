"""Numbers for the dashboard (one request instead of many): stat cards, what needs attention,
tasks by status, workload per person and upcoming meetings."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from ..deps import CurrentUser, DbSession
from ..models import Meeting, Recording, Task
from ..serializers import meeting_summary_out
from .common import visible_meetings


router = APIRouter()

ATTENTION_LIMIT = 5


def _count(db, query) -> int:
    return db.scalar(select(func.count()).select_from(query.subquery())) or 0


def _upcoming(db, user, now: datetime) -> list[dict]:
    q = (visible_meetings(user)
         .where(Meeting.scheduled_at >= now, Meeting.scheduled_at < now + timedelta(days=7), Meeting.status == "scheduled")
         .order_by(Meeting.scheduled_at).limit(5))
    return [meeting_summary_out(m) for m in db.scalars(q)]


def _attention(db, user) -> list[dict]:
    """What an admin should look at now, most important first (same ideas as the daily digest email)."""
    items: list[dict] = []
    org = user.organization_id

    def meetings(*where):
        return db.scalars(visible_meetings(user).where(*where).order_by(Meeting.scheduled_at.desc()).limit(ATTENTION_LIMIT))

    for m in meetings(Meeting.recordings.any(Recording.status == "recording")):
        items.append({"kind": "live", "meeting_id": str(m.id), "title": m.title,
                      "detail": "Live recording open: finish it or process what was saved"})
    for m in meetings(Meeting.status == "failed"):
        items.append({"kind": "failed", "meeting_id": str(m.id), "title": m.title, "detail": "Processing failed: retry from the meeting"})
    for m in meetings(Meeting.status == "review"):
        n = sum(1 for t in m.tasks if not t.approved)
        items.append({"kind": "review", "meeting_id": str(m.id), "title": m.title,
                      "detail": f"{n} AI-drafted task{'s' if n != 1 else ''} to review"})
    since = datetime.now(timezone.utc) - timedelta(days=14)
    starred = db.scalars(
        select(Recording).join(Meeting).where(Meeting.organization_id == org, Recording.important.is_(True),
                                              Recording.created_at >= since)
        .order_by(Recording.created_at.desc()).limit(ATTENTION_LIMIT))
    for r in starred:
        items.append({"kind": "starred", "meeting_id": str(r.meeting_id), "title": r.meeting.title,
                      "detail": f"Starred recording: {r.original_filename}"})
    return items


@router.get("")
def dashboard(user: CurrentUser, db: DbSession):
    now = datetime.now(timezone.utc)
    today = date.today()

    if user.role == "admin":
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        org_tasks = select(Task).where(Task.organization_id == user.organization_id, Task.approved.is_(True))
        open_tasks = org_tasks.where(Task.status != "done")

        by_status = Counter(dict(db.execute(
            select(Task.status, func.count()).where(Task.organization_id == user.organization_id, Task.approved.is_(True))
            .group_by(Task.status)).all()))

        workload: dict[str, dict] = {}
        for t in db.scalars(open_tasks):
            email = t.assignee.email if t.assignee else t.assignee_email
            key = email or ""
            w = workload.setdefault(key, {"name": (t.assignee.name if t.assignee else None) or email or "Unassigned",
                                          "email": email, "open": 0, "overdue": 0, "urgent": 0})
            w["open"] += 1
            w["overdue"] += bool(t.due_date and t.due_date < today)
            w["urgent"] += t.priority == "urgent"

        return {"role": "admin", "stats": {
            "meetings_this_month": _count(db, visible_meetings(user).where(
                Meeting.scheduled_at >= month_start, Meeting.scheduled_at < next_month)),
            "awaiting_review": _count(db, visible_meetings(user).where(Meeting.status == "review")),
            "open_tasks": _count(db, open_tasks),
            "done_tasks": _count(db, org_tasks.where(Task.status == "done")),
        }, "insights": {
            "overdue_tasks": _count(db, open_tasks.where(Task.due_date < today)),
            "urgent_tasks": _count(db, open_tasks.where(Task.priority == "urgent")),
            "tasks_by_status": {s: by_status.get(s, 0) for s in ("open", "in_progress", "done")},
            "workload": sorted(workload.values(), key=lambda w: (-w["overdue"], -w["open"], w["name"]))[:8],
            "attention": _attention(db, user),
            "upcoming": _upcoming(db, user, now),
        }}

    mine = select(Task).where(Task.assignee_id == user.id, Task.approved.is_(True), Task.status != "done")
    week_end = today + timedelta(days=7)
    return {"role": "member", "stats": {
        "open_tasks": _count(db, mine),
        "due_this_week": _count(db, mine.where(Task.due_date.is_not(None), Task.due_date <= week_end)),
        "upcoming_meetings": _count(db, visible_meetings(user).where(Meeting.scheduled_at >= now)),
    }, "insights": {
        "overdue_tasks": _count(db, mine.where(Task.due_date < today)),
        "upcoming": _upcoming(db, user, now),
    }}
