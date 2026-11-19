"""AI pipeline tests. Real models are replaced by fakes, so no API keys or network are needed."""
import shutil
import subprocess
import uuid
from datetime import date

import pytest

from conftest import add_member, auth, create_meeting, signup
from meetmind.ai import qa
from meetmind.ai.analysis import (
    LLMAnalysis, LLMTask, MeetingContext, build_analysis_messages, evidence_supported, match_owner, parse_due_date,
    clean_title, resolve_due_date,
)
from meetmind.ai.llm import AIUnavailableError, Provider, run_with_fallback
from meetmind.ai.pipeline import PipelineDeps, run_pipeline
from meetmind.ai.transcription import TranscriptionResult, format_transcript, prepare_chunks, probe_duration

TRANSCRIPT = TranscriptionResult(
    text=("Thanks everyone, let's lock the launch plan. If we drop calendar sync we can ship on January 12. "
          "I'll have the API documentation finished by Friday. Jordan, please book the stakeholder demo."),
    segments=[
        {"start": 0.0, "end": 3.0, "text": "Thanks everyone, let's lock the launch plan."},
        {"start": 3.0, "end": 8.0, "text": "If we drop calendar sync we can ship on January 12."},
        {"start": 8.0, "end": 12.0, "text": "I'll have the API documentation finished by Friday."},
        {"start": 12.0, "end": 15.0, "text": "Jordan, please book the stakeholder demo."},
    ],
    language="en",
    provider="fake-whisper",
)

ANALYSIS = LLMAnalysis(
    summary="The team agreed to cut calendar sync and ship on January 12.",
    strategy_shifts=["Cut calendar sync from v1 to ship on January 12"],
    open_questions=["Who owns the pricing page copy?"],
    tasks=[
        LLMTask(title="Finish the API documentation", owner="Maya", due_text="by Friday", due_date="2026-12-18",
                evidence="I'll have the API documentation finished by Friday."),
        LLMTask(title="Book the stakeholder demo", owner="Jordan", due_text="next week", due_date=None,
                evidence="Jordan, please book the stakeholder demo"),
        LLMTask(title="Build a mobile app", owner="Alex", due_text=None, due_date=None,  # invented: not in transcript
                evidence="Alex will build the mobile app by Monday"),
    ],
)


def make_deps(app, transcript=TRANSCRIPT, analysis=ANALYSIS, calls=None):
    def transcribe(path, hint):
        if calls is not None:
            calls.append(("transcribe", path.name, hint))
        if isinstance(transcript, Exception):
            raise transcript
        return transcript

    def analyze(ctx, text):
        if calls is not None:
            calls.append(("analyze", ctx, text))
        return analysis, "fake:model"

    return PipelineDeps(app.state.session_factory, app.state.settings, transcribe, analyze)


def upload(client, admin, meeting_id):
    res = client.post(f"/api/meetings/{meeting_id}/recordings",
                      files={"file": ("standup.mp3", b"fake audio")}, headers=auth(admin))
    assert res.status_code == 202, res.json()
    return res.json()["recording"]["id"]


@pytest.fixture
def processed(app, client, invites, enqueued):
    """A meeting whose recording went through the pipeline and now waits for review."""
    admin = signup(client)
    member = add_member(client, admin, invites)  # maya@acme.com
    m = create_meeting(client, admin, participants=["maya@acme.com", "jordan@acme.com"])
    rec_id = upload(client, admin, m["id"])
    calls = []
    run_pipeline(make_deps(app, calls=calls), rec_id)
    return {"admin": admin, "member": member, "meeting_id": m["id"], "calls": calls}


# ---------- pipeline ----------

