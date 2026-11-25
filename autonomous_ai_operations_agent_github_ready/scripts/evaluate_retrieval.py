"""Print precision@k and recall@k for the labelled questions over sample_docs/.

Usage: python scripts/evaluate_retrieval.py [k]
Uses a temporary database, so it never touches your working data.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.knowledge.evaluation import run_evaluation  # noqa: E402


def main() -> None:
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(database_url=f"sqlite:///{tmp / 'eval.db'}", upload_dir=tmp / "uploads", output_dir=tmp / "out")
    report = run_evaluation(settings, k=k)
    print(f"{'question':<60} {'P@'+str(k):>6} {'R@'+str(k):>6}  top documents")
    for row in report["rows"]:
        print(f"{row['query'][:58]:<60} {row['precision_at_k']:>6.2f} {row['recall_at_k']:>6.2f}  {', '.join(dict.fromkeys(row['top_documents']))}")
    print(f"\nMean precision@{k}: {report['mean_precision_at_k']:.3f}   Mean recall@{k}: {report['mean_recall_at_k']:.3f}")
    print(f"Queries: {report['queries']} (small hand-labelled set; treat as a regression check, not a benchmark)")


if __name__ == "__main__":
    main()
