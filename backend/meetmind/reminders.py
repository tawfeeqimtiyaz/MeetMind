"""Daily reminder emails (run by the scheduler through the worker).

- Owners: tasks due today/tomorrow (once per deadline) and overdue tasks (again every 3 days until done).
- Admins: "what's important today": meetings waiting for review, failed processing, overdue and urgent
  tasks, starred recordings and storage used. Only sent when something needs attention.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from . import notifications
from .config import Settings
from .models import Meeting, Organization, Recording, Task, User

log = logging.getLogger(__name__)

OVERDUE_REPEAT_DAYS = 3


def _fmt(d: date | None) -> str:
    return d.strftime("%a %d %b") if d else "no date"


def send_task_reminders(session_factory: sessionmaker, settings: Settings, today: date) -> int:
    """Email each owner about their due-soon and overdue tasks. Returns the number of emails."""
    with session_factory() as db:
        tasks = db.scalars(
            select(Task).join(User, Task.assignee_id == User.id)
            .where(Task.approved.is_(True), Task.status != "done", Task.due_date.is_not(None), User.status == "active")
        ).all()
        by_user: dict = defaultdict(lambda: {"overdue": [], "due_soon": [], "tasks": []})
        for t in tasks:
            bucket = by_user[t.assignee]
            item = {"title": t.title, "due": _fmt(t.due_date), "priority": t.priority, "meeting": t.meeting.title}
            if t.due_date < today:
                last = t.overdue_reminder_sent_on
                if last is None or (today - last).days >= OVERDUE_REPEAT_DAYS:
                    bucket["overdue"].append(item)
                    bucket["tasks"].append((t, "overdue"))
            elif t.due_date <= today + timedelta(days=1) and t.due_reminder_sent_on is None:
                bucket["due_soon"].append(item)
                bucket["tasks"].append((t, "due"))
        sent = 0
        for user, b in by_user.items():
            if not (b["overdue"] or b["due_soon"]):
                continue
            if notifications.send_task_reminder(settings, user, b["overdue"], b["due_soon"]):
                sent += 1
                for t, kind in b["tasks"]:
                    if kind == "overdue":
                        t.overdue_reminder_sent_on = today
                    else:
                        t.due_reminder_sent_on = today
        db.commit()
    log.info("Task reminders: %d email(s)", sent)
    return sent


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def send_admin_digests(session_factory: sessionmaker, settings: Settings, today: date) -> int:
    """One summary per admin of each organization, only when something needs attention."""
    base = settings.app_base_url
    sent = 0
    with session_factory() as db:
        for org in db.scalars(select(Organization)).all():
            meeting_url = lambda m: f"{base}/meeting.html?id={m.id}"  # noqa: E731
            meetings = select(Meeting).where(Meeting.organization_id == org.id)
            review = [{"title": m.title, "detail": f"{sum(1 for t in m.tasks if not t.approved)} AI-drafted task(s) to approve",
                       "url": meeting_url(m)} for m in db.scalars(meetings.where(Meeting.status == "review"))]
            failed = [{"title": m.title, "detail": (m.recordings[-1].error if m.recordings else None) or "Processing failed",
                       "url": meeting_url(m)} for m in db.scalars(meetings.where(Meeting.status == "failed"))]
            open_tasks = select(Task).where(Task.organization_id == org.id, Task.approved.is_(True), Task.status != "done")

            def owner(t):
                return (t.assignee.name or t.assignee.email) if t.assignee else (t.assignee_email or "unassigned")

            overdue = [{"title": t.title, "detail": f"{owner(t)} · was due {_fmt(t.due_date)} · {t.meeting.title}",
                        "url": meeting_url(t.meeting)}
                       for t in db.scalars(open_tasks.where(Task.due_date < today).order_by(Task.due_date))]
            urgent = [{"title": t.title, "detail": f"{owner(t)} · due {_fmt(t.due_date)} · {t.meeting.title}",
                       "url": meeting_url(t.meeting)}
                      for t in db.scalars(open_tasks.where(Task.priority == "urgent",
                                                           (Task.due_date >= today) | Task.due_date.is_(None)))]
            recordings = select(Recording).join(Meeting).where(Meeting.organization_id == org.id)
            important = [{"title": r.meeting.title, "detail": f"{r.original_filename} · {_human_size(r.size_bytes)}",
                          "url": meeting_url(r.meeting)} for r in db.scalars(recordings.where(Recording.important.is_(True)))]
            count, total = db.execute(
                select(func.count(Recording.id), func.coalesce(func.sum(Recording.size_bytes), 0))
                .join(Meeting).where(Meeting.organization_id == org.id)).one()

            if not (review or failed or overdue or urgent):
                continue  # nothing needs attention: no email
            admins = db.scalars(select(User).where(User.organization_id == org.id, User.role == "admin",
                                                   User.status == "active")).all()
            for admin in admins:
                if notifications.send_admin_digest(
                    settings, admin, org.name, review=review[:15], failed=failed[:15], overdue=overdue[:25],
                    urgent=urgent[:25], important=important[:15], recordings_count=count, storage=_human_size(int(total)),
                ):
                    sent += 1
    log.info("Admin digests: %d email(s)", sent)
    return sent


def rescue_abandoned_recordings(session_factory: sessionmaker, settings: Settings, max_idle_hours: int = 6) -> int:
    """Live recordings that stopped receiving pieces (closed laptop, crash) are processed with what arrived."""
    from .task_queue import enqueue

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_idle_hours)
    rescued = 0
    with session_factory() as db:
        for rec in db.scalars(select(Recording).where(Recording.status == "recording", Recording.updated_at < cutoff)):
            folder = Path(settings.upload_dir) / Path(rec.storage_path).parent / f"live-{rec.id}"
            pieces = sorted(folder.glob("*.part")) if folder.exists() else []
            contiguous = 0
            while contiguous < len(pieces) and pieces[contiguous].name == f"{contiguous:06d}.part":
                contiguous += 1
            for extra in pieces[contiguous:]:  # a gap means later pieces can't be joined reliably
                extra.unlink(missing_ok=True)
            if contiguous == 0:
                rec.status, rec.error = "failed", "The recording was interrupted before any audio arrived."
                rec.meeting.status = "failed"
                db.commit()
                continue
            rec.size_bytes = sum(p.stat().st_size for p in pieces[:contiguous])
            rec.status, rec.error = "queued", None
            rec.meeting.status = "processing"
            db.commit()
            enqueue(settings, "meetmind.jobs.process_recording", str(rec.id))
            rescued += 1
            log.info("Rescued interrupted recording %s (%d pieces)", rec.id, contiguous)
    return rescued
