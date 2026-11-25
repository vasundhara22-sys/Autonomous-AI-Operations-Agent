"""Tool-level entry point: read a stored document and analyse it. Returns analysis only, never the full text."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.docproc.extractor import analyse_text
from app.errors import ExtractionError, ToolError
from app.knowledge.extract import extract_pages
from app.models import Document


def extract_document_fields(session, settings: Settings, document_id: int) -> dict:
    doc = session.get(Document, document_id)
    if doc is None:
        raise ToolError("Document not found")
    if doc.status != "INDEXED":
        raise ToolError(f"Document is {doc.status}; only indexed documents can be analysed")
    try:
        pages = extract_pages(Path(doc.stored_path), doc.extension)
    except OSError:
        raise ExtractionError("Stored file is missing or unreadable") from None
    text = "\n".join(page_text for _, page_text in pages)
    return {"document_id": doc.id, "name": doc.original_name, "pages": len(pages), **analyse_text(text)}
