"""Slides: photos of the big screen, stills from screen recordings, the vision reader, search and decks."""
import subprocess
import threading
import uuid
from pathlib import Path

import pytest
from conftest import add_member, auth, create_meeting, signup
from test_ai import ANALYSIS, TRANSCRIPT

from meetmind import media
from meetmind.ai import agents
from meetmind.ai.pipeline import PipelineDeps, run_pipeline
from meetmind.ai.vision import SlideReading, parse_reading
from meetmind.models import Recording, Slide
from meetmind.slides import process_photo


# ---------- helpers ----------

def fake_reader(texts=None):
    """A vision model stand-in: returns the given readings in order (thread-safe), then repeats the last one."""
    lock, calls = threading.Lock(), []

    def read(image: bytes):
        with lock:
            calls.append(len(image))
            if texts is None:
                n = len(calls)
                return SlideReading(f"Slide {n}", f"Checkout errors 4.{n}% (goal under 1%)", True), "fake:vision"
            return texts[min(len(calls), len(texts)) - 1], "fake:vision"

    read.calls = calls
    return read


def make_image(path: Path, color="white") -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=640x360",
                    "-frames:v", "1", str(path)], check=True)
    return path


def make_presentation_video(path: Path) -> Path:
    """15 s: three 'slides' (red, blue, green screens) with a tone as the audio track."""
    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=5:r=10",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=5:r=10",
        "-f", "lavfi", "-i", "color=c=green:s=320x240:d=5:r=10",
        "-f", "lavfi", "-i", "sine=f=440:d=15",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def upload_photo(client, token, meeting_id, image: Path, **fields):
    with image.open("rb") as f:
        return client.post(f"/api/meetings/{meeting_id}/slides", files={"file": (image.name, f, "image/png")},
                           data={k: str(v) for k, v in fields.items()}, headers=auth(token))


# ---------- reading the model's answer ----------

def test_parse_reading_formats():
    r = parse_reading("TITLE: Q3 Revenue Target\nCONTENT:\nCheckout errors 4.2%\nE-4021 Card token expired")
    assert r.has_content and r.title == "Q3 Revenue Target" and "E-4021" in r.text
    assert not parse_reading("NO_CONTENT").has_content
    thinking = parse_reading("<think>Let me look…</think>\n**TITLE:** Roadmap\n**CONTENT:**\n- Beta: Dec 22")
    assert thinking.title == "Roadmap" and thinking.text == "- Beta: Dec 22"
    untitled = parse_reading("TITLE: NONE\nCONTENT:\nRevenue 1.2M")
    assert untitled.title is None and untitled.text == "Revenue 1.2M"
    assert parse_reading("Just some text on screen").text == "Just some text on screen"  # no labels


def test_same_template_slides_are_all_found(tmp_path):
    """Real decks share one template: only a few lines of text change, so the picture barely changes.
    Each still period must still count as a slide."""
    parts = []
    for i, text in enumerate(["Release plan - Dec 22", "Payment bug - E-4021 - 4.2 percent", "Owners - Maya and Alex"]):
        img = tmp_path / f"s{i}.png"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x0f172a:s=640x360",
                        "-frames:v", "1", "-vf", f"drawtext=text='{text}':fontsize=28:x=40:y=150:fontcolor=white", str(img)], check=True)
        parts += ["-loop", "1", "-t", "6", "-i", str(img)]
    video = tmp_path / "deck.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *parts, "-filter_complex",
                    "[0:v][1:v][2:v]concat=n=3:v=1:a=0,fps=5,format=yuv420p[v]", "-map", "[v]", "-c:v", "mpeg4", str(video)],
                   check=True)
    times = media.pick_still_times(media.still_periods(video), media.duration_seconds(video), min_gap=4, limit=60)
    assert len(times) == 3 and all(abs(t - want) <= 1 for t, want in zip(times, [1.5, 7.5, 13.5]))


def test_pick_slide_times_skips_quick_changes_and_caps():
    times = media.pick_slide_times([5.0, 6.0, 10.0, 30.0], duration=40, min_gap=4, limit=60)
    assert times == [1.0, 6.5, 11.5, 31.5]  # the change at 6 s is an animation step: skipped
    many = media.pick_slide_times([float(t) for t in range(5, 500, 5)], duration=500, min_gap=4, limit=10)
    assert len(many) == 10 and many[0] == 1.0


# ---------- photos of the big screen ----------

