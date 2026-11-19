"""Liveness and readiness probes (used by Docker now, Kubernetes later)."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/health")
def health():
    """Liveness: the process is up."""
    return {"status": "ok", "service": "meetmind-api"}


@router.get("/health/ready")
def ready(request: Request):
    """Readiness: dependencies (Postgres, Redis) are reachable."""
    checks = {}

    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report any failure
        log.warning("database check failed: %s", exc)
        checks["database"] = "error"

    try:
        Redis.from_url(request.app.state.settings.redis_url, socket_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("redis check failed: %s", exc)
        checks["redis"] = "error"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )
