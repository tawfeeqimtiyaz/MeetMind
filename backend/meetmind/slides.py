"""Slides: what was shown on the big screen during a meeting.

Two ways in:
  * screen recording ("In the room + presentation" or "Online + screen", or an uploaded video):
    the worker finds each slide change and keeps one still per slide;
  * photo: someone snaps the big screen with a laptop or phone camera during (or after) the meeting.

Each slide is read by a vision model (ai/vision.py). The text then goes into the AI analysis (numbers and
names make decisions and tasks concrete) and into search.
"""
import difflib
import logging
import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import media
from .config import Settings
from .models import Meeting, Slide

log = logging.getLogger(__name__)

# read_image(bytes) -> (SlideReading, model name); injected so tests don't call real models
ReadImage = Callable[[bytes], tuple]

SIMILAR = 0.9  # slides whose text is this similar to the previous one are repeats (animation steps, re-shown slide)


def slide_folder(meeting: Meeting) -> Path:
    return Path(str(meeting.organization_id)) / str(meeting.id) / "slides"


def read_slide(session_factory, settings: Settings, read_image: ReadImage, slide_id: str) -> str:
    """Read one slide with the vision model. Returns the new status: ready | failed | empty | missing.
    'empty' = a screen frame with nothing on it (desktop, black screen): it is deleted."""
    from .ai.llm import AIUnavailableError

    with session_factory() as db:
        slide = db.get(Slide, uuid.UUID(slide_id))
        if slide is None:
            return "missing"
        path = Path(settings.upload_dir) / slide.storage_path
        try:
            reading, model = read_image(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - one unreadable slide must not stop the others
            if not isinstance(exc, AIUnavailableError):
                log.warning("Could not read slide %s", slide_id, exc_info=True)
            slide.status = "failed"
            slide.error = (str(exc) if isinstance(exc, AIUnavailableError)
                           else "The image file is missing." if isinstance(exc, OSError)
                           else "Could not read this slide right now.")
            db.commit()
            return "failed"
        if not reading.has_content:
            if slide.source == "screen":
                db.delete(slide)
                db.commit()
                path.unlink(missing_ok=True)
                return "empty"
            slide.status, slide.error, slide.ai_model = "ready", "No slide content could be read from this photo.", model
            slide.title, slide.text = None, ""
            db.commit()
            return "ready"
        slide.title, slide.text, slide.ai_model = reading.title, reading.text, model
        slide.status, slide.error = "ready", None
        db.commit()
        return "ready"


def _norm(slide: Slide) -> str:
    return re.sub(r"\W+", " ", f"{slide.title or ''} {slide.text or ''}".lower()).strip()


def _numbers(slide: Slide) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", f"{slide.title or ''} {slide.text or ''}"))


def _is_repeat(a: Slide, b: Slide) -> bool:
    """Same slide again: nearly the same words AND exactly the same numbers. A chart slide where one number
    changed is new information, so it is kept."""
    return _numbers(a) == _numbers(b) and difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio() >= SIMILAR


def drop_repeats(session_factory, settings: Settings, recording_id: str) -> int:
    """Delete screen slides that repeat the previous one (animation builds, going back to a slide).
    Returns how many slides were kept."""
    with session_factory() as db:
        slides = sorted(
            (s for s in db.query(Slide).filter(Slide.recording_id == uuid.UUID(recording_id), Slide.source == "screen")),
            key=lambda s: s.offset_seconds or 0)
        kept: list[Slide] = []
        for s in slides:
            if s.status != "ready":
                kept.append(s)
                continue
            if any(k.status == "ready" and _is_repeat(k, s) for k in kept[-3:]):
                (Path(settings.upload_dir) / s.storage_path).unlink(missing_ok=True)
                db.delete(s)
                continue
            kept.append(s)
        db.commit()
        return len(kept)


def extract_screen_slides(session_factory, settings: Settings, read_image: ReadImage, recording_id: str,
                          video: Path) -> int:
    """Take a still of every slide shown in a screen recording, read them, drop repeats. Returns the count."""
    from .models import Recording

    with session_factory() as db:
        rec = db.get(Recording, uuid.UUID(recording_id))
        meeting = rec.meeting
        # re-processing: replace the slides taken from this recording last time
        for old in [s for s in meeting.slides if s.recording_id == rec.id and s.source == "screen"]:
            (Path(settings.upload_dir) / old.storage_path).unlink(missing_ok=True)
            meeting.slides.remove(old)
        db.flush()
        folder = slide_folder(meeting)
        duration = media.duration_seconds(video)
        # a slide = the screen standing still; recordings that never stand still fall back to "big changes"
        periods = media.still_periods(video, min_seconds=2.0)
        if periods:
            times = media.pick_still_times(periods, duration, settings.slide_min_gap_seconds,
                                           settings.max_slides_per_recording)
            how = f"{len(periods)} still period(s)"
        else:
            changes = media.scene_changes(video, settings.slide_scene_threshold)
            times = media.pick_slide_times(changes, duration, settings.slide_min_gap_seconds,
                                           settings.max_slides_per_recording)
            how = f"{len(changes)} picture change(s)"
        ids = []
        for at in times:
            rel = folder / f"{uuid.uuid4()}.jpg"
            if not media.grab_frame(video, at, Path(settings.upload_dir) / rel):
                continue
            slide = Slide(meeting=meeting, recording_id=rec.id, source="screen", offset_seconds=at,
                          storage_path=rel.as_posix(), status="queued")
            db.add(slide)
            db.flush()
            ids.append(str(slide.id))
        db.commit()
    log.info("Recording %s: %s, %d still(s) to read", recording_id, how, len(ids))

    # read a few at a time: each call waits on the network, not the CPU
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda sid: read_slide(session_factory, settings, read_image, sid), ids))
    return drop_repeats(session_factory, settings, recording_id)


