"""External-system adapters. The email provider is SIMULATED: it writes to an outbox folder."""

from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, ProviderTransientError
from app.models import Task, TaskStatus, utcnow


class SimulatedEmailProvider:
    """Writes messages to an outbox folder. Nothing is delivered to a real mailbox."""

    name = "simulated"

    def __init__(self, outbox_dir: Path, fail_times: int = 0) -> None:
        self.outbox_dir = Path(outbox_dir)
        self.fail_times = fail_times  # test hook: simulate transient provider failures

    def send(self, *, to: str, subject: str, body: str) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderTransientError("Simulated provider timeout")
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        message_id = f"sim-{uuid4().hex[:12]}"
        content = (
            "SIMULATED DELIVERY: NOT SENT TO A REAL MAILBOX\n"
            f"Message-ID: {message_id}\nTo: {to}\nSubject: {subject}\n\n{body}\n"
        )
        (self.outbox_dir / f"{message_id}.txt").write_text(content, encoding="utf-8")
        return message_id


def task_view(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "assignee_id": task.assignee_id,
        "region_id": task.region_id,
        "priority": task.priority,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "status": task.status,
        "workflow_id": task.workflow_id,
    }


class DatabaseTaskProvider:
    """Task store backed by the local database. Swap for Jira/Trello/Asana adapters later."""

    name = "database"
    OPEN_STATES = (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)

    def create(
        self,
        session: Session,
        *,
        title: str,
        description: str,
        assignee_id: int | None,
        region_id: int | None,
        priority: str,
        due_date,
        workflow_id: str | None,
    ) -> dict:
        existing = session.scalar(select(Task).where(Task.title == title, Task.status.in_(self.OPEN_STATES)))
        if existing is not None:
            return {"task_id": existing.id, "title": existing.title, "status": existing.status, "duplicate": True}
        task = Task(
            title=title,
            description=description or "",
            assignee_id=assignee_id,
            region_id=region_id,
            priority=priority,
            due_date=due_date,
            status=TaskStatus.OPEN,
            workflow_id=workflow_id,
        )
        session.add(task)
        session.flush()
        return {"task_id": task.id, "title": task.title, "status": task.status, "duplicate": False}

    def update_status(self, session: Session, task_id: int, status: str) -> dict:
        task = session.get(Task, task_id)
        if task is None:
            raise NotFound("Task not found")
        task.status = status
        task.updated_at = utcnow()
        session.flush()
        return {"task_id": task.id, "status": task.status}

    def list_tasks(self, session: Session, status: str | None = None, region_id: int | None = None) -> list[dict]:
        stmt = select(Task).order_by(Task.due_date.is_(None), Task.due_date, Task.id)
        if status:
            stmt = stmt.where(Task.status == status)
        if region_id is not None:
            stmt = stmt.where(Task.region_id == region_id)
        return [task_view(t) for t in session.scalars(stmt)]


class SmtpEmailProvider:
    """Sends real email through an SMTP account (for example Gmail with an app password).

    The password is never written to messages, logs, or the database.
    """

    name = "smtp"

    def __init__(self, *, host: str, port: int, username: str | None, password: str | None,
                 sender: str, timeout: float = 20.0, use_tls: bool = True) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.timeout = timeout
        self.use_tls = use_tls

    @classmethod
    def from_settings(cls, settings) -> "SmtpEmailProvider":
        required = (("SMTP_USERNAME", settings.smtp_username),
                    ("SMTP_PASSWORD", settings.smtp_password),
                    ("SMTP_FROM", settings.smtp_from))
        missing = [name for name, value in required if not value]
        if missing:
            raise RuntimeError(f"EMAIL_PROVIDER=smtp needs {', '.join(missing)} set in .env")
        return cls(host=settings.smtp_host, port=settings.smtp_port, username=settings.smtp_username,
                   password=settings.smtp_password, sender=settings.smtp_from,
                   timeout=settings.smtp_timeout_seconds)

    def send(self, *, to: str, subject: str, body: str) -> str:
        domain = self.sender.split("@")[-1] if "@" in self.sender else "localhost"
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message_id = make_msgid(domain=domain)
        message["Message-ID"] = message_id
        message.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                server.ehlo()
                if self.use_tls:
                    server.starttls()
                    server.ehlo()
                if self.username:
                    server.login(self.username, self.password or "")
                server.send_message(message)
        # Order matters: SMTP errors are subclasses of OSError, so the specific ones come first.
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise ProviderTransientError(
                    f"Mail server temporarily refused the message (code {exc.smtp_code})"
                ) from None
            raise RuntimeError(
                f"Mail server rejected the message (code {exc.smtp_code}). Check the SMTP settings in .env."
            ) from None
        except smtplib.SMTPServerDisconnected:
            raise ProviderTransientError("The mail server closed the connection; it will be retried") from None
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"Mail sending failed ({type(exc).__name__})") from None
        except OSError as exc:
            raise ProviderTransientError(
                f"Could not reach the mail server ({type(exc).__name__}); it will be retried"
            ) from None
        return message_id


def build_email_provider(settings, outbox_dir: Path):
    choice = (settings.email_provider or "simulated").strip().lower()
    if choice == "smtp":
        return SmtpEmailProvider.from_settings(settings)
    if choice == "simulated":
        return SimulatedEmailProvider(outbox_dir)
    raise RuntimeError(f"Unknown EMAIL_PROVIDER '{choice}'. Use 'simulated' or 'smtp'.")
