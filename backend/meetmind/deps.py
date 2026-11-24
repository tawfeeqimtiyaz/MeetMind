"""FastAPI dependencies: settings, DB session, current user, admin-only guard.

Endpoints declare what they need in their signature, e.g.

    def create_meeting(data: MeetingIn, user: AdminUser, db: DbSession): ...

and FastAPI builds those values for every request.
"""
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import Settings
from .errors import APIError
from .models import User
from .security import decode_access_token


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    """One session per request; rolled back on errors and always closed."""
    session = request.app.state.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_rag(request: Request):
    """The RAG service (tests put an in-memory one on app.state.rag)."""
    service = getattr(request.app.state, "rag", None)
    if service is None:
        from .ai.rag import get_service

        service = get_service(request.app.state.settings)
    return service


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]

# auto_error=False so a missing token gives our own 401 JSON (and adds the "Authorize" button in /api/docs)
_bearer = HTTPBearer(auto_error=False, description="JWT from /api/auth/login")


def get_current_user(
    db: DbSession,
    settings: SettingsDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise APIError(401, "unauthorized", "Authentication required")

    payload = decode_access_token(creds.credentials, settings)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise APIError(401, "invalid_token", "Invalid authentication token")

    user = db.get(User, user_id)
    if user is None or user.status != "active":
        raise APIError(401, "unauthorized", "Account is not active")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise APIError(403, "forbidden", "You do not have permission to do this")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
