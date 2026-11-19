"""Team management (admin only): list, invite, change role, disable."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import notifications
from ..deps import AdminUser, DbSession, SettingsDep
from ..errors import APIError
from ..models import Meeting, MeetingParticipant, Task, User
from ..schemas import InviteIn, UserPatch
from ..security import new_invite_token
from ..serializers import user_out

router = APIRouter()


def link_user_to_existing_records(db: Session, user: User) -> None:
    """Attach a newly invited user to meetings/tasks that already list their email."""
    org_meetings = select(Meeting.id).where(Meeting.organization_id == user.organization_id)
    db.execute(
        update(MeetingParticipant)
        .where(MeetingParticipant.email == user.email, MeetingParticipant.meeting_id.in_(org_meetings))
        .values(user_id=user.id)
    )
    db.execute(
        update(Task)
        .where(Task.organization_id == user.organization_id, Task.assignee_email == user.email)
        .values(assignee_id=user.id)
    )


@router.get("")
def list_users(admin: AdminUser, db: DbSession):
    users = db.scalars(
        select(User).where(User.organization_id == admin.organization_id).order_by(User.email)
    ).all()
    return {"items": [user_out(u) for u in users]}


@router.post("/invite", status_code=201)
def invite(data: InviteIn, admin: AdminUser, db: DbSession, settings: SettingsDep):
    if db.scalar(select(User).where(User.email == data.email)):
        raise APIError(409, "email_taken", "A user with this email already exists")

    raw_token, token_hash = new_invite_token()
    user = User(
        organization_id=admin.organization_id,
        email=data.email,
        name=data.name,
        role=data.role,
        status="invited",
        invite_token_hash=token_hash,
        invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.invite_expires_hours),
    )
    db.add(user)
    db.flush()
    link_user_to_existing_records(db, user)
    db.commit()

    notifications.send_invite(user, raw_token, inviter=admin, settings=settings)
    return {"user": user_out(user)}


@router.post("/{user_id}/resend-invite")
def resend_invite(user_id: uuid.UUID, admin: AdminUser, db: DbSession, settings: SettingsDep):
    """Send a fresh invite link (the old link stops working)."""
    user = db.get(User, user_id)
    if user is None or user.organization_id != admin.organization_id:
        raise APIError(404, "not_found", "User not found")
    if user.status != "invited":
        raise APIError(409, "not_invited", "This user has already joined")
    raw_token, user.invite_token_hash = new_invite_token()
    user.invite_expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.invite_expires_hours)
    db.commit()
    notifications.send_invite(user, raw_token, inviter=admin, settings=settings)
    return {"user": user_out(user)}


@router.patch("/{user_id}")
def update_user(user_id: uuid.UUID, data: UserPatch, admin: AdminUser, db: DbSession):
    user = db.get(User, user_id)
    if user is None or user.organization_id != admin.organization_id:
        raise APIError(404, "not_found", "User not found")
    if user.id == admin.id:
        raise APIError(400, "cannot_modify_self", "You cannot change your own role or status")

    if data.role is not None:
        user.role = data.role
    if data.status is not None:
        if user.status == "invited" and data.status == "active":
            raise APIError(400, "invite_pending", "Invited users become active by accepting their invite")
        user.status = data.status
    db.commit()
    return {"user": user_out(user)}