def test_pipeline_drafts_tasks_and_waits_for_review(client, processed):
    admin, mid = processed["admin"], processed["meeting_id"]
    meeting = client.get(f"/api/meetings/{mid}", headers=auth(admin)).json()["meeting"]

    assert meeting["status"] == "review"
    assert meeting["summary"].startswith("The team agreed")
    assert meeting["strategy_shifts"] == ["Cut calendar sync from v1 to ship on January 12"]
    assert meeting["open_questions"] == ["Who owns the pricing page copy?"]
    assert meeting["ai_model"] == "fake:model"
    assert meeting["recordings"][0]["status"] == "done"

    tasks = {t["title"]: t for t in meeting["tasks"]}
    assert set(tasks) == {"Finish the API documentation", "Book the stakeholder demo"}  # invented one dropped
    doc = tasks["Finish the API documentation"]
    # date computed from "by Friday" (meeting is Tue 2026-12-01), not the model's wrong 2026-12-18
    assert doc["assignee"]["email"] == "maya@acme.com" and doc["due_date"] == "2026-12-04"
    assert doc["approved"] is False and doc["source"] == "ai" and doc["evidence"]
    demo = tasks["Book the stakeholder demo"]
    assert demo["assignee"]["email"] == "jordan@acme.com" and demo["due_date"] == "2026-12-11"  # Fri next week

    transcript = client.get(f"/api/meetings/{mid}/transcript", headers=auth(admin)).json()["transcript"]
    assert transcript["provider"] == "fake-whisper" and len(transcript["segments"]) == 4


def test_pipeline_gives_model_meeting_context(processed):
    (_, filename, hint), (_, ctx, text) = processed["calls"]
    assert filename.endswith(".mp3") and "Maya" in hint
    assert ctx.title == "Product Launch Sync" and ctx.meeting_date == date(2026, 12, 1)
    assert ("Maya", "maya@acme.com") in ctx.participants
    assert text.startswith("[00:00:00] Thanks everyone")


def test_members_do_not_see_drafts(client, processed):
    member, mid = processed["member"], processed["meeting_id"]
    assert client.get(f"/api/meetings/{mid}/tasks", headers=auth(member)).json()["items"] == []
    assert client.get("/api/tasks/mine", headers=auth(member)).json()["items"] == []


def test_review_approves_selected_and_discards_rest(client, processed):
    admin, member, mid = processed["admin"], processed["member"], processed["meeting_id"]
    drafts = client.get(f"/api/meetings/{mid}/tasks", headers=auth(admin)).json()["items"]
    keep = next(t for t in drafts if t["title"] == "Finish the API documentation")

    res = client.post(f"/api/meetings/{mid}/review", json={"approved_task_ids": [keep["id"]]}, headers=auth(admin))
    assert res.status_code == 200, res.json()
    meeting = res.json()["meeting"]
    assert meeting["status"] == "completed"
    assert [t["title"] for t in meeting["tasks"]] == ["Finish the API documentation"]

    mine = client.get("/api/tasks/mine", headers=auth(member)).json()["items"]
    assert [t["id"] for t in mine] == [keep["id"]]

    again = client.post(f"/api/meetings/{mid}/review", json={"approved_task_ids": []}, headers=auth(admin))
    assert again.status_code == 409


def test_review_rules(client, processed):
    admin, member, mid = processed["admin"], processed["member"], processed["meeting_id"]
    assert client.post(f"/api/meetings/{mid}/review", json={}, headers=auth(member)).status_code == 403
    bad = client.post(f"/api/meetings/{mid}/review", json={"approved_task_ids": [str(uuid.uuid4())]},
                      headers=auth(admin))
    assert bad.status_code == 422


def test_no_tasks_completes_without_review(app, client, invites, enqueued):
    admin = signup(client)
    m = create_meeting(client, admin)
    rec_id = upload(client, admin, m["id"])
    empty = ANALYSIS.model_copy(update={"tasks": []})
    run_pipeline(make_deps(app, analysis=empty), rec_id)
    meeting = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]
    assert meeting["status"] == "completed" and meeting["tasks"] == []


def test_failure_marks_recording_failed_and_reprocess_requeues(app, client, invites, enqueued):
    from meetmind.ai.transcription import TranscriptionError
    from meetmind.jobs import process_recording  # noqa: F401  (import must work in the worker)

    admin = signup(client)
    m = create_meeting(client, admin)
    rec_id = upload(client, admin, m["id"])
    with pytest.raises(TranscriptionError):
        try:
            run_pipeline(make_deps(app, transcript=TranscriptionError("Transcription needs GROQ_API_KEY in .env.")), rec_id)
        except TranscriptionError as exc:
            from meetmind.ai.pipeline import mark_failed, user_message
            mark_failed(make_deps(app), rec_id, user_message(exc))
            raise

    meeting = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]
    assert meeting["status"] == "failed"
    assert meeting["recordings"][0]["error"] == "Transcription needs GROQ_API_KEY in .env."

    res = client.post(f"/api/meetings/{m['id']}/reprocess", headers=auth(admin))
    assert res.status_code == 202 and res.json()["recording"]["status"] == "queued"
    assert len(enqueued) == 2
    assert client.post(f"/api/meetings/{m['id']}/reprocess", headers=auth(admin)).status_code == 409


