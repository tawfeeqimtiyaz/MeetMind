"""Live recording in the browser, notes during the meeting, follow-up Q&A and ASAP deadlines."""
from datetime import date
from pathlib import Path

from conftest import add_member, auth, create_meeting, signup
from meetmind.ai.analysis import LLMAnalysis, LLMFollowUp, LLMTask, add_working_days, resolve_due_date, task_priority
from meetmind.ai.pipeline import run_pipeline
from test_ai import TRANSCRIPT, make_deps


def _start(client, admin, meeting_id, mode="room"):
    res = client.post(f"/api/meetings/{meeting_id}/live-recordings", json={"mode": mode, "mime_type": "audio/webm"},
                      headers=auth(admin))
    assert res.status_code == 201, res.json()
    return res.json()["recording"]


def _piece(client, admin, rid, seq, data=b"audio-bytes"):
    return client.put(f"/api/recordings/{rid}/chunks/{seq}", content=data,
                      headers={**auth(admin), "Content-Type": "application/octet-stream"})


# ---------- instant meeting + live recording ----------

def test_instant_meeting(client):
    admin = signup(client)
    res = client.post("/api/meetings/instant", json={"title": "Standup", "timezone": "Asia/Kolkata"}, headers=auth(admin))
    assert res.status_code == 201
    m = res.json()["meeting"]
    assert m["title"] == "Standup" and m["timezone"] == "Asia/Kolkata" and m["status"] == "scheduled"


def test_live_recording_pieces_finish_and_process(app, client, invites, enqueued, settings):
    admin = signup(client)
    m = create_meeting(client, admin)
    rec = _start(client, admin, m["id"])
    assert rec["status"] == "recording" and rec["mode"] == "room"
    assert client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]["status"] == "recording"

    # pieces may arrive out of order and be re-sent
    assert _piece(client, admin, rec["id"], 1, b"-second").status_code == 204
    missing = client.post(f"/api/recordings/{rec['id']}/finish", json={"chunks": 2}, headers=auth(admin))
    assert missing.status_code == 409 and missing.json()["error"]["details"]["missing"] == [0]
    assert _piece(client, admin, rec["id"], 0, b"first").status_code == 204
    assert _piece(client, admin, rec["id"], 0, b"first").status_code == 204  # repeat is harmless

    done = client.post(f"/api/recordings/{rec['id']}/finish", json={"chunks": 2, "duration_seconds": 61},
                       headers=auth(admin))
    assert done.status_code == 202, done.json()
    assert done.json()["recording"]["status"] == "queued" and done.json()["recording"]["duration_seconds"] == 61
    assert enqueued[-1] == ("meetmind.jobs.process_recording", (rec["id"],))
    assert _piece(client, admin, rec["id"], 2).status_code == 409  # finished: no more pieces

    # the worker joins the pieces in order before transcribing
    seen = []
    deps = make_deps(app)
    deps.transcribe = lambda path, hint: (seen.append(path.read_bytes()), TRANSCRIPT)[1]
    run_pipeline(deps, rec["id"])
    assert seen == [b"first-second"]
    assert not list(Path(settings.upload_dir).rglob("*.part"))


def test_one_live_recording_at_a_time_and_members_cannot_record(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    _start(client, admin, m["id"])
    again = client.post(f"/api/meetings/{m['id']}/live-recordings", json={"mode": "online"}, headers=auth(admin))
    assert again.status_code == 409
    assert client.post(f"/api/meetings/{m['id']}/live-recordings", json={"mode": "room"},
                       headers=auth(member)).status_code == 403


def test_discard_live_recording(client, settings):
    admin = signup(client)
    m = create_meeting(client, admin)
    rec = _start(client, admin, m["id"])
    _piece(client, admin, rec["id"], 0)
    assert client.delete(f"/api/recordings/{rec['id']}", headers=auth(admin)).status_code == 204
    meeting = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]
    assert meeting["recordings"] == [] and meeting["status"] == "scheduled"
    assert not list(Path(settings.upload_dir).rglob("*.part"))


def test_star_and_play_recording(client, invites, enqueued):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    up = client.post(f"/api/meetings/{m['id']}/recordings", files={"file": ("call.mp3", b"0123456789")}, headers=auth(admin))
    rid = up.json()["recording"]["id"]
    starred = client.patch(f"/api/recordings/{rid}", json={"important": True}, headers=auth(admin))
    assert starred.json()["recording"]["important"] is True

    link = client.get(f"/api/recordings/{rid}/playback", headers=auth(member)).json()["url"]
    media = client.get(link, headers={"Range": "bytes=2-5"})
    assert media.status_code == 206 and media.content == b"2345"  # seeking works (HTTP range)
    assert client.get(link.split("?")[0] + "?token=forged").status_code == 401


