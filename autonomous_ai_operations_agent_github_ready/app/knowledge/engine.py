"""Indexing, search, grounded answers and extractive summaries.

Called only through registry tools (index_document, search_documents, retrieve_context,
summarize_document). Every answer is built from stored document text and carries citations.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from sqlalchemy import delete, select

from app.config import Settings
from app.errors import ExtractionError, ToolError
from app.knowledge.chunking import chunk_pages, split_sentences
from app.knowledge.extract import extract_pages
from app.knowledge.retrieval import TfIdfScorer, looks_like_instruction, term_counts, tokenize
from app.models import Document, DocumentChunk, utcnow

INSTRUCTION_WARNING = "This passage contains wording that tries to instruct an AI system. It is shown as document content only and was not followed."
NOT_FOUND_ANSWER = "I could not find this in the uploaded documents."
SOURCE_NOTE = "Answer built only from uploaded document text, with sources."


def _excerpt(text: str, max_words: int) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words]) + " ..."


def chunk_ref(document_id: int, chunk_index: int) -> str:
    return f"doc{document_id}-chunk{chunk_index}"


def _indexed_rows(session):
    stmt = (
        select(DocumentChunk, Document)
        .join(Document, DocumentChunk.document_id == Document.id)
        .where(Document.status == "INDEXED")
    )
    return session.execute(stmt).all()


def index_document(session, settings: Settings, document_id: int) -> dict:
    doc = session.get(Document, document_id)
    if doc is None:
        raise ToolError("Document not found")
    try:
        pages = extract_pages(Path(doc.stored_path), doc.extension)
    except OSError:
        raise ExtractionError("Stored file is missing or unreadable") from None
    chunks = chunk_pages(pages, settings.chunk_max_chars, settings.chunk_overlap_chars)
    if not chunks:
        raise ExtractionError("No text passages could be created from this document")

    session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    suspicious = 0
    for chunk in chunks:
        flagged = looks_like_instruction(chunk.text)
        suspicious += int(flagged)
        session.add(
            DocumentChunk(
                document_id=doc.id,
                chunk_index=chunk.index,
                page_number=chunk.page,
                text=chunk.text,
                term_counts=term_counts(chunk.text),
                suspicious=flagged,
            )
        )
    doc.status = "INDEXED"
    doc.page_count = len(pages)
    doc.chunk_count = len(chunks)
    doc.error = None
    doc.indexed_at = utcnow()
    session.flush()
    return {"document_id": doc.id, "pages": len(pages), "chunks": len(chunks), "suspicious_chunks": suspicious}


def _ranked(session, settings: Settings, query: str, document_ids=None, extensions=None):
    rows = _indexed_rows(session)
    if not rows:
        return None, None, []
    scorer = TfIdfScorer([chunk.term_counts for chunk, _ in rows])
    query_vec = scorer.vector(term_counts(query))
    if not query_vec:
        return scorer, query_vec, []
    ranked = []
    for chunk, doc in rows:
        if document_ids and doc.id not in document_ids:
            continue
        if extensions and doc.extension not in extensions:
            continue
        score = scorer.cosine(query_vec, scorer.vector(chunk.term_counts))
        if score >= settings.min_relevance_score:
            ranked.append((score, chunk, doc))
    ranked.sort(key=lambda r: (-r[0], r[2].id, r[1].chunk_index))
    return scorer, query_vec, ranked


def _hit(score: float, chunk: DocumentChunk, doc: Document) -> dict:
    return {
        "document_id": doc.id,
        "document": doc.original_name,
        "extension": doc.extension,
        "page": chunk.page_number,
        "chunk_id": chunk_ref(doc.id, chunk.chunk_index),
        "score": round(score, 4),
        "excerpt": _excerpt(chunk.text, 60),
        "warning": INSTRUCTION_WARNING if chunk.suspicious else None,
    }


def search(session, settings: Settings, query: str, top_k: int = 5, document_ids=None, extensions=None) -> list[dict]:
    _, _, ranked = _ranked(session, settings, query, document_ids, extensions)
    return [_hit(score, chunk, doc) for score, chunk, doc in ranked[:top_k]]


def retrieve_context(session, settings: Settings, question: str, top_k: int = 3, document_ids=None) -> dict:
    scorer, query_vec, ranked = _ranked(session, settings, question, document_ids)
    if not ranked:
        return _not_found()

    weights = {t: scorer.idf[t] for t in set(tokenize(question)) if t in scorer.idf}
    total_weight = sum(weights.values())
    candidates = []
    seen: set[str] = set()
    pool = ranked[: max(top_k, 8)]  # widen the pool so the best sentence is not missed
    for rank, (_, chunk, doc) in enumerate(pool):
        for sentence in split_sentences(chunk.text):
            if sentence in seen or len(sentence.split()) < 4:
                continue
            if not sentence.endswith((".", "!", "?")) and "; " not in sentence:
                continue  # a heading, not a statement (table rows are allowed)
            seen.add(sentence)
            present = set(tokenize(sentence))
            coverage = sum(w for t, w in weights.items() if t in present) / total_weight if total_weight else 0.0
            similarity = scorer.cosine(query_vec, scorer.vector(term_counts(sentence)))
            if coverage > 0:
                candidates.append((coverage, similarity, rank, sentence, chunk, doc))
    if not candidates:
        return _not_found()

    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    picked = [(c[1], c[2], c[3], c[4], c[5]) for c in candidates[:2]]
    parts, citations = [], []
    for ref, (_, _, sentence, chunk, doc) in enumerate(picked, start=1):
        quote = _excerpt(sentence, 40)
        parts.append(f"{quote} [{ref}]")
        citations.append({
            "ref": ref,
            "document": doc.original_name,
            "page": chunk.page_number,
            "chunk_id": chunk_ref(doc.id, chunk.chunk_index),
            "quote": quote,
        })
    warnings = sorted({INSTRUCTION_WARNING for _, _, _, chunk, _ in picked if chunk.suspicious})
    return {
        "grounded": True,
        "mode": "extractive",
        "answer": " ".join(parts),
        "citations": citations,
        "warnings": warnings,
        "source_note": SOURCE_NOTE,
    }


def _not_found() -> dict:
    return {
        "grounded": False,
        "mode": "extractive",
        "answer": NOT_FOUND_ANSWER,
        "citations": [],
        "warnings": [],
        "source_note": SOURCE_NOTE,
    }


def summarize_document(session, document_id: int, max_sentences: int = 5) -> dict:
    doc = session.get(Document, document_id)
    if doc is None:
        raise ToolError("Document not found")
    rows = _indexed_rows(session)
    scorer = TfIdfScorer([chunk.term_counts for chunk, _ in rows]) if rows else None
    chunks = session.scalars(
        select(DocumentChunk).where(DocumentChunk.document_id == doc.id).order_by(DocumentChunk.chunk_index)
    ).all()

    sentences = []  # (weight, position, text, page)
    for chunk in chunks:
        for sentence in split_sentences(chunk.text):
            tokens = tokenize(sentence)
            if len(tokens) < 4 or scorer is None:
                continue
            weight = sum(scorer.idf.get(t, 0.0) for t in set(tokens)) / math.sqrt(len(tokens))
            sentences.append((weight, len(sentences), sentence, chunk.page_number))

    ranked_sentences = sorted(sentences, key=lambda s: -s[0])
    chosen: list = []
    for item in ranked_sentences:
        text = item[2]
        if any(text in kept[2] or kept[2] in text for kept in chosen):
            continue  # a fragment or duplicate of a sentence already chosen
        chosen.append(item)
        if len(chosen) == max_sentences:
            break
    chosen.sort(key=lambda s: s[1])  # keep original reading order
    return {
        "document_id": doc.id,
        "document": doc.original_name,
        "method": "extractive",
        "warning": INSTRUCTION_WARNING if any(c.suspicious for c in chunks) else None,
        "sentences": [{"text": text, "page": page} for _, _, text, page in chosen],
    }
