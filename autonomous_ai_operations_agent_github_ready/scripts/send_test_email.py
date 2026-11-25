"""Send one test email from your SMTP account to itself, to confirm the mail settings work.

Nothing is sent to anyone else. Usage: python scripts/send_test_email.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.providers import SmtpEmailProvider  # noqa: E402


def main() -> None:
    settings = get_settings()
    try:
        provider = SmtpEmailProvider.from_settings(settings)
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)
    try:
        message_id = provider.send(
            to=settings.smtp_from,
            subject="Ops Agent test email",
            body="If you can read this, the Ops Agent mail settings work.",
        )
    except Exception as exc:  # show a short, safe message instead of a traceback
        print(f"Send failed: {exc}")
        sys.exit(1)
    print(f"Sent a test email to {settings.smtp_from}. Message-ID: {message_id}")


if __name__ == "__main__":
    main()
