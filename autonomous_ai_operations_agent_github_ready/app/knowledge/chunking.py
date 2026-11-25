"""Split extracted text into overlapping chunks that keep their page number.

Chunks are built from whole sentences, lines and table rows. The overlap between
neighbouring chunks repeats complete sentences, never partial ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SENTENCE_BREAK = re.compile(r"(?<=[.!?:])\s+|\n+")


@dataclass(frozen=True)
class Chunk:
    index: int
    page: int | None
    text: str


def _units(text: str) -> list[tuple[str, bool]]:
    """Return (unit, starts_new_line). A unit is a sentence, heading, or table row."""
    units: list[tuple[str, bool]] = []
    pos, newline = 0, False
    for match in _SENTENCE_BREAK.finditer(text):
        segment = text[pos:match.start()].strip()
        if segment:
            units.append((segment, newline))
        newline = "\n" in match.group(0)
        pos = match.end()
    tail = text[pos:].strip()
    if tail:
        units.append((tail, newline))
    return units


def split_sentences(text: str) -> list[str]:
    return [unit for unit, _ in _units(text)]


def _hard_split(text: str, limit: int) -> list[str]:
    """Break a single over-long sentence at word boundaries."""
    parts = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def _join(units: list[tuple[str, bool]]) -> str:
    out = ""
    for i, (piece, new_line) in enumerate(units):
        if i == 0:
            out = piece
        else:
            out += ("\n" if new_line else " ") + piece
    return out


def _tail_units(units: list[tuple[str, bool]], overlap: int) -> list[tuple[str, bool]]:
    """Trailing whole units whose combined length fits in the overlap budget."""
    tail: list[tuple[str, bool]] = []
    for piece, new_line in reversed(units):
        candidate = [(piece, new_line)] + tail
        if len(_join(candidate)) > overlap:
            break
        tail = candidate
    return [(tail[0][0], False)] + tail[1:] if tail else []


def chunk_pages(pages: list[tuple[int | None, str]], max_chars: int = 600, overlap: int = 120) -> list[Chunk]:
    if overlap >= max_chars // 2:
        raise ValueError("overlap must be less than half of max_chars")
    limit = max_chars - overlap  # tail (<= overlap) plus a new piece (<= limit) always fits in max_chars
    chunks: list[Chunk] = []
    index = 0
    for page, text in pages:
        units: list[tuple[str, bool]] = []
        for sentence, new_line in _units(text):
            for n, piece in enumerate(_hard_split(sentence, limit)):
                keeps_line = new_line and n == 0
                if units and len(_join(units + [(piece, keeps_line)])) > max_chars:
                    chunks.append(Chunk(index, page, _join(units)))
                    index += 1
                    units = _tail_units(units, overlap)
                units.append((piece, keeps_line if units else False))
        if units:
            chunks.append(Chunk(index, page, _join(units)))
            index += 1
    return chunks
