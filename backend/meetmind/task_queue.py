"""Enqueue background jobs on Redis (processed by the `worker` container)."""
from redis import Redis
from rq import Queue

from .config import Settings


def enqueue(settings: Settings, func_path: str, *args, **kwargs):
    queue = Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url))
    return queue.enqueue(func_path, *args, job_timeout=3600, failure_ttl=7 * 24 * 3600, **kwargs)
