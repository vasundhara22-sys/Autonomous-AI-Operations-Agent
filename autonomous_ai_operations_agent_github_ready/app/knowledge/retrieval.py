"""Lexical retrieval: TF-IDF weighting with cosine similarity, plus safety helpers.

This is deterministic and needs no model download. It matches words and their
inflections (light stemming). It does NOT understand synonyms; a sentence-embedding
model can be plugged in later behind the same scorer interface.
"""

from __future__ import annotations

import math
import re
from collections import Counter

TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by for from has have i in is it its of on or that the this to was were "
    "will with you your we our they their not no do does if so than then there these those into "
    "about over more most also can may what which who how when where why".split()
)
INSTRUCTION_PATTERN = re.compile(
    r"ignore (all |any |the )?(previous|prior|above) instructions|disregard (the )?(system|previous|above)"
    r"|you are now|system prompt|reveal (your|the) (instructions|prompt)|approve (all|every)",
    re.IGNORECASE,
)


def _stem(token: str) -> str:
    """Light, consistent normalisation so inflections of a word match each other."""
    if len(token) <= 3:
        return token
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            break
    if len(token) >= 2 and token[-1] == token[-2] and token[-1] not in "aeiouls":
        token = token[:-1]  # submitted -> submitt -> submit
    while token.endswith("e") and len(token) > 4:
        token = token[:-1]  # expense -> expens, employee -> employ, leave -> leav
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in TOKEN.findall(text.lower()) if len(t) > 1 and t not in STOPWORDS]


def term_counts(text: str) -> dict[str, int]:
    return dict(Counter(tokenize(text)))


def looks_like_instruction(text: str) -> bool:
    """Flags wording that tries to instruct an AI system. Flagged text is still treated as data."""
    return bool(INSTRUCTION_PATTERN.search(text))


class TfIdfScorer:
    """IDF is learned from the whole index, so common words carry little weight."""

    def __init__(self, all_chunk_counts: list[dict[str, int]]) -> None:
        n = len(all_chunk_counts)
        df: Counter = Counter()
        for counts in all_chunk_counts:
            df.update(counts.keys())
        self.idf = {term: math.log((n + 1) / (freq + 1)) + 1 for term, freq in df.items()}

    def vector(self, counts: dict[str, int]) -> dict[str, float]:
        weights = {t: (1 + math.log(c)) * self.idf[t] for t, c in counts.items() if t in self.idf}
        norm = math.sqrt(sum(w * w for w in weights.values()))
        return {t: w / norm for t, w in weights.items()} if norm else {}

    @staticmethod
    def cosine(query_vec: dict[str, float], doc_vec: dict[str, float]) -> float:
        return sum(weight * doc_vec.get(term, 0.0) for term, weight in query_vec.items())
