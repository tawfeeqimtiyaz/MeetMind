"""Dashboard insights, the tasks board and processing stages."""
import uuid
from datetime import date, timedelta

from conftest import add_member, auth, create_meeting, signup
from test_ai import ANALYSIS, TRANSCRIPT, upload

from meetmind.ai.pipeline import PipelineDeps, run_pipeline
from meetmind.models import Meeting, Recording, Task

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
IN_3_DAYS = (date.today() + timedelta(days=3)).isoformat()


def add_task(client, admin, meeting_id, **body):
    res = client.post(f"/api/meetings/{meeting_id}/tasks", json=body, headers=auth(admin))
    assert res.status_code == 201, res.json()
    return res.json()["task"]


def test_pipeline_reports_its_stage(app, client, invites, enqueued):
    admin = signup(client)
    m = create_meeting(client, admin)
    rec_id = upload(client, admin, m["id"])
    seen = []

    def stage():
        with app.state.session_factory() as db:
            return db.get(Recording, uuid.UUID(rec_id)).stage

    def transcribe(path, hint):
        seen.append(stage())
        return TRANSCRIPT

    def analyze(ctx, text):
        seen.append(stage())
        return ANALYSIS, "fake:model"

    run_pipeline(PipelineDeps(app.state.session_factory, app.state.settings, transcribe, analyze), rec_id)
    assert seen == ["transcribing", "analysing"]
    rec = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]["recordings"][0]
    assert rec["status"] == "done" and rec["stage"] is None


def test_admin_dashboard_insights(client, invites, enqueued, db_session):
    admin = signup(client)
    add_member(client, admin, invites)  # maya@acme.com
    m = create_meeting(client, admin, participants=["maya@acme.com", "jordan@acme.com"])
    add_task(client, admin, m["id"], title="Fix login", assignee_email="maya@acme.com", due_date=YESTERDAY, priority="urgent")
    add_task(client, admin, m["id"], title="Write docs", assignee_email="maya@acme.com", due_date=IN_3_DAYS)
    add_task(client, admin, m["id"], title="Book demo", assignee_email="jordan@acme.com")
    done = add_task(client, admin, m["id"], title="Old thing")
    client.patch(f"/api/tasks/{done['id']}", json={"status": "done"}, headers=auth(admin))

    review = create_meeting(client, admin, title="Pricing review")
    failed = create_meeting(client, admin, title="Broken upload")
    for mid, status in ((review["id"], "review"), (failed["id"], "failed")):
        db_session.get(Meeting, uuid.UUID(mid)).status = status
    db_session.add(Task(organization_id=db_session.get(Meeting, uuid.UUID(review["id"])).organization_id,
                        meeting_id=uuid.UUID(review["id"]), title="Draft", source="ai", approved=False))
    db_session.commit()
    rec_id = upload(client, admin, m["id"])
    client.patch(f"/api/recordings/{rec_id}", json={"important": True}, headers=auth(admin))

    body = client.get("/api/dashboard", headers=auth(admin)).json()
    assert body["stats"]["open_tasks"] == 3  # unchanged stat cards
    ins = body["insights"]
    assert ins["overdue_tasks"] == 1 and ins["urgent_tasks"] == 1
    assert ins["tasks_by_status"] == {"open": 3, "in_progress": 0, "done": 1}
    assert ins["workload"][0] == {"name": "Maya", "email": "maya@acme.com", "open": 2, "overdue": 1, "urgent": 1}
    assert {w["email"] for w in ins["workload"]} == {"maya@acme.com", "jordan@acme.com"}
    kinds = {(a["kind"], a["title"]) for a in ins["attention"]}
    assert ("review", "Pricing review") in kinds and ("failed", "Broken upload") in kinds
    assert ("starred", "Product Launch Sync") in kinds
    review_item = next(a for a in ins["attention"] if a["kind"] == "review")
    assert review_item["detail"] == "1 AI-drafted task to review"


def test_member_dashboard_insights(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    add_task(client, admin, m["id"], title="Late", assignee_email="maya@acme.com", due_date=YESTERDAY)
    body = client.get("/api/dashboard", headers=auth(member)).json()
    assert body["role"] == "member" and body["insights"]["overdue_tasks"] == 1
    assert isinstance(body["insights"]["upcoming"], list) and "workload" not in body["insights"]


def test_task_board_filters_and_access(client, invites, db_session):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin, participants=["maya@acme.com", "jordan@acme.com"])
    add_task(client, admin, m["id"], title="Fix login bug", assignee_email="maya@acme.com", due_date=YESTERDAY, priority="urgent")
    add_task(client, admin, m["id"], title="Write release notes", assignee_email="jordan@acme.com", due_date=IN_3_DAYS)
    add_task(client, admin, m["id"], title="Nobody's task")
    org_id = db_session.get(Meeting, uuid.UUID(m["id"])).organization_id
    db_session.add(Task(organization_id=org_id, meeting_id=uuid.UUID(m["id"]), title="AI draft", source="ai", approved=False))
    db_session.commit()

    def titles(token, query=""):
        res = client.get("/api/tasks" + query, headers=auth(token))
        assert res.status_code == 200, res.json()
        return {t["title"] for t in res.json()["items"]}

    assert titles(admin) == {"Fix login bug", "Write release notes", "Nobody's task"}  # drafts are not on the board
    assert titles(admin, "?priority=urgent") == {"Fix login bug"}
    assert titles(admin, "?due=overdue") == {"Fix login bug"}
    assert titles(admin, "?due=week") == {"Write release notes"}
    assert titles(admin, "?assignee=none") == {"Nobody's task"}
    assert titles(admin, "?assignee=jordan@acme.com") == {"Write release notes"}
    assert titles(admin, "?q=LOGIN") == {"Fix login bug"}
    assert titles(admin, "?q=100%25") == set()  # % is a literal, not a wildcard
    assert titles(member) == {"Fix login bug"}  # members only see their own tasks
    assert titles(member, "?assignee=jordan@acme.com") == {"Fix login bug"}  # owner filter ignored for members
    assert client.get("/api/tasks?status=bogus", headers=auth(admin)).status_code == 422

    other = signup(client, email="admin@globex.com", org="Globex")
    assert titles(other) == set()
