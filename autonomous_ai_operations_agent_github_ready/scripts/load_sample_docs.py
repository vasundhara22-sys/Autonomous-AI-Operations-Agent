"""Index the files in sample_docs/ so the Knowledge Base has content to search.

Usage: python scripts/load_sample_docs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app import db  # noqa: E402
from app.auth import Principal, create_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.knowledge.extract import SUPPORTED  # noqa: E402
from app.knowledge.service import KnowledgeService  # noqa: E402
from app.models import Role, User  # noqa: E402


def main() -> None:
    settings = get_settings()
    db.configure(settings.database_url)
    with db.session_factory()() as s:
        user = s.scalar(select(User).where(User.role == Role.ANALYST, User.is_active.is_(True)))
        if user is None:
            create_user(s, "Sample loader", Role.ANALYST)
            user = s.scalar(select(User).where(User.role == Role.ANALYST, User.is_active.is_(True)))
        principal = Principal(user.id, user.name, user.role)

    kb = KnowledgeService(db.session_factory(), settings)
    for path in sorted((ROOT / "sample_docs").rglob("*")):
        if path.suffix.lower() not in SUPPORTED:
            continue
        result = kb.upload(principal, path.name, path.read_bytes())
        note = "already loaded" if result["duplicate"] else "indexed"
        print(f"{path.name:<30} {result['status']:<8} {result['chunks']:>3} passages ({note})")


if __name__ == "__main__":
    main()