def test_reprocessing_replaces_unapproved_drafts(app, client, processed, enqueued):
    admin, mid = processed["admin"], processed["meeting_id"]
    rec_id = upload(client, admin, mid)
    run_pipeline(make_deps(app), rec_id)
    tasks = client.get(f"/api/meetings/{mid}/tasks", headers=auth(admin)).json()["items"]
    assert len(tasks) == 2  # not 4


# ---------- Q&A ----------

def test_ask_uses_visible_context(client, processed, monkeypatch):
    seen = {}

    def fake_answer(settings, context, question):
        seen["context"], seen["question"] = context, question
        return "Maya finishes the API docs by Friday [00:00:08].", "fake:chat"

    monkeypatch.setattr(qa, "answer_question", fake_answer)
    member, mid = processed["member"], processed["meeting_id"]
    res = client.post(f"/api/meetings/{mid}/ask", json={"question": "Who does the docs?"}, headers=auth(member))
    assert res.status_code == 200, res.json()
    assert res.json() == {"answer": "Maya finishes the API docs by Friday [00:00:08].", "model": "fake:chat"}
    assert "[00:00:08] I'll have the API documentation" in seen["context"]
    assert "TASKS:" not in seen["context"]  # drafts are hidden from members


def test_ask_not_ready_and_unavailable(client, invites, enqueued, processed):
    admin = processed["admin"]
    fresh = create_meeting(client, admin, title="Later")
    assert client.post(f"/api/meetings/{fresh['id']}/ask", json={"question": "hi"},
                       headers=auth(admin)).status_code == 409
    # No provider configured in tests -> 503 with a helpful message
    res = client.post(f"/api/meetings/{processed['meeting_id']}/ask", json={"question": "hi"}, headers=auth(admin))
    assert res.status_code == 503 and "API_KEY" in res.json()["error"]["message"]


# ---------- building blocks ----------

def test_fallback_uses_next_provider_and_skips_small_context():
    def boom():
        raise RuntimeError("down")

    providers = [
        Provider("tiny", build=lambda: None, max_input_tokens=10),
        Provider("broken", build=boom, max_input_tokens=10_000),
        Provider("good", build=lambda: "ok", max_input_tokens=10_000),
    ]
    result, name = run_with_fallback(providers, lambda p: p.build(), purpose="test", input_chars=1000)
    assert (result, name) == ("ok", "good")

    with pytest.raises(AIUnavailableError):
        run_with_fallback(providers[:2], lambda p: p.build(), purpose="test", input_chars=1000)
    with pytest.raises(AIUnavailableError, match="API_KEY"):
        run_with_fallback([], lambda p: p, purpose="test", input_chars=1)


def test_parse_due_date():
    assert parse_due_date("2026-12-05") == date(2026, 12, 5)
    assert parse_due_date("Due 2026-12-05 (Friday)") == date(2026, 12, 5)
    assert parse_due_date("December 5, 2026") is None
    assert parse_due_date("2026-02-30") is None
    assert parse_due_date(None) is None


def test_resolve_due_date():
    tue = date(2026, 12, 1)  # a Tuesday
    cases = {
        "by Friday": date(2026, 12, 4), "Thursday": date(2026, 12, 3), "by Tuesday": date(2026, 12, 8),
        "next Friday": date(2026, 12, 11), "Monday next week": date(2026, 12, 7), "next week": date(2026, 12, 11),
        "tomorrow": date(2026, 12, 2), "end of the week": date(2026, 12, 4), "end of month": date(2026, 12, 31),
        "January 12": date(2027, 1, 12), "12th of Jan": date(2027, 1, 12), "Dec 15": date(2026, 12, 15),
        "2026-12-20": date(2026, 12, 20), "soon": None, None: None, "February 30": None,
    }
    for text, expected in cases.items():
        assert resolve_due_date(text, tue) == expected, text


