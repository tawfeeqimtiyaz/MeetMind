"""Tasks across meetings: 'My tasks' and updates."""
import uuid
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import AdminUser, CurrentUser, DbSession
from ..errors import APIError
from ..models import Task, User
from ..schemas import TaskPatch, TaskPriority, TaskStatus
from ..serializers import task_out
from .common import org_user_by_email

router = APIRouter()

MEMBER_EDITABLE = {"status"}


def _get_task(db: Session, user: User, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.organization_id != user.organization_id:
        raise APIError(404, "not_found", "Task not found")
    return task


BOARD_LIMIT = 500


@router.get("")
def list_tasks(
    user: CurrentUser,
    db: DbSession,
    status: TaskStatus | None = None,
    priority: TaskPriority | None = None,
    assignee: str | None = Query(None, max_length=320, description="Owner email, or 'none' for unassigned (admins)"),
    due: Literal["overdue", "week", "none"] | None = None,
    q: str | None = Query(None, max_length=200, description="Words in the title"),
):
    """Tasks board: admins see every approved task in the organization, members only their own."""
    query = (select(Task).options(selectinload(Task.meeting), selectinload(Task.assignee))
             .where(Task.organization_id == user.organization_id, Task.approved.is_(True)))
    if user.role != "admin":
        query = query.where(Task.assignee_id == user.id)
    elif assignee == "none":
        query = query.where(Task.assignee_id.is_(None), Task.assignee_email.is_(None))
    elif assignee:
        query = query.where(Task.assignee_email == assignee.strip().lower())
    if status:
        query = query.where(Task.status == status)
    if priority:
        query = query.where(Task.priority == priority)
    today = date.today()
    if due == "overdue":
        query = query.where(Task.due_date < today, Task.status != "done")
    elif due == "week":
        query = query.where(Task.due_date >= today, Task.due_date <= today + timedelta(days=7))
    elif due == "none":
        query = query.where(Task.due_date.is_(None))
    if q and q.strip():
        query = query.where(Task.title.ilike("%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
                                             escape="\\"))
    tasks = db.scalars(query.order_by(Task.due_date.is_(None), Task.due_date, Task.created_at.desc()).limit(BOARD_LIMIT)).all()
    return {"items": [task_out(t) for t in tasks], "limited": len(tasks) == BOARD_LIMIT}


@router.get("/mine")
def my_tasks(user: CurrentUser, db: DbSession, status: TaskStatus | None = None):
    q = select(Task).where(Task.assignee_id == user.id, Task.approved.is_(True))
    if status:
        q = q.where(Task.status == status)
    tasks = db.scalars(q.order_by(Task.due_date.is_(None), Task.due_date, Task.created_at.desc())).all()
    return {"items": [task_out(t) for t in tasks]}


@router.patch("/{task_id}")
def update_task(task_id: uuid.UUID, data: TaskPatch, user: CurrentUser, db: DbSession):
    task = _get_task(db, user, task_id)
    changed = data.model_fields_set

    if user.role != "admin":
        if task.assignee_id != user.id or not task.approved:
            raise APIError(404, "not_found", "Task not found")
        if not changed <= MEMBER_EDITABLE:
            raise APIError(403, "forbidden", "Members can only change the status of their own tasks")

    for field in ("title", "description", "due_date", "status", "approved", "priority"):
        if field in changed:
            value = getattr(data, field)
            if value is None and field in {"title", "status", "approved", "priority"}:
                raise APIError(422, "validation_error", f"{field} cannot be empty")
            setattr(task, field, value)
    if "assignee_email" in changed:
        task.assignee_email = data.assignee_email
        task.assignee = org_user_by_email(db, task.organization_id, data.assignee_email)

    db.commit()
    return {"task": task_out(task)}


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: uuid.UUID, admin: AdminUser, db: DbSession):
    task = _get_task(db, admin, task_id)
    db.delete(task)
    db.commit()
    return Response(status_code=204)
