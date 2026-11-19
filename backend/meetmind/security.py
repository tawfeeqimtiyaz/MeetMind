"""Password hashing, JWT access tokens and invite tokens (framework-independent)."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import Settings
from .errors import APIError

_hasher = PasswordHasher()

# Used to keep login timing the same whether or not the email exists
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


# ---------- passwords ----------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


# ---------- JWT access tokens ----------

def create_access_token(user, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "org": str(user.organization_id),
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expires_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str, settings: Settings) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise APIError(401, "token_expired", "Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise APIError(401, "invalid_token", "Invalid authentication token")


# ---------- invite tokens (only the hash is stored) ----------

def new_invite_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
