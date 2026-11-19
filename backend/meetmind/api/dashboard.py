"""Numbers for the dashboard stat cards (one request instead of many)."""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from ..deps import CurrentUser, DbSession
from ..models import Meeting, Task
from .common import visible_meetings


router = APIRouter()


def _count(db, query) -> int:
    return db.scalar(select(func.count()).select_from(query.subquery())) or 0


@router.get("")
def dashboard(user: CurrentUser, db: DbSession):
    now = datetime.now(timezone.utc)

    if user.role == "admin":
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        org_tasks = select(Task).where(Task.organization_id == user.organization_id, Task.approved.is_(True))
        return {"role": "admin", "stats": {
            "meetings_this_month": _count(db, visible_meetings(user).where(
                Meeting.scheduled_at >= month_start, Meeting.scheduled_at < next_month)),
            "awaiting_review": _count(db, visible_meetings(user).where(Meeting.status == "review")),
            "open_tasks": _count(db, org_tasks.where(Task.status != "done")),
            "done_tasks": _count(db, org_tasks.where(Task.status == "done")),
        }}

    mine = select(Task).where(Task.assignee_id == user.id, Task.approved.is_(True), Task.status != "done")
    week_end = date.today() + timedelta(days=7)
    return {"role": "member", "stats": {
        "open_tasks": _count(db, mine),
        "due_this_week": _count(db, mine.where(Task.due_date.is_not(None), Task.due_date <= week_end)),
        "upcoming_meetings": _count(db, visible_meetings(user).where(Meeting.scheduled_at >= now)),
    }}
