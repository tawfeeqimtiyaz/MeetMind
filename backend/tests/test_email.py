"""Email tests: templates, SMTP delivery, and which events send which emails."""
import re

import pytest

from conftest import PASSWORD, auth, create_meeting, signup
from meetmind import mailer
from meetmind.ai.pipeline import mark_failed, run_pipeline
from test_ai import make_deps, upload


def _invite(client, admin, email="new@acme.com", role="member", name="Noor"):
    res = client.post("/api/users/invite", json={"email": email, "name": name, "role": role}, headers=auth(admin))
    assert res.status_code == 201, res.json()
    return res.json()["user"]


def _token(mail) -> str:
    return re.search(r"accept-invite\.html\?token=([\w-]+)", mail["text"]).group(1)


def test_invite_email_link_works(client, outbox):
    admin = signup(client)
    _invite(client, admin, role="admin")

    [mail] = outbox
    assert mail["to"] == "new@acme.com"
    assert mail["subject"] == "Ada Admin invited you to Acme on MeetMind"
    assert "as an admin" in mail["text"] and "Accept invite" in mail["html"]

    res = client.post("/api/auth/accept-invite", json={"token": _token(mail), "password": PASSWORD, "name": "Noor"})
    assert res.status_code == 200 and res.json()["user"]["role"] == "admin"


def test_resend_invite_replaces_old_link(client, outbox):
    admin = signup(client)
    user = _invite(client, admin)
    old = _token(outbox[0])

    res = client.post(f"/api/users/{user['id']}/resend-invite", headers=auth(admin))
    assert res.status_code == 200
    new = _token(outbox[1])
    assert new != old
    assert client.post("/api/auth/accept-invite", json={"token": old, "password": PASSWORD}).status_code == 400
    assert client.post("/api/auth/accept-invite", json={"token": new, "password": PASSWORD}).status_code == 200
    # once joined, there is nothing to resend
    assert client.post(f"/api/users/{user['id']}/resend-invite", headers=auth(admin)).status_code == 409


def test_html_escapes_user_content():
    html, text = mailer.render(
        "review_ready", subject="s", app_url="http://x", name="<b>Ada</b>", meeting_title="<script>alert(1)</script>",
        summary="Tom & Jerry", draft_count=2, meeting_url="http://x/m",
    )
    assert "<script>" not in html and "&lt;script&gt;" in html and "Tom &amp; Jerry" in html
    assert "<script>alert(1)</script>" in text  # plain text is not HTML, nothing to escape


def test_headers_cannot_be_injected(settings):
    msg = mailer.build_message(settings, "a@acme.com", "Hi\r\nBcc: evil@x.com", "<p>x</p>", "x")
    assert msg["Subject"] == "Hi Bcc: evil@x.com" and msg["Bcc"] is None


def test_send_now_uses_smtp_settings(settings, monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg.get_content_type()))

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    settings.smtp_host, settings.smtp_port = "smtp.example.com", 587
    settings.smtp_starttls, settings.smtp_username, settings.smtp_password = True, "bot@example.com", "secret"
    mailer.send_now(settings, "a@acme.com", "Hello", "<p>Hi</p>", "Hi")
    assert calls == [("connect", "smtp.example.com", 587), ("starttls",), ("login", "bot@example.com"),
                     ("send", "a@acme.com", "multipart/alternative")]


def test_email_can_be_disabled(client, outbox, settings):
    settings.email_enabled = False
    admin = signup(client)
    _invite(client, admin)
    assert outbox == []


# ---------- pipeline emails ----------

@pytest.fixture
def team(app, client, outbox, enqueued):
    """Admin + active member Maya (via the real invite email) + a meeting with both."""
    admin = signup(client)
    _invite(client, admin, email="maya@acme.com", name="Maya")
    client.post("/api/auth/accept-invite", json={"token": _token(outbox[-1]), "password": PASSWORD})
    m = create_meeting(client, admin, participants=["maya@acme.com", "jordan@acme.com"])
    outbox.clear()
    return {"admin": admin, "meeting_id": m["id"]}


def test_review_ready_then_tasks_assigned(app, client, outbox, team):
    admin, mid = team["admin"], team["meeting_id"]
    run_pipeline(make_deps(app), upload(client, admin, mid))

    [ready] = outbox
    assert ready["to"] == "admin@acme.com"
    assert ready["subject"] == "Ready for review: Product Launch Sync"
    assert f"meeting.html?id={mid}" in ready["text"] and "2 tasks" in ready["text"]

    drafts = client.get(f"/api/meetings/{mid}/tasks", headers=auth(admin)).json()["items"]
    client.post(f"/api/meetings/{mid}/review", json={"approved_task_ids": [t["id"] for t in drafts]},
                headers=auth(admin))
    # Maya has an account -> emailed; Jordan has no account yet -> not emailed
    assigned = outbox[1:]
    assert [m["to"] for m in assigned] == ["maya@acme.com"]
    assert assigned[0]["subject"] == "1 new task from Product Launch Sync"
    assert "Finish the API documentation" in assigned[0]["html"] and "Fri 04 Dec 2026" in assigned[0]["text"]


def test_rejected_tasks_send_nothing(app, client, outbox, team):
    admin, mid = team["admin"], team["meeting_id"]
    run_pipeline(make_deps(app), upload(client, admin, mid))
    client.post(f"/api/meetings/{mid}/review", json={"approved_task_ids": []}, headers=auth(admin))
    assert [m["subject"] for m in outbox] == ["Ready for review: Product Launch Sync"]


def test_processing_failed_email(app, client, outbox, team):
    admin, mid = team["admin"], team["meeting_id"]
    rec_id = upload(client, admin, mid)
    mark_failed(make_deps(app), rec_id, "Transcription needs GROQ_API_KEY in .env.")
    [mail] = outbox
    assert mail["to"] == "admin@acme.com" and mail["subject"] == "Processing failed: Product Launch Sync"
    assert "Transcription needs GROQ_API_KEY" in mail["html"]


def test_email_failure_never_breaks_the_api(client, monkeypatch):
    from meetmind import notifications

    def broken(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(notifications, "deliver", broken)
    admin = signup(client)
    res = client.post("/api/users/invite", json={"email": "x@acme.com"}, headers=auth(admin))
    assert res.status_code == 201
