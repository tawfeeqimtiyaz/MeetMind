"""Test fixtures: the FastAPI app on in-memory SQLite, plus helpers to create orgs and users."""
import pytest
from fastapi.testclient import TestClient

from meetmind import create_app, notifications, task_queue
from meetmind.config import Settings
from meetmind.db import Base

PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        app_env="test",
        secret_key="test-secret-key-that-is-long-enough-for-hs256",
        database_url="sqlite+pysqlite:///:memory:",
        upload_dir=str(tmp_path / "uploads"),
        ratelimit_enabled=False,
    )


@pytest.fixture
def app(settings):
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    yield app
    Base.metadata.drop_all(app.state.engine)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session(app):
    """A direct DB session, for arranging data the API can't create (e.g. AI-made tasks)."""
    with app.state.session_factory() as s:
        yield s


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """Every email the app would send, instead of queueing it on Redis."""
    sent = []
    monkeypatch.setattr(notifications, "deliver",
                        lambda settings, to, subject, html, text: sent.append(
                            {"to": to, "subject": subject, "html": html, "text": text}))
    return sent


@pytest.fixture
def invites(monkeypatch):
    """Capture invite tokens instead of logging/emailing them."""
    sent = {}
    monkeypatch.setattr(notifications, "send_invite",
                        lambda user, token, inviter, settings: sent.__setitem__(user.email, token))
    return sent


@pytest.fixture
def enqueued(monkeypatch):
    """Capture background jobs instead of sending them to Redis."""
    jobs = []
    monkeypatch.setattr(task_queue, "enqueue", lambda settings, func, *args, **kw: jobs.append((func, args)))
    return jobs


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, email="admin@acme.com", org="Acme"):
    res = client.post("/api/auth/signup", json={
        "organization_name": org, "name": "Ada Admin", "email": email, "password": PASSWORD,
    })
    assert res.status_code == 201, res.json()
    return res.json()["token"]


def add_member(client, admin_token, invites, email="maya@acme.com", role="member"):
    res = client.post("/api/users/invite", json={"email": email, "name": "Maya", "role": role},
                      headers=auth(admin_token))
    assert res.status_code == 201, res.json()
    res = client.post("/api/auth/accept-invite", json={"token": invites[email], "password": PASSWORD})
    assert res.status_code == 200, res.json()
    return res.json()["token"]


def create_meeting(client, admin_token, participants=("maya@acme.com",), title="Product Launch Sync"):
    res = client.post("/api/meetings", json={
        "title": title,
        "agenda": "Finalize roadmap",
        "scheduled_at": "2026-12-01T10:00:00Z",
        "participants": list(participants),
    }, headers=auth(admin_token))
    assert res.status_code == 201, res.json()
    return res.json()["meeting"]
