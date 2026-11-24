"""Tasks across meetings: 'My tasks' and updates."""
import uuid

from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import AdminUser, CurrentUser, DbSession
from ..errors import APIError
from ..models import Task, User
from ..schemas import TaskPatch, TaskStatus
from ..serializers import task_out
from .common import org_user_by_email

router = APIRouter()

MEMBER_EDITABLE = {"status"}


def _get_task(db: Session, user: User, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.organization_id != user.organization_id:
        raise APIError(404, "not_found", "Task not found")
    return task


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
