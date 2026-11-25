"""Knowledge-base service: upload, index, search, answer and summarise documents."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select

from app.auth import Principal
from app.config import Settings
from app.errors import InvalidState, NotFound, OpsError, PermissionDenied
from app.knowledge.extract import validate_upload
from app.logging_utils import log_event
from app.models import Document, Role
from app.tools.builtin import build_registry
from app.tools.registry import ToolContext

log = logging.getLogger("ops_agent.knowledge")
ANALYST_ROLES = (Role.ANALYST, Role.APPROVER)
ANY_ROLE = (Role.VIEWER, Role.ANALYST, Role.APPROVER)


def _require(principal: Principal, roles: tuple[str, ...]) -> None:
    if principal.role not in roles:
        raise PermissionDenied(f"Role '{principal.role}' is not allowed to perform this action")


def document_view(doc: Document) -> dict:
    return {
        "id": doc.id,
        "name": doc.original_name,
        "extension": doc.extension,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "pages": doc.page_count,
        "chunks": doc.chunk_count,
        "error": doc.error,
        "uploaded_at": doc.created_at.isoformat() if doc.created_at else None,
        "indexed_at": doc.indexed_at.isoformat() if doc.indexed_at else None,
    }


class KnowledgeService:
    def __init__(self, session_factory, settings: Settings) -> None:
        self.sf = session_factory
        self.settings = settings
        self.registry = build_registry()

    def _ctx(self, session) -> ToolContext:
        return ToolContext(session=session, settings=self.settings, agent="research",
                           workflow_id=None, email_provider=None)

    # ----- upload and indexing ----------------------------------------------------

    def upload(self, principal: Principal, filename: str | None, content: bytes) -> dict:
        _require(principal, ANALYST_ROLES)
        name, ext = validate_upload(filename, content, self.settings.max_upload_mb * 1024 * 1024)
        digest = hashlib.sha256(content).hexdigest()

        with self.sf() as s:
            existing = s.scalar(select(Document).where(Document.sha256 == digest))
            is_duplicate = existing is not None
            if existing is not None and existing.status == "INDEXED":
                return {**document_view(existing), "duplicate": True}
            if existing is None:
                folder = self.settings.upload_dir
                folder.mkdir(parents=True, exist_ok=True)
                stored = folder / f"{digest}{ext}"
                if not stored.exists():
                    stored.write_bytes(content)
                existing = Document(
                    original_name=name, stored_path=str(stored), extension=ext,
                    size_bytes=len(content), sha256=digest, status="PENDING", uploaded_by=principal.id,
                )
                s.add(existing)
                s.flush()
                log_event(s, workflow_id=None, agent="research",
                          message=f"Document uploaded by {principal.name}: {name}",
                          data={"document_id": existing.id, "size_bytes": len(content)})
                s.commit()
            doc_id = existing.id

        self._index(doc_id)
        with self.sf() as s:
            return {**document_view(s.get(Document, doc_id)), "duplicate": is_duplicate}

    def _index(self, doc_id: int) -> None:
        with self.sf() as s:
            try:
                self.registry.invoke("index_document", {"document_id": doc_id}, self._ctx(s))
            except Exception as exc:
                s.rollback()
                doc = s.get(Document, doc_id)
                doc.status = "FAILED"
                doc.error = (str(exc) if isinstance(exc, OpsError) else "Unexpected error while indexing")[:500]
                log_event(s, workflow_id=None, agent="research", level="ERROR",
                          message=f"Indexing failed for document {doc_id}", data={"error": doc.error})
                s.commit()

    # ----- reading ----------------------------------------------------------------

    def list_documents(self, principal: Principal) -> list[dict]:
        _require(principal, ANY_ROLE)
        with self.sf() as s:
            docs = s.scalars(select(Document).order_by(Document.created_at.desc(), Document.id.desc())).all()
            return [document_view(d) for d in docs]

    def get_document(self, principal: Principal, document_id: int) -> dict:
        _require(principal, ANY_ROLE)
        with self.sf() as s:
            doc = s.get(Document, document_id)
            if doc is None:
                raise NotFound("Document not found")
            return document_view(doc)

    # ----- search, answers, summaries (through the tool registry) -----------------

    def search(self, principal: Principal, query: str, top_k: int = 5, document_ids=None, extensions=None) -> dict:
        _require(principal, ANY_ROLE)
        with self.sf() as s:
            return self.registry.invoke(
                "search_documents",
                {"query": query, "top_k": top_k, "document_ids": document_ids, "extensions": extensions},
                self._ctx(s),
            )

    def answer(self, principal: Principal, question: str, top_k: int = 3, document_ids=None) -> dict:
        _require(principal, ANY_ROLE)
        with self.sf() as s:
            return self.registry.invoke(
                "retrieve_context", {"question": question, "top_k": top_k, "document_ids": document_ids}, self._ctx(s)
            )

    def summarize(self, principal: Principal, document_id: int, max_sentences: int = 5) -> dict:
        _require(principal, ANY_ROLE)
        with self.sf() as s:
            doc = s.get(Document, document_id)
            if doc is None:
                raise NotFound("Document not found")
            if doc.status != "INDEXED":
                raise InvalidState(f"Document is {doc.status}; only indexed documents can be summarised")
            return self.registry.invoke(
                "summarize_document", {"document_id": document_id, "max_sentences": max_sentences}, self._ctx(s)
            )
