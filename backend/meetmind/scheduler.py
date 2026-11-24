"""Tiny scheduler: wakes up every few minutes and queues periodic jobs for the worker.

- daily at REMINDER_HOUR (REMINDER_TIMEZONE): task reminders + admin digest
- hourly: rescue live recordings that were interrupted (e.g. the laptop was closed)

Run: python -m meetmind.scheduler   (the `scheduler` container does this)
A Redis key per day/hour makes sure each job is queued once, even if the scheduler restarts.
"""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from redis import Redis

from . import task_queue
from .config import Settings

log = logging.getLogger("meetmind.scheduler")


def tick(settings: Settings, redis: Redis, now: datetime | None = None) -> list[str]:
    """Queue whatever is due right now. Returns the job names queued (for logs and tests)."""
    try:
        zone = ZoneInfo(settings.reminder_timezone)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    now = (now or datetime.now(zone)).astimezone(zone)
    queued = []
    if now.hour == settings.reminder_hour and redis.set(f"meetmind:sched:daily:{now:%Y-%m-%d}", 1, nx=True, ex=172800):
        task_queue.enqueue(settings, "meetmind.jobs.send_daily_reminders", now.date().isoformat())
        queued.append("send_daily_reminders")
    if redis.set(f"meetmind:sched:hourly:{now:%Y-%m-%d-%H}", 1, nx=True, ex=7200):
        task_queue.enqueue(settings, "meetmind.jobs.rescue_recordings")
        queued.append("rescue_recordings")
    return queued


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    redis = Redis.from_url(settings.redis_url)
    log.info("Scheduler started: daily reminders at %02d:00 %s", settings.reminder_hour, settings.reminder_timezone)
    while True:
        try:
            for job in tick(settings, redis):
                log.info("Queued %s", job)
        except Exception:  # noqa: BLE001 - keep running; Redis may be restarting
            log.exception("Scheduler tick failed")
        time.sleep(300)


if __name__ == "__main__":
    main()
