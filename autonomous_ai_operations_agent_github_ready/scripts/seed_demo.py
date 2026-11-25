"""Create the database, sample reference data, and demo users.

Usage:
    python scripts/seed_demo.py            # first run
    python scripts/seed_demo.py --reset    # delete the SQLite file and start again

Tokens are generated randomly and printed ONCE. Only their SHA-256 hashes are stored.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app import db  # noqa: E402
from app.auth import create_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.seed import seed_reference_data  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if args.reset and settings.database_url.startswith("sqlite:///"):
        path = Path(settings.database_url.replace("sqlite:///", "", 1))
        if path.exists():
            path.unlink()
            print(f"Deleted {path}")

    db.configure(settings.database_url)
    with db.session_factory()() as s:
        created = seed_reference_data(s)
        print("Sample sales data:", "created" if created else "already present")
        if s.scalar(select(func.count()).select_from(User)) == 0:
            analyst = create_user(s, "Demo Analyst", Role.ANALYST)
            approver = create_user(s, "Demo Approver", Role.APPROVER)
            print("\nDemo API keys (shown once, store them safely):")
            print(f"  Analyst : {analyst}")
            print(f"  Approver: {approver}")
        else:
            print("Demo users already exist; use --reset to regenerate keys.")
    print(f"\nDatabase: {settings.database_url}")


if __name__ == "__main__":
    main()
