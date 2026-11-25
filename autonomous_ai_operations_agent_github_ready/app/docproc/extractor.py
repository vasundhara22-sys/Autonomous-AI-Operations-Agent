"""Document-type detection, field extraction and validation. Pure functions, no database.

Fields are read only from lines shaped like "Label: value". Nothing is guessed: a field
with no such line is reported as missing, and a value that fails its format check is
reported as invalid.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.docproc.schemas import DOCUMENT_TYPES, DocumentType, FieldSpec

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y")
_CURRENCY_PREFIX = re.compile(r"^(INR|USD|EUR|GBP|Rs\.?|₹|\$)\s*", re.IGNORECASE)
_MONEY = re.compile(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})?")
MIN_SCORE = 2  # signal words plus label matches needed before a type is accepted


def _valid_date(value: str) -> bool:
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value.strip(), fmt)
            return True
        except ValueError:
            continue
    return False


def _valid_money(value: str) -> bool:
    return bool(_MONEY.fullmatch(_CURRENCY_PREFIX.sub("", value.strip())))


def _check(kind: str, value: str) -> bool:
    if kind == "date":
        return _valid_date(value)
    if kind == "money":
        return _valid_money(value)
    return True


def _find_value(lines: list[str], aliases: tuple[str, ...]) -> str | None:
    """Return the text after the first matching "Label:" line. None if no line matches, "" if empty."""
    for alias in aliases:
        pattern = re.compile(rf"^\s*{re.escape(alias)}\s*[:\-]\s*(.*?)\s*$", re.IGNORECASE)
        for line in lines:
            match = pattern.match(line)
            if match:
                return match.group(1)
    return None


def extract_fields(text: str, doc_type: DocumentType) -> dict[str, dict]:
    lines = text.splitlines()
    fields: dict[str, dict] = {}
    for spec in doc_type.fields:
        raw = _find_value(lines, spec.aliases)
        if raw is None or not raw.strip():
            status, value = "missing", None
        else:
            value = raw.strip()
            status = "found" if _check(spec.kind, value) else "invalid"
        fields[spec.key] = {"label": spec.label, "value": value, "status": status, "required": spec.required}
    return fields


def _score(text_lower: str, lines: list[str], doc_type: DocumentType) -> int:
    signals = sum(1 for word in doc_type.signals if word in text_lower)
    labels = sum(1 for spec in doc_type.fields if _find_value(lines, spec.aliases) is not None)
    return signals + labels


def analyse_text(text: str) -> dict:
    lines = text.splitlines()
    lower = text.lower()
    best, best_score = None, 0
    for doc_type in DOCUMENT_TYPES:
        score = _score(lower, lines, doc_type)
        if score > best_score:
            best, best_score = doc_type, score

    if best is None or best_score < MIN_SCORE:
        return {
            "doc_type": None, "doc_type_label": None, "unknown": True, "score": best_score,
            "fields": {}, "missing": [], "missing_labels": [], "invalid": [], "invalid_labels": [],
        }

    fields = extract_fields(text, best)
    missing = [k for k, f in fields.items() if f["required"] and f["status"] == "missing"]
    invalid = [k for k, f in fields.items() if f["status"] == "invalid"]
    return {
        "doc_type": best.key,
        "doc_type_label": best.label,
        "unknown": False,
        "score": best_score,
        "fields": fields,
        "missing": missing,
        "missing_labels": [fields[k]["label"] for k in missing],
        "invalid": invalid,
        "invalid_labels": [fields[k]["label"] for k in invalid],
    }
