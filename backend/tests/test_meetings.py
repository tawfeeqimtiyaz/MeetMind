from pathlib import Path

from conftest import add_member, auth, create_meeting, signup


def test_admin_creates_and_lists_meetings(client, invites):
    admin = signup(client)
    m = create_meeting(client, admin)
    assert m["status"] == "scheduled"
    assert m["participants"][0]["email"] == "maya@acme.com"

    listing = client.get("/api/meetings", headers=auth(admin)).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == m["id"]


def test_member_cannot_create_meeting(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    res = client.post("/api/meetings", json={"title": "x", "scheduled_at": "2026-12-01T10:00:00Z"},
                      headers=auth(member))
    assert res.status_code == 403


def test_member_sees_only_meetings_they_attend(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    mine = create_meeting(client, admin, participants=["maya@acme.com"], title="With Maya")
    other = create_meeting(client, admin, participants=["bob@acme.com"], title="Without Maya")

    items = client.get("/api/meetings", headers=auth(member)).json()["items"]
    assert [i["id"] for i in items] == [mine["id"]]
    assert client.get(f"/api/meetings/{other['id']}", headers=auth(member)).status_code == 404


def test_participant_added_before_invite_gets_linked(client, invites):
    admin = signup(client)
    m = create_meeting(client, admin, participants=["later@acme.com"])
    member = add_member(client, admin, invites, email="later@acme.com")
    assert client.get(f"/api/meetings/{m['id']}", headers=auth(member)).status_code == 200


def test_organizations_are_isolated(client, invites):
    acme = signup(client, email="admin@acme.com", org="Acme")
    globex = signup(client, email="admin@globex.com", org="Globex")
    m = create_meeting(client, acme)

    assert client.get(f"/api/meetings/{m['id']}", headers=auth(globex)).status_code == 404
    assert client.get("/api/meetings", headers=auth(globex)).json()["total"] == 0
    assert client.delete(f"/api/meetings/{m['id']}", headers=auth(globex)).status_code == 404


def test_update_and_delete_meeting(client, invites):
    admin = signup(client)
    m = create_meeting(client, admin)
    res = client.patch(f"/api/meetings/{m['id']}", json={"title": "Renamed", "participants": []},
                       headers=auth(admin))
    assert res.status_code == 200
    body = res.json()["meeting"]
    assert body["title"] == "Renamed" and body["participants"] == []

    assert client.delete(f"/api/meetings/{m['id']}", headers=auth(admin)).status_code == 204
    assert client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).status_code == 404


def test_upload_recording_queues_job(client, invites, enqueued, settings):
    admin = signup(client)
    m = create_meeting(client, admin)
    res = client.post(
        f"/api/meetings/{m['id']}/recordings",
        files={"file": ("../../etc/standup.mp3", b"fake audio bytes", "audio/mpeg")},
        headers=auth(admin),
    )
    assert res.status_code == 202, res.json()
    rec = res.json()["recording"]
    assert rec["status"] == "queued"
    assert rec["original_filename"] == "standup.mp3"  # path parts stripped
    assert rec["size_bytes"] == len(b"fake audio bytes")
    assert enqueued == [("meetmind.jobs.process_recording", (rec["id"],))]
    # Stored under the upload dir with a generated name, never the client's path
    stored = list(Path(settings.upload_dir).rglob("*.mp3"))
    assert len(stored) == 1 and stored[0].name != "standup.mp3"


def test_upload_strips_windows_paths(client, invites, enqueued):
    admin = signup(client)
    m = create_meeting(client, admin)
    res = client.post(f"/api/meetings/{m['id']}/recordings",
                      files={"file": (r"C:\Users\me\call.m4a", b"audio")}, headers=auth(admin))
    assert res.status_code == 202, res.json()
    assert res.json()["recording"]["original_filename"] == "call.m4a"


def test_upload_rejects_bad_type_missing_and_empty_file(client, invites, enqueued):
    admin = signup(client)
    m = create_meeting(client, admin)
    url = f"/api/meetings/{m['id']}/recordings"
    bad = client.post(url, files={"file": ("evil.exe", b"x")}, headers=auth(admin))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "unsupported_file_type"
    missing = client.post(url, headers=auth(admin))
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "no_file"
    empty = client.post(url, files={"file": ("empty.mp3", b"")}, headers=auth(admin))
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "empty_file"
    assert enqueued == []


def test_upload_requires_admin(client, invites, enqueued):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    url = f"/api/meetings/{m['id']}/recordings"
    file = {"file": ("a.mp3", b"audio")}
    assert client.post(url, files=file).status_code == 401
    assert client.post(url, files=file, headers=auth(member)).status_code == 403
    assert enqueued == []


def test_upload_too_large(client, invites, enqueued, settings):
    settings.max_upload_mb = 0  # every non-empty file is now too big
    admin = signup(client)
    m = create_meeting(client, admin)
    res = client.post(f"/api/meetings/{m['id']}/recordings",
                      files={"file": ("a.mp3", b"audio")}, headers=auth(admin))
    assert res.status_code == 413
    assert list(Path(settings.upload_dir).rglob("*.mp3")) == []
    assert enqueued == []


def test_transcript_404_until_processed(client, invites):
    admin = signup(client)
    m = create_meeting(client, admin)
    assert client.get(f"/api/meetings/{m['id']}/transcript", headers=auth(admin)).status_code == 404


def test_dashboard_stats(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    client.post(f"/api/meetings/{m['id']}/tasks", json={"title": "A", "assignee_email": "maya@acme.com",
                                                         "due_date": "2000-01-01"}, headers=auth(admin))
    done = client.post(f"/api/meetings/{m['id']}/tasks", json={"title": "B"}, headers=auth(admin)).json()["task"]
    client.patch(f"/api/tasks/{done['id']}", json={"status": "done"}, headers=auth(admin))

    a = client.get("/api/dashboard", headers=auth(admin)).json()
    assert a["role"] == "admin"
    assert a["stats"]["open_tasks"] == 1 and a["stats"]["done_tasks"] == 1 and a["stats"]["awaiting_review"] == 0

    s = client.get("/api/dashboard", headers=auth(member)).json()
    from datetime import datetime, timezone
    upcoming = 1 if datetime.now(timezone.utc) < datetime(2026, 12, 1, 10, tzinfo=timezone.utc) else 0
    assert s == {"role": "member", "stats": {"open_tasks": 1, "due_this_week": 1, "upcoming_meetings": upcoming}}

    item = client.get("/api/meetings", headers=auth(admin)).json()["items"][0]
    assert item["draft_task_count"] == 0 and item["participant_count"] == 1
