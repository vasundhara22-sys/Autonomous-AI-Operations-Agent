"""Upload validation and text extraction for TXT, MD, CSV, PDF and DOCX files.

Checks are done before any parser runs: extension allow-list, file-signature match,
size limit, and a cap on extracted text (guards against decompression bombs).
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from app.errors import ExtractionError, FileTooLarge, UploadRejected

SUPPORTED = {".txt", ".md", ".csv", ".pdf", ".docx"}
SIGNATURES = {".pdf": b"%PDF", ".docx": b"PK\x03\x04"}
MAX_TEXT_CHARS = 2_000_000
MAX_PAGES = 500
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def safe_filename(filename: str | None) -> str:
    name = Path(filename or "").name.strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    return name[:200] or "document"


def validate_upload(filename: str | None, content: bytes, max_bytes: int) -> tuple[str, str]:
    """Return (safe_name, extension) or raise UploadRejected / FileTooLarge."""
    name = safe_filename(filename)
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED:
        raise UploadRejected(f"Unsupported file type. Allowed: {', '.join(sorted(SUPPORTED))}")
    if not content:
        raise UploadRejected("File is empty")
    if len(content) > max_bytes:
        raise FileTooLarge(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit")
    signature = SIGNATURES.get(ext)
    if signature and not content.startswith(signature):
        raise UploadRejected("File contents do not match its extension")
    return name, ext


def clean_text(text: str) -> str:
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pages(path: Path, ext: str) -> list[tuple[int | None, str]]:
    """Return a list of (page_number or None, text)."""
    if ext in (".txt", ".md"):
        pages = [(None, path.read_text(encoding="utf-8", errors="replace"))]
    elif ext == ".csv":
        pages = [(None, _csv_to_text(path))]
    elif ext == ".pdf":
        pages = _pdf_pages(path)
    elif ext == ".docx":
        pages = [(None, _docx_text(path))]
    else:
        raise UploadRejected("Unsupported file type")

    cleaned = [(p, clean_text(t)) for p, t in pages]
    cleaned = [(p, t) for p, t in cleaned if t]
    if not cleaned:
        raise ExtractionError("No readable text found (scanned images need OCR, which is not included)")
    if sum(len(t) for _, t in cleaned) > MAX_TEXT_CHARS:
        raise ExtractionError("Document contains too much text")
    return cleaned


def _csv_to_text(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return ""
    header, body = rows[0], rows[1:]
    lines = ["; ".join(f"{h}: {v}" for h, v in zip(header, row)) for row in body]
    return "\n".join(lines)


def _pdf_pages(path: Path) -> list[tuple[int | None, str]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if len(reader.pages) > MAX_PAGES:
        raise ExtractionError(f"PDF has more than {MAX_PAGES} pages")
    return [(i + 1, _unwrap(page.extract_text() or "")) for i, page in enumerate(reader.pages)]


def _unwrap(text: str) -> str:
    """Join a single line break that falls mid-sentence; keep breaks after sentence ends and blank lines."""
    return re.sub(r"(?<![.!?:\n])\n(?=\S)", " ", text)


def _docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)