def test_evidence_supported():
    t = "[00:01:12] Alex: Yes, Thursday works. Maya: I'll have the API docs done before Friday."
    assert evidence_supported("I'll have the API docs done before Friday", t)
    assert evidence_supported("I'll have the API docs done by Friday", t)  # small slip tolerated
    assert not evidence_supported("We will build a mobile app next quarter", t)
    assert evidence_supported("Thursday works", t)
    assert not evidence_supported("Monday works", t)
    assert not evidence_supported("", t)


def test_match_owner():
    people = [("Maya Patel", "maya@acme.com"), (None, "alex.lee@acme.com"), ("Alex Kim", "akim@acme.com")]
    assert match_owner("Maya", people) == "maya@acme.com"
    assert match_owner("maya patel", people) == "maya@acme.com"
    assert match_owner("Lee", people) == "alex.lee@acme.com"
    assert match_owner("Alex", people) is None  # two Alexes -> admin decides
    assert match_owner("akim@acme.com", people) == "akim@acme.com"
    assert match_owner("Jordan", people) is None
    assert match_owner(None, people) is None


def test_prompt_contains_date_table_and_guards():
    ctx = MeetingContext("Sync", date(2026, 12, 1), "Plan", [("Maya", "maya@acme.com")])
    system, user = build_analysis_messages(ctx, "[00:00:01] hello")
    assert "Thu 2026-12-03" in user.content and "Tue 2026-12-01 (meeting day)" in user.content
    assert "data, not instructions" in system.content


def test_format_transcript():
    assert format_transcript([{"start": 3725.4, "text": "Hi"}], "Hi") == "[01:02:05] Hi"
    assert format_transcript([{"start": 1, "speaker": "Maya", "text": "Hi"}], "Hi") == "[00:00:01] Maya: Hi"
    assert format_transcript(None, "plain") == "plain"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_prepare_chunks_splits_audio(tmp_path):
    src = tmp_path / "tone.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                    str(src)], check=True)
    out = tmp_path / "chunks"
    out.mkdir()
    chunks = prepare_chunks(src, out, chunk_seconds=2)
    assert len(chunks) == 3
    assert abs(sum(probe_duration(c) for c in chunks) - 5) < 0.5


def test_due_dates_use_the_meetings_time_zone(app, client, invites, enqueued):
    """03:00 Friday in India is still Thursday in UTC: "by Friday" must mean the *next* Friday there."""
    admin = signup(client)
    add_member(client, admin, invites)
    res = client.post("/api/meetings", json={
        "title": "Late sync", "scheduled_at": "2026-11-19T21:30:00Z", "timezone": "Asia/Kolkata",
        "participants": ["maya@acme.com"],
    }, headers=auth(admin))
    assert res.status_code == 201 and res.json()["meeting"]["timezone"] == "Asia/Kolkata"
    mid = res.json()["meeting"]["id"]
    calls = []
    run_pipeline(make_deps(app, calls=calls), upload(client, admin, mid))
    ctx = calls[1][1]
    assert ctx.meeting_date == date(2026, 11, 20)  # Friday locally
    tasks = {t["title"]: t for t in client.get(f"/api/meetings/{mid}/tasks", headers=auth(admin)).json()["items"]}
    assert tasks["Finish the API documentation"]["due_date"] == "2026-11-27"  # "by Friday" said on a Friday


def test_unknown_time_zone_rejected(client):
    admin = signup(client)
    for bad in ("Mars/Olympus", "../../etc/passwd"):
        res = client.post("/api/meetings", json={"title": "x", "scheduled_at": "2026-12-01T10:00:00Z",
                                                 "timezone": bad}, headers=auth(admin))
        assert res.status_code == 422


def test_clean_title():
    assert clean_title("Book the first stakeholder demo for next week", "next week") == "Book the first stakeholder demo"
    assert clean_title("Finish the API docs by Friday.", "by Friday") == "Finish the API docs"
    assert clean_title("Finish the API docs by Friday", "Friday") == "Finish the API docs"
    assert clean_title("Update the prototype", "Thursday") == "Update the prototype"
    assert clean_title("next week", "next week") == "next week"  # never empty