def prepare_photo(settings: Settings, slide: Slide) -> None:
    """Shrink a phone photo in place (as JPEG) before it is read and shown."""
    path = Path(settings.upload_dir) / slide.storage_path
    shrunk = path.with_name(path.stem + "-small.jpg")
    if media.normalize_image(path, shrunk):
        path.unlink(missing_ok=True)
        shrunk.replace(path.with_suffix(".jpg"))
        slide.storage_path = Path(slide.storage_path).with_suffix(".jpg").as_posix()


def process_photo(session_factory, settings: Settings, read_image: ReadImage, slide_id: str) -> str:
    """A photo of the big screen: shrink it, read it."""
    with session_factory() as db:
        slide = db.get(Slide, uuid.UUID(slide_id))
        if slide is None:
            return "missing"
        try:
            prepare_photo(settings, slide)
            db.commit()
        except Exception:  # noqa: BLE001 - reading the original is still worth a try
            log.warning("Could not shrink slide photo %s", slide_id, exc_info=True)
    return read_slide(session_factory, settings, read_image, slide_id)


def slide_lines(meeting: Meeting, max_chars: int = 12000) -> list[str]:
    """Slides for the AI analysis prompt: '[00:05:12] Title: text', in the order they were shown."""
    from .ai.transcription import format_timestamp

    out, used = [], 0
    ready = [s for s in meeting.slides if s.status == "ready" and (s.title or s.text)]
    for s in sorted(ready, key=lambda s: (s.offset_seconds is None, s.offset_seconds or 0)):
        at = f"[{format_timestamp(s.offset_seconds)}] " if s.offset_seconds is not None else ""
        line = f"{at}{s.title or 'Slide'}: {' / '.join(x.strip() for x in (s.text or '').splitlines() if x.strip())}"[:800]
        if used + len(line) > max_chars:
            break
        out.append(line)
        used += len(line)
    return out
