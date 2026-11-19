"""Request validation (pydantic). Anything invalid becomes a 422 with field details."""
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field


def _aware(v: datetime) -> datetime:
    """Treat timestamps without a timezone as UTC."""
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _dedupe(v: list[str]) -> list[str]:
    return list(dict.fromkeys(v))


def _timezone(v: str) -> str:
    try:
        ZoneInfo(v)
    except Exception:  # unknown name, or a path-like value
        raise ValueError("unknown time zone (use an IANA name like 'Europe/Berlin')")
    return v


Email = Annotated[EmailStr, AfterValidator(str.lower)]
TimeZoneName = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_timezone)]
UtcDatetime = Annotated[datetime, AfterValidator(_aware)]
EmailList = Annotated[list[Email], Field(max_length=100), AfterValidator(_dedupe)]
Password = Annotated[str, Field(min_length=10, max_length=128)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------- auth ----------

class SignupIn(_In):
    organization_name: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    email: Email
    password: Password


class LoginIn(_In):
    email: Email
    password: str = Field(min_length=1, max_length=128)


class InviteIn(_In):
    email: Email
    name: str | None = Field(default=None, max_length=120)
    role: Literal["admin", "member"] = "member"


class UserPatch(_In):
    role: Literal["admin", "member"] | None = None
    status: Literal["active", "disabled"] | None = None


class AcceptInviteIn(_In):
    token: str = Field(min_length=10, max_length=200)
    password: Password
    name: str | None = Field(default=None, max_length=120)


# ---------- meetings ----------

class MeetingIn(_In):
    title: str = Field(min_length=1, max_length=200)
    agenda: str | None = Field(default=None, max_length=5000)
    scheduled_at: UtcDatetime
    timezone: TimeZoneName | None = None
    participants: EmailList = []


class MeetingPatch(_In):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    agenda: str | None = Field(default=None, max_length=5000)
    scheduled_at: UtcDatetime | None = None
    timezone: TimeZoneName | None = None
    participants: EmailList | None = None


# ---------- tasks ----------

TaskStatus = Literal["open", "in_progress", "done"]


class TaskIn(_In):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    assignee_email: Email | None = None
    due_date: date | None = None


class TaskPatch(_In):
    """Members may only change `status` on their own tasks; admins may change everything."""
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    assignee_email: Email | None = None
    due_date: date | None = None
    status: TaskStatus | None = None
    approved: bool | None = None


# ---------- AI ----------

class ReviewIn(_In):
    """Admin's decision on AI-drafted tasks: listed tasks are approved, the other drafts are discarded."""
    approved_task_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class AskIn(_In):
    question: str = Field(min_length=1, max_length=1000)
