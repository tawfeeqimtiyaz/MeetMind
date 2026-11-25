"""Slides: what was shown on the big screen. Anyone in the meeting can add a photo of the screen
(during or after the meeting); slides from screen recordings are added by the worker."""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import jwt
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from .. import task_queue
from ..config import Settings
from ..deps import CurrentUser, DbSession, SettingsDep
from ..errors import APIError
from ..models import Slide, User
from ..serializers import slide_out
from ..slides import slide_folder
from .common import get_meeting_for
from .meetings import _UPLOAD_DOC, _copy_limited

router = APIRouter()
log = logging.getLogger(__name__)

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def slide_token(settings: Settings, user: User, meeting_id) -> str:
    """One short-lived token for all slide images of a meeting (an <img> tag can't send the login header)."""
    payload = {"sub": str(user.id), "mtg": str(meeting_id), "typ": "slides",
               "exp": datetime.now(timezone.utc) + timedelta(hours=2)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _get_slide(db, user: User, slide_id: uuid.UUID) -> Slide:
    slide = db.get(Slide, slide_id)
    if slide is None:
        raise APIError(404, "not_found", "Slide not found")
    get_meeting_for(db, user, slide.meeting_id)  # 404 unless the user can see the meeting
    return slide


@router.get("/meetings/{meeting_id}/slides")
def list_slides(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: SettingsDep):
    meeting = get_meeting_for(db, user, meeting_id)
    token = slide_token(settings, user, meeting.id)
    ordered = sorted(meeting.slides, key=lambda s: (s.offset_seconds is None, s.offset_seconds or 0, s.created_at))
    return {"items": [slide_out(s, token) for s in ordered]}


@router.post("/meetings/{meeting_id}/slides", status_code=202, openapi_extra=_UPLOAD_DOC)
async def add_slide_photo(meeting_id: uuid.UUID, request: Request, user: CurrentUser, db: DbSession,
                          settings: SettingsDep):
    """A photo of the big screen (multipart field `file`, optional `offset_seconds`). Read in the background."""
    meeting = await run_in_threadpool(get_meeting_for, db, user, meeting_id)
    limit = settings.max_slide_image_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise APIError(413, "file_too_large", f"Photos can be at most {settings.max_slide_image_mb} MB")

    async with request.form(max_files=1, max_fields=3) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise APIError(400, "no_file", "Attach the photo as form field 'file'")
        ext = PurePosixPath(upload.filename.replace("\\", "/")).suffix.lower()
        if ext not in IMAGE_TYPES:
            raise APIError(400, "unsupported_file_type", "Use a JPG, PNG or WEBP photo")
        offset = form.get("offset_seconds")
        try:
            offset = float(offset) if offset not in (None, "") else None
        except (TypeError, ValueError):
            raise APIError(422, "validation_error", "offset_seconds must be a number")
        if offset is not None and not 0 <= offset <= 24 * 3600:
            raise APIError(422, "validation_error", "offset_seconds is out of range")

        rel = slide_folder(meeting) / f"{uuid.uuid4()}{ext}"
        dest = Path(settings.upload_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = await run_in_threadpool(_copy_limited, upload.file, dest, limit)
        if size == 0 or size > limit:
            dest.unlink(missing_ok=True)
            raise APIError(400 if size == 0 else 413, "empty_file" if size == 0 else "file_too_large",
                           "The photo is empty" if size == 0 else f"Photos can be at most {settings.max_slide_image_mb} MB")

        def save():
            nonlocal offset
            live = next((r for r in meeting.recordings if r.status == "recording"), None)
            if offset is None and live is not None:
                # sent from another device (e.g. a phone in the room) while recording: about now in the recording
                started = live.created_at if live.created_at.tzinfo else live.created_at.replace(tzinfo=timezone.utc)
                offset = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
            slide = Slide(meeting=meeting, added_by_id=user.id, source="photo", offset_seconds=offset,
                          storage_path=rel.as_posix(), status="queued")
            db.add(slide)
            db.commit()
            try:
                task_queue.enqueue(settings, "meetmind.jobs.process_slide", str(slide.id))
            except Exception:
                log.exception("Could not queue slide %s", slide.id)
                slide.status, slide.error = "failed", "Could not queue the photo for reading."
                db.commit()
            db.refresh(slide)
            return slide

        slide = await run_in_threadpool(save)
    return {"slide": slide_out(slide, slide_token(settings, user, meeting.id))}


@router.get("/slides/{slide_id}/image", include_in_schema=False)
def slide_image(slide_id: uuid.UUID, token: str, db: DbSession, settings: SettingsDep):
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise APIError(401, "invalid_token", "This image link has expired")
    if claims.get("typ") != "slides":
        raise APIError(401, "invalid_token", "Invalid image link")
    user = db.get(User, uuid.UUID(claims["sub"]))
    if user is None or user.status != "active":
        raise APIError(401, "unauthorized", "Account is not active")
    slide = _get_slide(db, user, slide_id)
    if claims.get("mtg") != str(slide.meeting_id):
        raise APIError(401, "invalid_token", "Invalid image link")
    path = Path(settings.upload_dir) / slide.storage_path
    if not path.exists():
        raise APIError(404, "not_found", "Image not found")
    media_type = IMAGE_TYPES.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/slides/{slide_id}", status_code=204)
def delete_slide(slide_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: SettingsDep):
    slide = _get_slide(db, user, slide_id)
    if slide.added_by_id != user.id and user.role != "admin":
        raise APIError(403, "forbidden", "You can only delete photos you added")
    meeting_id, path = str(slide.meeting_id), Path(settings.upload_dir) / slide.storage_path
    db.delete(slide)
    db.commit()
    path.unlink(missing_ok=True)
    try:
        task_queue.enqueue(settings, "meetmind.jobs.index_meeting", meeting_id)  # drop it from search
    except Exception:  # noqa: BLE001
        log.warning("Could not queue re-indexing after deleting slide %s", slide_id)
    return Response(status_code=204)
