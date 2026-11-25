"""Reading text out of attached documents (specs, documentation, bug reports, logs)."""
import html
import json
import re
from pathlib import Path

SUPPORTED_DOCUMENTS = frozenset({
    ".pdf", ".docx", ".pptx", ".md", ".markdown", ".txt", ".csv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".htm",
})


class DocumentError(RuntimeError):
    """Raised with a message that is safe to show users."""


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def _pptx_pages(path: Path) -> list[str]:
    """PowerPoint: the text of each slide (titles, text boxes, tables) plus the speaker notes."""
    from pptx import Presentation

    pages = []
    for slide in Presentation(str(path)).slides:
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"Speaker notes: {notes}")
        pages.append("\n".join(parts))
    return pages


def extract_pages(path: Path) -> list[str]:
    """Text per page (PDF) or one 'page' for other formats. Raises DocumentError if nothing readable."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            pages = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
        elif ext == ".docx":
            import docx

            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs]
            for table in d.tables:  # bug reports often keep steps/environments in tables
                for row in table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            pages = ["\n".join(parts)]
        elif ext == ".pptx":
            pages = _pptx_pages(path)  # one "page" per slide, so answers can cite "slide 7"
        else:
            raw = path.read_text(encoding="utf-8", errors="replace")
            if ext in {".html", ".htm", ".xml"}:
                raw = _html_to_text(raw)
            elif ext == ".json":
                try:
                    raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
                except ValueError:
                    pass
            pages = [raw]
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt or password-protected files
        raise DocumentError(f"Could not read this file ({type(exc).__name__}).") from exc

    pages = [re.sub(r"[ \t]+", " ", p).strip() for p in pages]
    if not any(pages):
        raise DocumentError("No readable text found. Scanned PDFs (images of text) aren't supported yet.")
    return pages


def process_document(session_factory, settings, rag, document_id: str) -> str:
    """Extract the text, save it, and add it to the search index.
    Returns the new status ('ready' | 'failed'); raises if only the indexing step failed (so it is retried)."""
    import uuid

    from .models import Document

    with session_factory() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if doc is None:
            return "missing"
        try:
            doc.pages = extract_pages(Path(settings.upload_dir) / doc.storage_path)
            doc.status, doc.error = "ready", None
        except DocumentError as exc:
            doc.status, doc.error = "failed", str(exc)
            db.commit()
            return "failed"
        db.commit()
        rag.index_document(doc.meeting, doc)
        return "ready"
