"""Recording meetings directly in the browser.

The browser (MediaRecorder) produces a piece of audio/video every few seconds and uploads each piece
right away. If the network drops, pieces wait in the browser and are re-sent; each piece is stored under
its number, so re-sending is harmless. When the meeting ends, `finish` checks that every piece arrived,
and the worker joins them into one file and runs the normal AI pipeline.
"""
import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import AdminUser, CurrentUser, DbSession, SettingsDep
from ..errors import APIError
from ..models import Meeting, Recording, User
from ..schemas import FinishRecordingIn, InstantMeetingIn, LiveRecordingIn, RecordingPatch
from ..serializers import meeting_out, recording_out
from .common import get_meeting_for
from .meetings import _queue_processing, _set_participants

router = APIRouter()
log = logging.getLogger(__name__)

BUSY = {"recording", "queued", "processing"}
EXT = {"audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "video/mp4": ".mp4"}


def parts_dir(settings: Settings, rec: Recording) -> Path:
    """Folder holding the numbered pieces of a live recording until it is finished."""
    return Path(settings.upload_dir) / Path(rec.storage_path).parent / f"live-{rec.id}"


def _get_recording(db: Session, user: User, recording_id: uuid.UUID) -> Recording:
    rec = db.get(Recording, recording_id)
    if rec is None:
        raise APIError(404, "not_found", "Recording not found")
    get_meeting_for(db, user, rec.meeting_id)  # 404 unless the user can see the meeting
    return rec


# ---------- instant meeting ----------

@router.post("/meetings/instant", status_code=201)
def start_instant_meeting(data: InstantMeetingIn, admin: AdminUser, db: DbSession):
    """Create a meeting that starts now, e.g. to record it immediately."""
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        organization_id=admin.organization_id,
        created_by_id=admin.id,
        title=data.title or f"Instant meeting {now:%d %b %Y %H:%M} UTC",
        agenda=data.agenda,
        scheduled_at=now,
        timezone=data.timezone,
    )
    _set_participants(db, meeting, data.participants)
    db.add(meeting)
    db.commit()
    return {"meeting": meeting_out(meeting, [])}


# ---------- live recording ----------

@router.post("/meetings/{meeting_id}/live-recordings", status_code=201)
def start_live_recording(meeting_id: uuid.UUID, data: LiveRecordingIn, admin: AdminUser, db: DbSession,
                         settings: SettingsDep):
    meeting = get_meeting_for(db, admin, meeting_id)
    if any(r.status in BUSY for r in meeting.recordings):
        raise APIError(409, "already_recording", "This meeting already has a recording in progress or processing")

    base_type = data.mime_type.split(";")[0].strip().lower()
    ext = EXT.get(base_type, ".webm")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    rec = Recording(
        meeting=meeting,
        uploaded_by_id=admin.id,
        original_filename=f"{data.mode}-recording-{stamp}{ext}",
        storage_path=(Path(str(meeting.organization_id)) / str(meeting.id) / f"{uuid.uuid4()}{ext}").as_posix(),
        content_type=base_type,
        size_bytes=0,
        status="recording",
        mode=data.mode,
    )
    db.add(rec)
    meeting.status = "recording"
    db.commit()
    parts_dir(settings, rec).mkdir(parents=True, exist_ok=True)
    return {"recording": recording_out(rec)}


def _write_piece(folder: Path, seq: int, body: bytes) -> None:
    tmp = folder / f"{seq:06d}.tmp"
    tmp.write_bytes(body)
    tmp.replace(folder / f"{seq:06d}.part")  # atomic: a half-written piece never counts


@router.put("/recordings/{recording_id}/chunks/{seq}", status_code=204)
async def upload_piece(recording_id: uuid.UUID, seq: int, request: Request, admin: AdminUser, db: DbSession,
                       settings: SettingsDep):
    """Store piece number `seq` (0, 1, 2, ...) of a live recording. Safe to repeat."""
    rec = await run_in_threadpool(_get_recording, db, admin, recording_id)
    if rec.status != "recording":
        raise APIError(409, "not_recording", "This recording has already been finished")
    if not 0 <= seq < 200_000:
        raise APIError(422, "validation_error", "Invalid piece number")
    limit = settings.live_chunk_max_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise APIError(413, "chunk_too_large", f"Each piece can be at most {settings.live_chunk_max_mb} MB")
    body = await request.body()
    if not body:
        raise APIError(400, "empty_chunk", "Empty piece")
    if len(body) > limit:
        raise APIError(413, "chunk_too_large", f"Each piece can be at most {settings.live_chunk_max_mb} MB")

    folder = parts_dir(settings, rec)
    total = sum(p.stat().st_size for p in folder.glob("*.part")) if folder.exists() else 0
    if total + len(body) > settings.max_live_recording_mb * 1024 * 1024:
        raise APIError(413, "recording_too_large", f"Recordings can be at most {settings.max_live_recording_mb} MB")
    folder.mkdir(parents=True, exist_ok=True)
    await run_in_threadpool(_write_piece, folder, seq, body)

    def bump():
        rec.chunks_received = max(rec.chunks_received or 0, seq + 1)
        db.commit()
    await run_in_threadpool(bump)
    return Response(status_code=204)


