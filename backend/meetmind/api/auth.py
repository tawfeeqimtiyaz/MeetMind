"""Sign-up, login, invite acceptance, current user."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import select

from ..config import Settings
from ..deps import CurrentUser, DbSession, SettingsDep
from ..errors import APIError
from ..models import Organization, User
from ..ratelimit import rate_limit
from ..schemas import AcceptInviteIn, LoginIn, SignupIn
from ..security import create_access_token, hash_password, hash_token, verify_password
from ..serializers import user_out

router = APIRouter()
log = logging.getLogger(__name__)


def _session(user: User, settings: Settings) -> dict:
    return {"token": create_access_token(user, settings), "user": user_out(user)}


@router.post("/signup", status_code=201, dependencies=[rate_limit("5/hour", "signup")])
def signup(data: SignupIn, db: DbSession, settings: SettingsDep):
    """Create a new organization and its first admin."""
    if not settings.allow_signup:
        raise APIError(403, "signup_disabled", "Sign-up is disabled on this server")

    if db.scalar(select(User).where(User.email == data.email)):
        raise APIError(409, "email_taken", "An account with this email already exists")

    org = Organization(name=data.organization_name)
    user = User(
        organization=org,
        email=data.email,
        name=data.name,
        password_hash=hash_password(data.password),
        role="admin",
        status="active",
        last_login_at=datetime.now(timezone.utc),
    )
    db.add_all([org, user])
    db.commit()
    log.info("New organization %s created by %s", org.id, user.id)
    return _session(user, settings)


@router.post("/login", dependencies=[rate_limit("10/minute", "login")])
def login(data: LoginIn, db: DbSession, settings: SettingsDep):
    user = db.scalar(select(User).where(User.email == data.email))

    # Always run the hash check so response time doesn't reveal which emails exist
    if not verify_password(user.password_hash if user else None, data.password) or user.status != "active":
        raise APIError(401, "invalid_credentials", "Incorrect email or password")

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return _session(user, settings)


@router.post("/accept-invite", dependencies=[rate_limit("10/minute", "accept-invite")])
def accept_invite(data: AcceptInviteIn, db: DbSession, settings: SettingsDep):
    user = db.scalar(select(User).where(User.invite_token_hash == hash_token(data.token)))

    now = datetime.now(timezone.utc)
    expires = user.invite_expires_at if user else None
    if expires is not None and expires.tzinfo is None:  # SQLite drops tz info
        expires = expires.replace(tzinfo=timezone.utc)
    if user is None or user.status != "invited" or expires is None or expires < now:
        raise APIError(400, "invalid_invite", "This invite link is invalid or has expired")

    user.password_hash = hash_password(data.password)
    user.name = data.name or user.name
    user.status = "active"
    user.invite_token_hash = None
    user.invite_expires_at = None
    user.last_login_at = now
    db.commit()
    return _session(user, settings)


@router.get("/me")
def me(user: CurrentUser):
    return {"user": user_out(user)}