def test_photo_of_screen_is_read_shown_and_deleted(app, client, invites, enqueued, tmp_path, settings):
    admin = signup(client)
    member = add_member(client, admin, invites)  # maya@acme.com, a participant
    m = create_meeting(client, admin)

    res = upload_photo(client, member, m["id"], make_image(tmp_path / "screen.png"), offset_seconds=42)
    assert res.status_code == 202, res.json()
    slide = res.json()["slide"]
    assert slide["status"] == "queued" and slide["offset_seconds"] == 42 and slide["image_url"]
    assert ("meetmind.jobs.process_slide", (slide["id"],)) in enqueued

    reader = fake_reader([SlideReading("Q3 Revenue Target", "Checkout errors 4.2% -> under 1%", True)])
    assert process_photo(app.state.session_factory, settings, reader, slide["id"]) == "ready"
    shown = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]["slides"][0]
    assert shown["title"] == "Q3 Revenue Target" and shown["ai_model"] == "fake:vision" and shown["added_by"] == "Maya"

    img = client.get(shown["image_url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"  # photos are stored as JPEG
    assert client.get(f"/api/slides/{slide['id']}/image?token=nope").status_code == 401

    # members can't delete what others added; the author or an admin can
    other = upload_photo(client, admin, m["id"], make_image(tmp_path / "b.png")).json()["slide"]
    assert client.delete(f"/api/slides/{other['id']}", headers=auth(member)).status_code == 403
    assert client.delete(f"/api/slides/{slide['id']}", headers=auth(member)).status_code == 204
    assert ("meetmind.jobs.index_meeting", (m["id"],)) in enqueued


def test_photo_rules_and_access(client, invites, enqueued, tmp_path):
    admin = signup(client)
    member = add_member(client, admin, invites)
    hidden = create_meeting(client, admin, participants=["bob@acme.com"])
    image = make_image(tmp_path / "s.png")
    assert upload_photo(client, member, hidden["id"], image).status_code == 404  # not their meeting
    m = create_meeting(client, admin)
    with (tmp_path / "notes.txt").open("wb") as f:
        f.write(b"hello")
    with (tmp_path / "notes.txt").open("rb") as f:
        bad = client.post(f"/api/meetings/{m['id']}/slides", files={"file": ("notes.txt", f)}, headers=auth(admin))
    assert bad.status_code == 400
    assert upload_photo(client, admin, m["id"], image, offset_seconds="soon").status_code == 422

    other_org = signup(client, email="admin@globex.com", org="Globex")
    slide = upload_photo(client, admin, m["id"], image).json()["slide"]
    assert client.delete(f"/api/slides/{slide['id']}", headers=auth(other_org)).status_code == 404


def test_photo_during_live_recording_gets_its_moment(client, invites, enqueued, tmp_path):
    admin = signup(client)
    m = create_meeting(client, admin)
    client.post(f"/api/meetings/{m['id']}/live-recordings", json={"mode": "presentation", "mime_type": "video/webm"},
                headers=auth(admin))
    slide = upload_photo(client, admin, m["id"], make_image(tmp_path / "s.png")).json()["slide"]
    assert slide["offset_seconds"] is not None and 0 <= slide["offset_seconds"] < 60  # about "now" in the recording


def test_unreadable_photo_is_kept_with_a_message(app, client, invites, enqueued, tmp_path, settings):
    admin = signup(client)
    m = create_meeting(client, admin)
    slide = upload_photo(client, admin, m["id"], make_image(tmp_path / "blank.png")).json()["slide"]
    process_photo(app.state.session_factory, settings, fake_reader([SlideReading(None, "", False)]), slide["id"])
    shown = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]["slides"][0]
    assert shown["status"] == "ready" and "No slide content" in shown["error"]


# ---------- screen recordings ----------

@pytest.fixture
def presentation(app, client, invites, enqueued, tmp_path):
    """A meeting with an uploaded 15 s presentation video (three slides)."""
    admin = signup(client)
    m = create_meeting(client, admin)
    video = make_presentation_video(tmp_path / "talk.mp4")
    with video.open("rb") as f:
        res = client.post(f"/api/meetings/{m['id']}/recordings", files={"file": ("talk.mp4", f, "video/mp4")},
                          headers=auth(admin))
    assert res.status_code == 202, res.json()
    return {"admin": admin, "meeting_id": m["id"], "recording_id": res.json()["recording"]["id"]}


def test_screen_recording_slides_reach_analysis_and_search(app, client, presentation):
    contexts, stages = [], []
    reader = fake_reader()  # distinct text per slide

    def stage():
        with app.state.session_factory() as db:
            return db.get(Recording, uuid.UUID(presentation["recording_id"])).stage

    def read(image):
        stages.append(stage())
        return reader(image)

    def analyze(ctx, text):
        contexts.append(ctx)
        return ANALYSIS, "fake:model"

    deps = PipelineDeps(app.state.session_factory, app.state.settings, lambda p, h: TRANSCRIPT, analyze,
                        rag=lambda: app.state.rag, read_image=read)
    run_pipeline(deps, presentation["recording_id"])

    meeting = client.get(f"/api/meetings/{presentation['meeting_id']}", headers=auth(presentation["admin"])).json()["meeting"]
    slides = meeting["slides"]
    # one still per slide, 1.5 s after it appeared (allow a little for frame timing)
    assert len(slides) == 3
    assert all(abs(s["offset_seconds"] - want) <= 1 for s, want in zip(slides, [1.5, 6.5, 11.5]))
    assert all(s["source"] == "screen" and s["status"] == "ready" for s in slides)
    assert set(stages) == {"reading_slides"}
    assert len(contexts[0].slides) == 3 and contexts[0].slides[0].startswith("[00:00:0")
    assert meeting["recordings"][0]["stage"] is None

    hits = app.state.rag.search("checkout errors goal", str(_org(app, presentation)), None, kinds=["slide"])
    assert hits and hits[0].metadata["kind"] == "slide" and hits[0].metadata["slide_id"]


def test_repeated_slides_are_dropped_and_reprocessing_replaces_them(app, client, presentation):
    same = SlideReading("Roadmap", "Beta moves to Dec 22", True)
    deps = PipelineDeps(app.state.session_factory, app.state.settings, lambda p, h: TRANSCRIPT,
                        lambda ctx, text: (ANALYSIS, "fake:model"), read_image=fake_reader([same]))
    run_pipeline(deps, presentation["recording_id"])
    run_pipeline(deps, presentation["recording_id"])  # e.g. "Retry processing"
    with app.state.session_factory() as db:
        slides = db.query(Slide).all()
    assert len(slides) == 1  # three identical readings -> one slide, and no duplicates after re-processing


def test_slides_never_block_processing(app, client, presentation):
    def broken(image):
        raise RuntimeError("vision service down")

    deps = PipelineDeps(app.state.session_factory, app.state.settings, lambda p, h: TRANSCRIPT,
                        lambda ctx, text: (ANALYSIS, "fake:model"), read_image=broken)
    run_pipeline(deps, presentation["recording_id"])
    meeting = client.get(f"/api/meetings/{presentation['meeting_id']}", headers=auth(presentation["admin"])).json()["meeting"]
    assert meeting["status"] == "review"  # the meeting is processed even though no slide could be read


def test_agent_finds_slides_without_the_vector_index(app, client, presentation, settings):
    deps = PipelineDeps(app.state.session_factory, app.state.settings, lambda p, h: TRANSCRIPT,
                        lambda ctx, text: (ANALYSIS, "fake:model"), read_image=fake_reader())
    run_pipeline(deps, presentation["recording_id"])
    app.state.rag.store.items.clear()  # index empty or down: the tool searches the saved slide text
    from meetmind.models import User
    with app.state.session_factory() as db:
        scope = agents.scope_for(app.state.session_factory, db, settings, app.state.rag,
                                 db.query(User).filter_by(email="admin@acme.com").one(), presentation["meeting_id"])
    sources = agents._Sources()
    tools = {t.name: t for t in agents.build_tools(scope, sources, [])}
    found = tools["search_slides"].invoke({"query": "checkout errors goal"})
    assert "Checkout errors" in found and "shown at 00:00:0" in found
    first = sources.items[0][1]  # (key, source)
    assert first["kind"] == "slide" and first["slide_id"]


def _org(app, presentation):
    from meetmind.models import Meeting
    with app.state.session_factory() as db:
        return db.get(Meeting, uuid.UUID(presentation["meeting_id"])).organization_id


# ---------- PowerPoint decks ----------

def test_pptx_deck_is_read_slide_by_slide(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    from meetmind.documents import extract_pages

    deck = Presentation()
    s1 = deck.slides.add_slide(deck.slide_layouts[1])
    s1.shapes.title.text = "Q3 Revenue Target"
    s1.placeholders[1].text = "Checkout errors 4.2% -> under 1%"
    s1.notes_slide.notes_text_frame.text = "Mention the E-4021 bug"
    s2 = deck.slides.add_slide(deck.slide_layouts[5])
    s2.shapes.title.text = "Owners"
    table = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    table.cell(0, 0).text, table.cell(0, 1).text = "Task", "Owner"
    table.cell(1, 0).text, table.cell(1, 1).text = "Fix payment bug", "Maya"
    deck.save(tmp_path / "plan.pptx")

    pages = extract_pages(tmp_path / "plan.pptx")
    assert len(pages) == 2
    assert "Q3 Revenue Target" in pages[0] and "Speaker notes: Mention the E-4021 bug" in pages[0]
    assert "Fix payment bug | Maya" in pages[1]
