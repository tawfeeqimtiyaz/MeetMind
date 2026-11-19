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