# ---------- notes ----------

def test_notes_by_participants_reach_the_ai(app, client, invites, enqueued):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    res = client.post(f"/api/meetings/{m['id']}/notes", json={"text": "Maya will also send the release notes",
                                                              "offset_seconds": 42}, headers=auth(member))
    assert res.status_code == 201 and res.json()["note"]["author"]["email"] == "maya@acme.com"
    client.post(f"/api/meetings/{m['id']}/notes", json={"text": "Important moment", "kind": "bookmark",
                                                        "offset_seconds": 8}, headers=auth(admin))

    # A task that is only in the notes (not the audio) is accepted because the note is evidence
    analysis = LLMAnalysis(summary="s", strategy_shifts=[], open_questions=[], follow_ups=[], tasks=[
        LLMTask(priority=None, title="Send the release notes", owner="Maya", due_text=None, due_date=None,
                evidence="Maya will also send the release notes")])
    calls = []
    from test_ai import upload
    run_pipeline(make_deps(app, analysis=analysis, calls=calls), upload(client, admin, m["id"]))
    ctx = calls[1][1]
    assert "[00:00:42] Maya noted: Maya will also send the release notes" in ctx.notes
    assert any("STARRED by" in n for n in ctx.notes)
    titles = [t["title"] for t in client.get(f"/api/meetings/{m['id']}/tasks", headers=auth(admin)).json()["items"]]
    assert "Send the release notes" in titles


def test_summary_never_talks_about_star_markers():
    from meetmind.ai.pipeline import clean_summary

    assert clean_summary("The team discussed the mobile beta release plan, a moment marked important, originally "
                         "targeting December 15th.") == \
        "The team discussed the mobile beta release plan, originally targeting December 15th."
    assert clean_summary("Two moments were marked as important. The launch moved to Dec 22.") == "The launch moved to Dec 22."
    assert clean_summary("Pricing was agreed. One moment was flagged as important.") == "Pricing was agreed."
    # ordinary sentences are untouched
    text = "Maya marked the bug as important and will fix it. In a moment of clarity, Alex agreed."
    assert clean_summary(text) == text


def test_note_permissions(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    m = create_meeting(client, admin)
    note = client.post(f"/api/meetings/{m['id']}/notes", json={"text": "admin note"}, headers=auth(admin)).json()["note"]
    assert client.delete(f"/api/notes/{note['id']}", headers=auth(member)).status_code == 403
    other = create_meeting(client, admin, participants=["bob@acme.com"], title="Private")
    assert client.post(f"/api/meetings/{other['id']}/notes", json={"text": "x"}, headers=auth(member)).status_code == 404
    assert client.delete(f"/api/notes/{note['id']}", headers=auth(admin)).status_code == 204


# ---------- follow-ups & ASAP ----------

def test_follow_ups_and_urgent_tasks_are_saved(app, client, invites, enqueued):
    admin = signup(client)
    m = create_meeting(client, admin)
    analysis = LLMAnalysis(
        summary="s", strategy_shifts=["Ship on January 12"], open_questions=[],
        follow_ups=[LLMFollowUp(question="Can we keep calendar sync?", answer="No, it moves to v2",
                                asked_by="Jordan", answered_by=None)],
        tasks=[LLMTask(priority=None, title="Finish the API documentation", owner="Maya", due_text="ASAP",
                       due_date=None, evidence="I'll have the API documentation finished by Friday.")])
    from test_ai import upload
    run_pipeline(make_deps(app, analysis=analysis), upload(client, admin, m["id"]))
    meeting = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]
    assert meeting["follow_ups"][0]["answer"] == "No, it moves to v2"
    task = meeting["tasks"][0]
    assert task["priority"] == "urgent" and task["due_date"] == "2026-12-03"  # Tue 1 Dec + 2 working days


def test_asap_and_priority_rules():
    fri = date(2026, 11, 20)
    assert add_working_days(fri, 2) == date(2026, 11, 24)  # skips the weekend
    assert resolve_due_date("ASAP", fri) == date(2026, 11, 24)
    assert resolve_due_date("as soon as possible please", fri, asap_days=3) == date(2026, 11, 25)
    assert task_priority(None, "ASAP") == "urgent"
    assert task_priority("high", "Friday") == "high"
    assert task_priority(None, "Friday") == "normal"
