"""Documents, the task assistant (plan -> confirm), the multi-tool agent's tools, reminders and the scheduler."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import add_member, auth, create_meeting, signup
from meetmind.ai import agents, task_agent
from meetmind.ai.pipeline import run_pipeline
from meetmind.ai.task_agent import ProposedAction, ProposedPlan
from meetmind.documents import process_document
from test_ai import make_deps, upload


# ---------- documents ----------

def _attach(client, token, meeting_id, name="bug-report.md", body=b"# Bug 142\nLogin fails with error E42 on Safari 17."):
    return client.post(f"/api/meetings/{meeting_id}/documents", files={"file": (name, body)}, headers=auth(token))


def test_documents_upload_process_and_search(app, client, invites, enqueued, settings):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    res = _attach(client, member, m["id"])  # members who attend can attach documents too
    assert res.status_code == 202, res.json()
    doc = res.json()["document"]
    assert doc["status"] == "queued" and ("meetmind.jobs.process_document", (doc["id"],)) in enqueued

    assert process_document(app.state.session_factory, settings, app.state.rag, doc["id"]) == "ready"
    meeting = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]
    assert meeting["documents"][0]["status"] == "ready" and meeting["documents"][0]["pages"] == 1

    hits = client.get("/api/search", params={"q": "login error E42 Safari"}, headers=auth(admin)).json()["items"]
    assert hits[0]["kind"] == "document" and hits[0]["document_name"] == "bug-report.md" and hits[0]["page"] == 1

    # re-indexing the meeting keeps its documents
    run_pipeline(make_deps(app), upload(client, admin, m["id"]))
    kinds = {c.metadata["kind"] for c, _ in app.state.rag.store.items.values()}
    assert {"document", "transcript", "notes"} <= kinds

    assert client.delete(f"/api/documents/{doc['id']}", headers=auth(admin)).status_code == 204
    assert all(c.metadata.get("document_id") != doc["id"] for c, _ in app.state.rag.store.items.values())


def test_document_rules(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    assert _attach(client, admin, m["id"], name="virus.exe").status_code == 400
    private = create_meeting(client, admin, participants=["bob@acme.com"], title="Private")
    assert _attach(client, member, private["id"]).status_code == 404


def test_unreadable_document_fails_clearly(app, client, enqueued, settings):
    admin = signup(client)
    m = create_meeting(client, admin)
    doc = _attach(client, admin, m["id"], name="broken.pdf", body=b"not really a pdf").json()["document"]
    assert process_document(app.state.session_factory, settings, app.state.rag, doc["id"]) == "failed"
    d = client.get(f"/api/meetings/{m['id']}/documents", headers=auth(admin)).json()["items"][0]
    assert d["status"] == "failed" and "Could not read" in d["error"]


# ---------- task assistant ----------

@pytest.fixture
def reviewed(app, client, invites, enqueued):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin, participants=["maya@acme.com", "jordan@acme.com"])
    run_pipeline(make_deps(app), upload(client, admin, m["id"]))  # 2 AI drafts, meeting in "review"
    return {"admin": admin, "member": member, "mid": m["id"]}


def _propose(monkeypatch, plan):
    monkeypatch.setattr(task_agent, "propose", lambda settings, db, meeting, instruction: (plan, "fake:planner"))


def test_assistant_plans_then_applies_on_confirm(client, reviewed, monkeypatch, outbox):
    admin, mid = reviewed["admin"], reviewed["mid"]
    drafts = client.get(f"/api/meetings/{mid}/tasks", headers=auth(admin)).json()["items"]
    docs_n = next(i for i, t in enumerate(drafts, 1) if "API" in t["title"])
    demo_n = next(i for i, t in enumerate(drafts, 1) if "demo" in t["title"])
    _propose(monkeypatch, ProposedPlan(notify_owners=True, message="Please complete as soon as possible", actions=[
        ProposedAction(action="assign", task_number=docs_n, title=None, owner="Maya", due_text="ASAP", priority=None),
        ProposedAction(action="assign", task_number=demo_n, title=None, owner="Jordan", due_text="Thursday", priority="high"),
        ProposedAction(action="create", task_number=None, title="Write the release notes", owner="Maya",
                       due_text="next Friday", priority=None),
        ProposedAction(action="assign", task_number=99, title=None, owner="Ghost", due_text=None, priority=None),
    ]))
    outbox.clear()
    res = client.post(f"/api/meetings/{mid}/assistant/plan", json={"instruction": "Docs to Maya ASAP, Jordan the demo by Thursday"},
                      headers=auth(admin))
    assert res.status_code == 201, res.json()
    run = res.json()["run"]
    steps = run["plan"]["steps"]
    assert run["status"] == "proposed" and len(steps) == 3
    assert steps[0]["owner_email"] == "maya@acme.com" and steps[0]["priority"] == "urgent" and steps[0]["due_date"]
    assert any("#99" in w for w in run["plan"]["warnings"])
    # nothing changed yet
    assert all(not t["approved"] for t in client.get(f"/api/meetings/{mid}/tasks", headers=auth(admin)).json()["items"])
    assert outbox == []

    res = client.post(f"/api/agent-runs/{run['id']}/confirm", headers=auth(admin))
    assert res.status_code == 200, res.json()
    meeting = res.json()["meeting"]
    assert meeting["status"] == "completed"  # all drafts handled -> paused review closed
    tasks = {t["title"]: t for t in meeting["tasks"]}
    assert tasks["Write the release notes"]["assignee"]["email"] == "maya@acme.com"
    assert all(t["approved"] for t in meeting["tasks"])
    assert any(t["priority"] == "urgent" for t in meeting["tasks"])
    mail = next(m for m in outbox if m["to"] == "maya@acme.com")
    assert "Please complete as soon as possible" in mail["html"] and "URGENT" in mail["html"]

    again = client.post(f"/api/agent-runs/{run['id']}/confirm", headers=auth(admin))
    assert again.status_code == 409
    history = client.get(f"/api/meetings/{mid}/assistant/runs", headers=auth(admin)).json()["items"]
    assert history[0]["status"] == "done" and history[0]["result"]["changed"] == 3


def test_assistant_cancel_and_permissions(client, reviewed, monkeypatch):
    admin, member, mid = reviewed["admin"], reviewed["member"], reviewed["mid"]
    _propose(monkeypatch, ProposedPlan(notify_owners=False, message=None, actions=[]))
    assert client.post(f"/api/meetings/{mid}/assistant/plan", json={"instruction": "approve all"},
                       headers=auth(member)).status_code == 403
    run = client.post(f"/api/meetings/{mid}/assistant/plan", json={"instruction": "approve all"},
                      headers=auth(admin)).json()["run"]
    assert client.post(f"/api/agent-runs/{run['id']}/cancel", headers=auth(admin)).json()["run"]["status"] == "cancelled"
    assert client.post(f"/api/agent-runs/{run['id']}/confirm", headers=auth(admin)).status_code == 409


# ---------- multi-tool agent: tools respect access ----------

def test_agent_tools_respect_access(app, client, invites, enqueued, settings):
    admin = signup(client)
    member = add_member(client, admin, invites)
    mine = create_meeting(client, admin, participants=["maya@acme.com"], title="Mine")
    other = create_meeting(client, admin, participants=["bob@acme.com"], title="Secret")
    client.post(f"/api/meetings/{mine['id']}/tasks", json={"title": "Visible task", "assignee_email": "maya@acme.com",
                                                            "priority": "urgent"}, headers=auth(admin))
    client.post(f"/api/meetings/{other['id']}/tasks", json={"title": "Hidden task"}, headers=auth(admin))

    from meetmind.models import User
    with app.state.session_factory() as db:
        maya = db.query(User).filter_by(email="maya@acme.com").one()
        scope = agents.scope_for(app.state.session_factory, db, settings, app.state.rag, maya)
    tools = {t.name: t for t in agents.build_tools(scope, agents._Sources(), [])}
    listed = tools["list_tasks"].invoke({"priority": "urgent"})
    assert "Visible task" in listed and "Hidden task" not in listed
    meetings = tools["list_meetings"].invoke({})
    assert "Mine" in meetings and "Secret" not in meetings
    assert "not found" in tools["get_meeting_details"].invoke({"meeting_title": "Secret"}).lower()


def test_ask_falls_back_to_plain_answers_without_ai(client, invites):
    admin = signup(client)
    res = client.post("/api/search/ask", json={"question": "anything"}, headers=auth(admin))
    assert res.status_code == 200 and res.json()["tools"] == ["rag"]


# ---------- reminders & scheduler ----------

def test_task_reminders_and_admin_digest(app, client, invites, settings, outbox, enqueued):
    from meetmind.reminders import send_admin_digests, send_task_reminders

    admin = signup(client)
    add_member(client, admin, invites)
    m = create_meeting(client, admin)
    today = date(2026, 11, 23)
    client.post(f"/api/meetings/{m['id']}/tasks", json={"title": "Overdue thing", "assignee_email": "maya@acme.com",
                                                         "due_date": "2026-11-20", "priority": "urgent"}, headers=auth(admin))
    client.post(f"/api/meetings/{m['id']}/tasks", json={"title": "Due tomorrow", "assignee_email": "maya@acme.com",
                                                         "due_date": "2026-11-24"}, headers=auth(admin))
    client.post(f"/api/meetings/{m['id']}/tasks", json={"title": "Later", "assignee_email": "maya@acme.com",
                                                         "due_date": "2026-12-20"}, headers=auth(admin))
    outbox.clear()
    assert send_task_reminders(app.state.session_factory, settings, today) == 1
    mail = outbox[0]
    assert mail["to"] == "maya@acme.com" and "Overdue thing" in mail["text"] and "Due tomorrow" in mail["text"]
    assert "Later" not in mail["text"]
    assert send_task_reminders(app.state.session_factory, settings, today) == 0  # once per day/deadline
    assert send_task_reminders(app.state.session_factory, settings, today + timedelta(days=3)) == 1  # overdue repeats

    outbox.clear()
    assert send_admin_digests(app.state.session_factory, settings, today) == 1
    assert outbox[0]["to"] == "admin@acme.com" and "Overdue thing" in outbox[0]["html"]


def test_scheduler_queues_each_job_once(settings, enqueued):
    from meetmind.scheduler import tick

    class FakeRedis:
        def __init__(self):
            self.keys = set()

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

    settings.reminder_hour, settings.reminder_timezone = 9, "Asia/Kolkata"
    redis = FakeRedis()
    at_nine = datetime(2026, 11, 23, 9, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert tick(settings, redis, at_nine) == ["send_daily_reminders", "rescue_recordings"]
    assert tick(settings, redis, at_nine) == []
    assert ("meetmind.jobs.send_daily_reminders", ("2026-11-23",)) in enqueued


def test_interrupted_live_recording_is_rescued(app, client, enqueued, settings):
    from meetmind.models import Recording
    from meetmind.reminders import rescue_abandoned_recordings

    admin = signup(client)
    m = create_meeting(client, admin)
    rec = client.post(f"/api/meetings/{m['id']}/live-recordings", json={"mode": "room"}, headers=auth(admin)).json()["recording"]
    for seq in (0, 1, 3):  # piece 2 never arrived
        client.put(f"/api/recordings/{rec['id']}/chunks/{seq}", content=b"x", headers=auth(admin))
    with app.state.session_factory() as db:
        r = db.get(Recording, __import__("uuid").UUID(rec["id"]))
        r.updated_at = datetime.now(timezone.utc) - timedelta(hours=7)
        db.commit()
    assert rescue_abandoned_recordings(app.state.session_factory, settings) == 1
    assert ("meetmind.jobs.process_recording", (rec["id"],)) in enqueued
    parts = sorted(p.name for p in Path(settings.upload_dir).rglob("*.part"))
    assert parts == ["000000.part", "000001.part"]  # pieces after the gap are dropped


def test_agent_text_search_fallback_when_vector_index_fails(app, client, invites, enqueued, settings):
    """If embeddings/Chroma are down, the assistant's tools still find documents and transcript lines."""
    admin = signup(client)
    m = create_meeting(client, admin)
    doc = _attach(client, admin, m["id"]).json()["document"]
    process_document(app.state.session_factory, settings, app.state.rag, doc["id"])
    run_pipeline(make_deps(app), upload(client, admin, m["id"]))

    class Down:
        def embed_query(self, text):
            raise ConnectionError("410 Gone")

    app.state.rag.embedder = Down()
    app.state.rag.store.items.clear()  # empty index (e.g. model retired before indexing)
    from meetmind.models import User
    with app.state.session_factory() as db:
        scope = agents.scope_for(app.state.session_factory, db, settings, app.state.rag,
                                 db.query(User).filter_by(email="admin@acme.com").one(), m["id"])
    sources = agents._Sources()
    tools = {t.name: t for t in agents.build_tools(scope, sources, [])}
    found = tools["search_documents"].invoke({"query": "login error E42 Safari"})
    assert "bug-report.md" in found and "E42" in found
    said = tools["search_meeting"].invoke({"query": "stakeholder demo Jordan"})
    assert "Jordan, please book the stakeholder demo" in said and "00:00:12" in said


def test_fuzzy_owner_matching():
    from meetmind.ai.analysis import match_owner
    people = [("Maya", "maya@example.com"), (None, "alex@example.com"), (None, "jordan@example.com")]
    assert match_owner("Mya", people) == "maya@example.com"  # speech-to-text typo
    assert match_owner("Jordon", people) == "jordan@example.com"
    assert match_owner("Al", people) is None  # too short to guess


def test_owner_note_is_written_to_owners():
    from meetmind.ai.task_agent import _owner_note
    urgent = [{"op": "assign", "priority": "urgent"}]
    assert _owner_note("Please finish this before the client call.", urgent) == "Please finish this before the client call."
    assert _owner_note("Tasks 1 and 2 have been assigned as requested.", urgent) ==         "This is urgent. Please complete it as soon as possible."
    assert _owner_note(None, [{"op": "assign", "priority": "normal"}]) is None
