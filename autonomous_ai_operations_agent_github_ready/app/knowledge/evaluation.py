"""Retrieval evaluation against labelled questions over the sample documents.

Definitions (k = number of results returned):
  precision@k = results from a relevant document / k
  recall@k    = relevant documents that appear in the top k / number of relevant documents
Results are chunks, so a relevant document can contribute several results.
"""

from __future__ import annotations

from pathlib import Path

from app import db
from app.auth import Role, authenticate, create_user
from app.config import Settings
from app.knowledge.extract import SUPPORTED
from app.knowledge.service import KnowledgeService

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = ROOT / "sample_docs"

GOLD = [
    ("How many days of annual leave do employees get?", {"leave_policy.txt"}),
    ("Who approves sick leave?", {"leave_policy.txt"}),
    ("When must expense receipts be submitted?", {"expense_policy.txt"}),
    ("How long does reimbursement take for travel expenses?", {"expense_policy.txt"}),
    ("What is the hotel limit per night?", {"expense_policy.txt"}),
    ("What is the password requirement?", {"it_security_policy.txt"}),
    ("How quickly must a data breach be reported?", {"it_security_policy.txt"}),
    ("What happens when a region is below target?", {"sales_recovery_playbook.txt"}),
    ("What is severe recovery?", {"sales_recovery_playbook.txt"}),
    ("Which region has the highest Q4 target?", {"regional_targets_2026.csv"}),
]


def run_evaluation(settings: Settings, k: int = 3, sample_dir: Path = SAMPLE_DIR) -> dict:
    db.configure(settings.database_url)
    with db.session_factory()() as s:
        token = create_user(s, "Evaluator", Role.ANALYST)
        principal = authenticate(s, token)
    kb = KnowledgeService(db.session_factory(), settings)
    for path in sorted(sample_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            kb.upload(principal, path.name, path.read_bytes())

    rows = []
    for query, relevant in GOLD:
        hits = kb.search(principal, query, top_k=k)["results"]
        relevant_hits = [h for h in hits if h["document"] in relevant]
        found = {h["document"] for h in relevant_hits}
        rows.append({
            "query": query,
            "relevant": sorted(relevant),
            "top_documents": [h["document"] for h in hits],
            "precision_at_k": round(len(relevant_hits) / k, 3),
            "recall_at_k": round(len(found) / len(relevant), 3),
        })
    return {
        "k": k,
        "queries": len(rows),
        "mean_precision_at_k": round(sum(r["precision_at_k"] for r in rows) / len(rows), 3),
        "mean_recall_at_k": round(sum(r["recall_at_k"] for r in rows) / len(rows), 3),
        "rows": rows,
    }
