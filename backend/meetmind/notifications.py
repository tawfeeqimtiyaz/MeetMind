"""Outgoing notifications (email).

Flow: render the email here -> queue it on Redis -> the worker sends it over SMTP
(jobs.send_email), retrying 3 times. A slow or broken mail server never slows down
or breaks the API or the AI pipeline: notification failures are logged, not raised.

Emails:
  invite             -> the invited person
  review_ready       -> the meeting's admin, when AI drafted tasks to approve
  tasks_assigned     -> each person whose tasks were approved
  processing_failed  -> the meeting's admin
"""
import logging
from collections import defaultdict

from sqlalchemy import select

from . import mailer, task_queue
from .config import Settings

log = logging.getLogger(__name__)


def deliver(settings: Settings, to: str, subject: str, html: str, text: str) -> None:
    """Queue one rendered email for the worker. (Tests replace this to capture emails.)"""
    from rq import Retry

    task_queue.enqueue(settings, "meetmind.jobs.send_email", to, subject, html, text,
                       retry=Retry(max=3, interval=[10, 60, 300]))


def _send(settings: Settings, to: str, subject: str, template: str, **context) -> bool:
    if not settings.email_enabled:
        return False
    try:
        html, text = mailer.render(template, subject=subject, app_url=settings.app_base_url, **context)
        deliver(settings, to, subject, html, text)
        return True
    except Exception:  # noqa: BLE001 - email is best-effort
        log.exception("Could not queue %s email to %s", template, to)
        return False


def _meeting_url(settings: Settings, meeting) -> str:
    return f"{settings.app_base_url}/meeting.html?id={meeting.id}"


def _admin_recipients(db, meeting) -> list:
    """The admin who created the meeting; if they're gone, every active admin of the org."""
    from .models import User

    creator = db.get(User, meeting.created_by_id) if meeting.created_by_id else None
    if creator and creator.status == "active" and creator.role == "admin":
        return [creator]
    return list(db.scalars(select(User).where(
        User.organization_id == meeting.organization_id, User.role == "admin", User.status == "active",
    )))


# ---------- the emails ----------

def send_invite(user, raw_token: str, inviter, settings: Settings) -> bool:
    url = f"{settings.app_base_url}/accept-invite.html?token={raw_token}"
    if not settings.is_production:
        log.info("[dev] Invite link for %s: %s", user.email, url)
    org = user.organization.name if user.organization else "your team"
    return _send(
        settings, user.email, f"{inviter.name or inviter.email} invited you to {org} on MeetMind", "invite",
        name=user.name, inviter=inviter.name or inviter.email, organization=org, role=user.role,
        accept_url=url, expires_hours=settings.invite_expires_hours,
    )


def notify_review_ready(settings: Settings, db, meeting, draft_count: int) -> None:
    for admin in _admin_recipients(db, meeting):
        _send(
            settings, admin.email, f"Ready for review: {meeting.title}", "review_ready",
            name=admin.name, meeting_title=meeting.title, summary=meeting.summary or "",
            draft_count=draft_count, meeting_url=_meeting_url(settings, meeting),
        )


def notify_tasks_assigned(settings: Settings, meeting, tasks) -> None:
    """One email per person, listing their newly approved tasks."""
    by_user = defaultdict(list)
    for t in tasks:
        if t.assignee is not None and t.assignee.status == "active":
            by_user[t.assignee].append(t)
    for user, items in by_user.items():
        n = len(items)
        _send(
            settings, user.email, f"{n} new task{'s' if n != 1 else ''} from {meeting.title}", "tasks_assigned",
            name=user.name, meeting_title=meeting.title, meeting_url=_meeting_url(settings, meeting),
            tasks=[{"title": t.title, "due": t.due_date.strftime("%a %d %b %Y") if t.due_date else None,
                    "evidence": t.evidence} for t in items],
            tasks_url=f"{settings.app_base_url}/member.html",
        )


def notify_processing_failed(settings: Settings, db, meeting, error: str) -> None:
    for admin in _admin_recipients(db, meeting):
        _send(
            settings, admin.email, f"Processing failed: {meeting.title}", "processing_failed",
            name=admin.name, meeting_title=meeting.title, error=error, meeting_url=_meeting_url(settings, meeting),
        )
