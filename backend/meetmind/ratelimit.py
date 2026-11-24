"""Rate limiting as a FastAPI dependency, backed by Redis (shared across all API workers).

Usage:  @router.post("/login", dependencies=[rate_limit("10/minute", "login")])
Limits are counted per client IP. Uvicorn runs with --proxy-headers, so behind nginx
`request.client.host` is the real visitor IP, not nginx's.
"""
import logging
import time

from fastapi import Depends, Request
from limits import RateLimitItem, parse
from limits.storage import storage_from_string
from limits.strategies import MovingWindowRateLimiter

from .errors import APIError

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, storage_uri: str, enabled: bool = True):
        self.enabled = enabled
        self._strategy = MovingWindowRateLimiter(storage_from_string(storage_uri)) if enabled else None  # sliding window: no burst at minute boundaries

    def check(self, item: RateLimitItem, scope: str, key: str) -> int | None:
        """Count one hit. Returns None if allowed, else seconds until the window resets."""
        if not self.enabled:
            return None
        try:
            if self._strategy.hit(item, scope, key):
                return None
            reset_at = self._strategy.get_window_stats(item, scope, key).reset_time
            return max(1, int(reset_at - time.time()))
        except Exception:  # noqa: BLE001
            # Fail open: a Redis hiccup should not take the whole API down
            log.warning("Rate limit storage unavailable; allowing request", exc_info=True)
            return None


def rate_limit(limit: str, scope: str):
    """Dependency factory, e.g. rate_limit("5/hour", "signup")."""
    item = parse(limit)

    def dependency(request: Request) -> None:
        key = request.client.host if request.client else "unknown"
        retry_after = request.app.state.rate_limiter.check(item, scope, key)
        if retry_after is not None:
            raise APIError(
                429, "rate_limited", "Too many requests, please try again later",
                headers={"Retry-After": str(retry_after)},
            )

    return Depends(dependency)
