"""Background jobs run by the RQ worker (`worker` container)."""
import logging
from functools import lru_cache

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _deps():
    """Jobs run outside the web app, so they build their own settings + DB connection once per worker."""
    from .ai import default_deps
    from .config import Settings
    from .db import make_engine, make_session_factory

    # RQ only configures its own logger; show MeetMind's INFO logs (pipeline decisions) in `docker compose logs`
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("rq").propagate = False  # RQ prints its own lines already

    settings = Settings()
    return default_deps(settings, make_session_factory(make_engine(settings.database_url)))


def send_email(to: str, subject: str, html: str, text: str) -> None:
    """Deliver one email over SMTP (queued by notifications.py; RQ retries on failure)."""
    from .mailer import send_now

    send_now(_deps().settings, to, subject, html, text)
    log.info("Email sent to %s: %s", to, subject)


def index_meeting(meeting_id: str) -> None:
    """(Re)build one meeting's search index entries. Raises so RQ retries on failure."""
    import uuid

    from .models import Meeting

    deps = _deps()
    with deps.session_factory() as db:
        meeting = db.get(Meeting, uuid.UUID(meeting_id))
        if meeting is None:
            deps.rag().remove_meeting(meeting_id)
            return
        deps.rag().index_meeting(meeting)


def process_document(document_id: str) -> None:
    """Extract an attached document's text and add it to the search index."""
    from .documents import process_document as run

    deps = _deps()
    try:
        run(deps.session_factory, deps.settings, deps.rag(), document_id)
    except Exception:  # noqa: BLE001 - the text is saved; only indexing failed: retry it
        log.warning("Indexing document %s failed; retrying in the background", document_id, exc_info=True)
        from rq import Retry

        from .task_queue import enqueue

        enqueue(deps.settings, "meetmind.jobs.index_document", document_id,
                retry=Retry(max=5, interval=[30, 120, 600, 1800, 3600]))


def process_slide(slide_id: str) -> None:
    """A photo of the big screen: shrink it, let a vision model read it, then refresh the meeting's search."""
    import uuid

    from .models import Slide
    from .slides import process_photo

    deps = _deps()
    status = process_photo(deps.session_factory, deps.settings, deps.read_image, slide_id)
    log.info("Slide %s: %s", slide_id, status)
    if status == "ready":
        with deps.session_factory() as db:
            slide = db.get(Slide, uuid.UUID(slide_id))
            if slide is not None:
                deps.rag().index_meeting(slide.meeting)


def index_document(document_id: str) -> None:
    import uuid

    from .models import Document

    deps = _deps()
    with deps.session_factory() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if doc is None:
            deps.rag().remove_document(document_id)
        elif doc.status == "ready":
            deps.rag().index_document(doc.meeting, doc)


def send_daily_reminders(day: str | None = None) -> None:
    """Task reminders for owners + the admin digest (queued daily by the scheduler)."""
    from datetime import date

    from .reminders import send_admin_digests, send_task_reminders

    deps = _deps()
    today = date.fromisoformat(day) if day else date.today()
    send_task_reminders(deps.session_factory, deps.settings, today)
    send_admin_digests(deps.session_factory, deps.settings, today)


def rescue_recordings() -> None:
    from .reminders import rescue_abandoned_recordings

    deps = _deps()
    rescue_abandoned_recordings(deps.session_factory, deps.settings)


def reindex_all() -> int:
    """Index every processed meeting, e.g. after switching the embedding model.
    Run: docker compose exec worker python -m meetmind.jobs reindex"""
    from sqlalchemy import select

    from .models import Meeting, Transcript

    deps = _deps()
    with deps.session_factory() as db:
        ids = [str(i) for i in db.scalars(select(Meeting.id).join(Transcript))]
    done = 0
    for meeting_id in ids:
        try:
            index_meeting(meeting_id)
            done += 1
        except Exception:  # noqa: BLE001
            log.exception("Indexing meeting %s failed", meeting_id)
    with deps.session_factory() as db:
        from .models import Document

        doc_ids = [str(i) for i in db.scalars(select(Document.id).where(Document.status == "ready"))]
    for document_id in doc_ids:
        try:
            index_document(document_id)
        except Exception:  # noqa: BLE001
            log.exception("Indexing document %s failed", document_id)
    log.info("Re-indexed %d of %d meetings and %d documents", done, len(ids), len(doc_ids))
    return done


def process_recording(recording_id: str) -> None:
    """Transcribe + analyse a recording with the LangGraph pipeline (pauses for admin review)."""
    from .ai.pipeline import mark_failed, run_pipeline, user_message

    deps = _deps()
    try:
        thread_id = run_pipeline(deps, recording_id)
        log.info("Recording %s processed (thread %s)", recording_id, thread_id)
    except Exception as exc:
        log.exception("Processing recording %s failed", recording_id)
        mark_failed(deps, recording_id, user_message(exc))
        raise  # also keep the job in RQ's failed registry for inspection


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["reindex"]:
        print(f"Re-indexed {reindex_all()} meetings")
    else:
        print("usage: python -m meetmind.jobs reindex")
