"""MeetMind backend — FastAPI application factory."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings

__version__ = "1.0.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.app_env != "test":
        settings.check_secure()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from . import models  # noqa: F401  (register tables on Base.metadata)
    from .api import auth, dashboard, health, meetings, tasks, users
    from .db import make_engine, make_session_factory
    from .errors import register_error_handlers
    from .ratelimit import RateLimiter, rate_limit

    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        engine.dispose()  # close pooled DB connections on shutdown

    # Interactive API docs at /api/docs (hidden in production)
    show_docs = not settings.is_production
    app = FastAPI(
        title="MeetMind API",
        version=__version__,
        description="Meetings → transcripts, AI summaries, tasks and Q&A.",
        docs_url="/api/docs" if show_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if show_docs else None,
        lifespan=lifespan,
    )

    # Shared objects, reachable from dependencies via request.app.state
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.rate_limiter = RateLimiter(
        settings.ratelimit_storage_uri or settings.redis_url, enabled=settings.ratelimit_enabled,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    # Every API route gets a default per-IP limit; health probes are exempt
    default_limit = [rate_limit("300/minute", "global")]
    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"], dependencies=default_limit)
    app.include_router(users.router, prefix="/api/users", tags=["users"], dependencies=default_limit)
    app.include_router(meetings.router, prefix="/api/meetings", tags=["meetings"], dependencies=default_limit)
    app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"], dependencies=default_limit)
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"], dependencies=default_limit)

    return app