@router.post("/recordings/{recording_id}/finish", status_code=202)
def finish_live_recording(recording_id: uuid.UUID, data: FinishRecordingIn, admin: AdminUser, db: DbSession,
                          settings: SettingsDep):
    """All pieces sent: check none is missing, then join + process them in the background."""
    rec = _get_recording(db, admin, recording_id)
    if rec.status != "recording":
        raise APIError(409, "not_recording", "This recording has already been finished")
    folder = parts_dir(settings, rec)
    missing = [i for i in range(data.chunks) if not (folder / f"{i:06d}.part").exists()]
    if missing:
        # the browser re-sends these and calls finish again
        raise APIError(409, "missing_chunks", "Some pieces haven't arrived yet", {"missing": missing[:500]})

    rec.size_bytes = sum((folder / f"{i:06d}.part").stat().st_size for i in range(data.chunks))
    rec.chunks_received = data.chunks
    rec.duration_seconds = data.duration_seconds
    _queue_processing(db, settings, rec.meeting, rec)  # worker joins the pieces first, then runs the AI
    return {"recording": recording_out(rec)}


# ---------- managing recordings ----------

@router.patch("/recordings/{recording_id}")
def update_recording(recording_id: uuid.UUID, data: RecordingPatch, admin: AdminUser, db: DbSession):
    """Star a recording as important (it is highlighted in the admin's reminder emails)."""
    rec = _get_recording(db, admin, recording_id)
    rec.important = data.important
    db.commit()
    return {"recording": recording_out(rec)}


@router.delete("/recordings/{recording_id}", status_code=204)
def delete_recording(recording_id: uuid.UUID, admin: AdminUser, db: DbSession, settings: SettingsDep):
    """Delete a recording (or discard a live one). The transcript and AI results stay."""
    rec = _get_recording(db, admin, recording_id)
    if rec.status in {"queued", "processing"}:
        raise APIError(409, "busy", "Wait until processing has finished")
    meeting = rec.meeting
    folder, path = parts_dir(settings, rec), Path(settings.upload_dir) / rec.storage_path
    was_live = rec.status == "recording"
    db.delete(rec)
    if was_live and meeting.status == "recording":
        meeting.status = "completed" if meeting.summary else "scheduled"
    db.commit()
    shutil.rmtree(folder, ignore_errors=True)
    path.unlink(missing_ok=True)
    return Response(status_code=204)


# ---------- playback (an <audio>/<video> tag can't send the login header, so it gets a short-lived link) ----------

def _media_token(settings: Settings, user: User, rec: Recording) -> str:
    payload = {"sub": str(user.id), "rec": str(rec.id), "typ": "media",
               "exp": datetime.now(timezone.utc) + timedelta(hours=2)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


@router.get("/recordings/{recording_id}/playback")
def playback_link(recording_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: SettingsDep):
    rec = _get_recording(db, user, recording_id)
    if rec.status in {"recording"} or not (Path(settings.upload_dir) / rec.storage_path).exists():
        raise APIError(409, "not_ready", "This recording isn't ready to play yet")
    return {"url": f"/api/recordings/{rec.id}/media?token={_media_token(settings, user, rec)}",
            "content_type": rec.content_type}


@router.get("/recordings/{recording_id}/media", include_in_schema=False)
def media(recording_id: uuid.UUID, token: str, db: DbSession, settings: SettingsDep):
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise APIError(401, "invalid_token", "This playback link has expired")
    if claims.get("typ") != "media" or claims.get("rec") != str(recording_id):
        raise APIError(401, "invalid_token", "Invalid playback link")
    user = db.get(User, uuid.UUID(claims["sub"]))
    if user is None or user.status != "active":
        raise APIError(401, "unauthorized", "Account is not active")
    rec = _get_recording(db, user, recording_id)
    path = Path(settings.upload_dir) / rec.storage_path
    if not path.exists():
        raise APIError(404, "not_found", "Recording file not found")
    # FileResponse supports HTTP Range requests, so the player can jump to any timestamp
    return FileResponse(path, media_type=rec.content_type or "application/octet-stream")
