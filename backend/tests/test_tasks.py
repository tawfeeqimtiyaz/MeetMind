import uuid

from conftest import add_member, auth, create_meeting, signup


def _create_task(client, admin, meeting_id, **body):
    payload = {"title": "Finalize API documentation", "assignee_email": "maya@acme.com", **body}
    res = client.post(f"/api/meetings/{meeting_id}/tasks", json=payload, headers=auth(admin))
    assert res.status_code == 201, res.json()
    return res.json()["task"]


def test_member_sees_and_updates_own_task(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    task = _create_task(client, admin, m["id"], due_date="2026-12-05")

    mine = client.get("/api/tasks/mine", headers=auth(member)).json()["items"]
    assert [t["id"] for t in mine] == [task["id"]]
    assert mine[0]["assignee"]["email"] == "maya@acme.com"

    res = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}, headers=auth(member))
    assert res.status_code == 200 and res.json()["task"]["status"] == "done"


def test_member_cannot_edit_other_fields_or_others_tasks(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    own = _create_task(client, admin, m["id"])
    other = _create_task(client, admin, m["id"], assignee_email="bob@acme.com")

    assert client.patch(f"/api/tasks/{own['id']}", json={"title": "hijack"}, headers=auth(member)).status_code == 403
    assert client.patch(f"/api/tasks/{other['id']}", json={"status": "done"}, headers=auth(member)).status_code == 404


def test_unapproved_ai_tasks_hidden_from_members(client, invites, db_session):
    """AI-extracted tasks wait for admin approval (human-in-the-loop)."""
    from meetmind.models import Task

    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    task = _create_task(client, admin, m["id"])

    t = db_session.get(Task, uuid.UUID(task["id"]))
    t.source, t.approved = "ai", False
    db_session.commit()

    assert client.get("/api/tasks/mine", headers=auth(member)).json()["items"] == []
    assert client.get(f"/api/meetings/{m['id']}/tasks", headers=auth(member)).json()["items"] == []
    assert len(client.get(f"/api/meetings/{m['id']}/tasks", headers=auth(admin)).json()["items"]) == 1

    client.patch(f"/api/tasks/{task['id']}", json={"approved": True}, headers=auth(admin))
    assert len(client.get("/api/tasks/mine", headers=auth(member)).json()["items"]) == 1


def test_invalid_status_rejected(client, invites):
    admin = signup(client)
    m = create_meeting(client, admin)
    task = _create_task(client, admin, m["id"])
    res = client.patch(f"/api/tasks/{task['id']}", json={"status": "finished"}, headers=auth(admin))
    assert res.status_code == 422
