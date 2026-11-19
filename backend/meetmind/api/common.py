"""Helpers shared by the API routers."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import APIError
from ..models import Meeting, MeetingParticipant, User


def visible_meetings(user: User):
    """Admins see every meeting in their organization; members only meetings they take part in."""
    q = select(Meeting).where(Meeting.organization_id == user.organization_id)
    if user.role != "admin":
        q = q.where(Meeting.participants.any(MeetingParticipant.user_id == user.id))
    return q


def get_meeting_for(db: Session, user: User, meeting_id) -> Meeting:
    meeting = db.scalar(visible_meetings(user).where(Meeting.id == meeting_id))
    if meeting is None:
        # 404 (not 403) so other organizations' meeting IDs are not revealed
        raise APIError(404, "not_found", "Meeting not found")
    return meeting


def org_user_by_email(db: Session, organization_id, email: str | None) -> User | None:
    if not email:
        return None
    return db.scalar(select(User).where(User.organization_id == organization_id, User.email == email))
