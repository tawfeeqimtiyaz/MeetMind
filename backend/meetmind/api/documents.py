"""Documents attached to a meeting: anyone who attends can add specs, documentation or bug reports,
and the AI assistant can answer questions from them."""
import logging
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from .. import task_queue
from ..deps import CurrentUser, DbSession, SettingsDep, get_rag
from ..documents import SUPPORTED_DOCUMENTS
from ..errors import APIError
from ..models import Document
from ..serializers import document_out
from .common import get_meeting_for
from .meetings import _UPLOAD_DOC, _copy_limited

router = APIRouter()
log = logging.getLogger(__name__)


def _get_document(db, user, document_id) -> Document:
    doc = db.get(Document, document_id)
    if doc is None:
        raise APIError(404, "not_found", "Document not found")
    get_meeting_for(db, user, doc.meeting_id)
    return doc


@router.get("/meetings/{meeting_id}/documents")
def list_documents(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession):
    meeting = get_meeting_for(db, user, meeting_id)
    return {"items": [document_out(d) for d in meeting.documents]}


@router.post("/meetings/{meeting_id}/documents", status_code=202, openapi_extra=_UPLOAD_DOC)
async def upload_document(meeting_id: uuid.UUID, request: Request, user: CurrentUser, db: DbSession,
                          settings: SettingsDep):
    """Attach a document (multipart field `file`). Text extraction + indexing run in the background."""
    meeting = await run_in_threadpool(get_meeting_for, db, user, meeting_id)
    limit = settings.max_document_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise APIError(413, "file_too_large", f"Documents can be at most {settings.max_document_mb} MB")

    async with request.form(max_files=1, max_fields=2) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise APIError(400, "no_file", "Attach the document as form field 'file'")
        name = PurePosixPath(upload.filename.replace("\\", "/")).name[:255]
        ext = PurePosixPath(name).suffix.lower()
        if ext not in SUPPORTED_DOCUMENTS:
            allowed = ", ".join(sorted(SUPPORTED_DOCUMENTS))
            raise APIError(400, "unsupported_file_type", f"Unsupported file type. Allowed: {allowed}")

        rel = Path(str(meeting.organization_id)) / str(meeting.id) / "documents" / f"{uuid.uuid4()}{ext}"
        dest = Path(settings.upload_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = await run_in_threadpool(_copy_limited, upload.file, dest, limit)
        if size == 0 or size > limit:
            dest.unlink(missing_ok=True)
            raise APIError(400 if size == 0 else 413, "empty_file" if size == 0 else "file_too_large",
                           "The file is empty" if size == 0 else f"Documents can be at most {settings.max_document_mb} MB")

        def save():
            doc = Document(meeting=meeting, uploaded_by_id=user.id, filename=name, storage_path=rel.as_posix(),
                           content_type=upload.content_type, size_bytes=size, status="queued")
            db.add(doc)
            db.commit()
            try:
                task_queue.enqueue(settings, "meetmind.jobs.process_document", str(doc.id))
            except Exception:
                log.exception("Could not queue document %s", doc.id)
                doc.status, doc.error = "failed", "Could not queue the document for processing."
                db.commit()
            db.refresh(doc)
            return doc

        doc = await run_in_threadpool(save)
    return {"document": document_out(doc)}


@router.get("/documents/{document_id}/download")
def download_document(document_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: SettingsDep):
    doc = _get_document(db, user, document_id)
    path = Path(settings.upload_dir) / doc.storage_path
    if not path.exists():
        raise APIError(404, "not_found", "File not found")
    return FileResponse(path, filename=doc.filename, media_type=doc.content_type or "application/octet-stream")


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: uuid.UUID, request: Request, user: CurrentUser, db: DbSession,
                    settings: SettingsDep):
    doc = _get_document(db, user, document_id)
    if doc.uploaded_by_id != user.id and user.role != "admin":
        raise APIError(403, "forbidden", "You can only delete documents you uploaded")
    path = Path(settings.upload_dir) / doc.storage_path
    db.delete(doc)
    db.commit()
    path.unlink(missing_ok=True)
    try:
        get_rag(request).remove_document(str(document_id))
    except Exception:  # noqa: BLE001
        log.warning("Could not remove document %s from the search index", document_id)
    return Response(status_code=204)
