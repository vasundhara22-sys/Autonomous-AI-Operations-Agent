"""Leave updates and SMTP sending. The mail server is faked, so no real email is ever sent."""

import smtplib
from datetime import date

import pytest
from sqlalchemy import func, select

from app import providers
from app.config import Settings
from app.errors import PermissionDenied, ProviderTransientError
from app.leave.engine import leave_status
from app.leave.messages import compose_leave_update
from app.models import Approval
from app.providers import SmtpEmailProvider, SimulatedEmailProvider, build_email_provider
from app.seed import seed_leave_data
from app.workflow.leave_graph import LEAVE_INTENT
from app.workflow.service import WorkflowService

PASSWORD = "fake-app-password-1234"
MESSAGE = "Leave updates"


class FakeSMTP:
    connections: list = []
    fail_connect = False
    login_error = None

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.fail_connect:
            raise smtplib.SMTPServerDisconnected("connection unexpectedly closed")
        self.host, self.port = host, port
        self.tls = False
        self.logged_in = None
        self.sent = []
        FakeSMTP.connections.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self, *args):
        return (250, b"ok")

    def starttls(self, *args, **kwargs):
        self.tls = True
        return (220, b"ready")

    def login(self, user, password):
        if FakeSMTP.login_error:
            raise FakeSMTP.login_error
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)
        return {}


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.connections = []
    FakeSMTP.fail_connect = False
    FakeSMTP.login_error = None
    monkeypatch.setattr(providers.smtplib, "SMTP", FakeSMTP)
    yield FakeSMTP


def _smtp() -> SmtpEmailProvider:
    return SmtpEmailProvider(host="smtp.example.com", port=587, username="sender@example.com",
                             password=PASSWORD, sender="sender@example.com", timeout=5)


def _sent_messages():
    return [m for conn in FakeSMTP.connections for m in conn.sent]


def _seed(env):
    with env.sf() as s:
        seed_leave_data(s)  # dates are relative to today, which the workflow also uses


# ----- SMTP sending ------------------------------------------------------------------

def test_smtp_sends_with_tls_and_login():
    message_id = _smtp().send(to="priya@example.com", subject="Leave update", body="Hello")
    conn = FakeSMTP.connections[0]
    assert conn.tls is True
    assert conn.logged_in == ("sender@example.com", PASSWORD)
    message = conn.sent[0]
    assert message["To"] == "priya@example.com"
    assert message["From"] == "sender@example.com"
    assert message["Message-ID"] == message_id
    assert PASSWORD not in message.as_string()


def test_connection_drop_is_transient():
    FakeSMTP.fail_connect = True
    with pytest.raises(ProviderTransientError):
        _smtp().send(to="priya@example.com", subject="Leave update", body="Hello")


def test_wrong_password_is_reported_without_the_password():
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    with pytest.raises(RuntimeError) as info:
        _smtp().send(to="priya@example.com", subject="Leave update", body="Hello")
    assert "535" in str(info.value)
    assert PASSWORD not in str(info.value)


def test_smtp_needs_credentials_in_settings(tmp_path):
    settings = Settings(email_provider="smtp", smtp_username=None, smtp_password=None, smtp_from=None,
                        database_url=f"sqlite:///{tmp_path / 'x.db'}", output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="SMTP_USERNAME"):
        build_email_provider(settings, tmp_path / "outbox")


def test_default_provider_is_simulated(tmp_path):
    settings = Settings(email_provider="simulated", database_url=f"sqlite:///{tmp_path / 'x.db'}", output_dir=tmp_path)
    provider = build_email_provider(settings, tmp_path / "outbox")
    assert isinstance(provider, SimulatedEmailProvider)


# ----- leave rules and wording -------------------------------------------------------

def test_leave_status_finds_each_kind_of_update(env):
    _seed(env)
    today = date.today()
    with env.sf() as s:
        result = leave_status(s, today, low_balance_days=3.0, upcoming_days=7)
    by_name = {row["name"]: row for row in result["employees"]}
    assert by_name["Rahul Menon"]["pending"] and not by_name["Rahul Menon"]["upcoming"]
    assert by_name["Priya Nair"]["upcoming"]
    assert by_name["Karan Shah"]["low_balance"] is True
    assert by_name["Karan Shah"]["remaining_days"] == 2.0
    assert "Asha Verma" not in by_name  # nothing to report, so no update


def test_update_wording_states_the_balance():
    row = {"name": "Karan Shah", "pending": [], "upcoming": [], "remaining_days": 2.0, "low_balance": True}
    subject, body = compose_leave_update(row, "2026-11-24")
    assert "low leave balance" in subject
    assert "Annual leave remaining: 2 days." in body


# ----- workflow ----------------------------------------------------------------------

def test_leave_workflow_drafts_one_update_per_employee_and_flags_missing_email(env):
    _seed(env)
    wf = env.service.start_leave_workflow(env.analyst, "Send leave related updates to employees")
    assert wf["status"] == "WAITING_FOR_APPROVAL"
    assert wf["intent"] == LEAVE_INTENT
    assert len(wf["approvals"]) == 3  # Rahul, Priya, Karan
    assert any("Neha Joshi" in issue for issue in wf["summary"]["unresolved_issues"])
    assert _sent_messages() == []  # nothing sent before approval


def test_nothing_is_sent_without_approval(env):
    _seed(env)
    smtp_service = WorkflowService(env.sf, env.settings, email_provider=_smtp())
    wf = smtp_service.start_leave_workflow(env.analyst, "Send leave related updates to employees")
    final = smtp_service.resume(env.approver, wf["id"])  # resume with approvals still pending
    assert final["status"] == "WAITING_FOR_APPROVAL"
    assert FakeSMTP.connections == []


def test_approved_updates_are_sent_once_by_smtp(env):
    _seed(env)
    smtp_service = WorkflowService(env.sf, env.settings, email_provider=_smtp())
    wf = smtp_service.start_leave_workflow(env.analyst, "Send leave related updates to employees")
    smtp_service.approve_all(env.approver, wf["id"])
    final = smtp_service.resume(env.approver, wf["id"])

    assert final["status"] == "COMPLETED"
    assert sorted(m["To"] for m in _sent_messages()) == sorted(
        ["rahul.menon@example.com", "priya.nair@example.com", "karan.shah@example.com"]
    )
    smtp_service.resume(env.approver, wf["id"])  # running again must not resend
    assert len(_sent_messages()) == 3
    assert all(PASSWORD not in m.as_string() for m in _sent_messages())


def test_rejected_update_is_not_sent(env):
    _seed(env)
    smtp_service = WorkflowService(env.sf, env.settings, email_provider=_smtp())
    wf = smtp_service.start_leave_workflow(env.analyst, "Send leave related updates to employees")
    for approval in wf["approvals"]:
        decision = "reject" if "Karan" in approval["title"] else "approve"
        smtp_service.decide(env.approver, approval["id"], decision)
    final = smtp_service.resume(env.approver, wf["id"])
    assert final["status"] == "COMPLETED"
    recipients = [m["To"] for m in _sent_messages()]
    assert "karan.shah@example.com" not in recipients
    assert len(recipients) == 2


def test_simulated_mode_writes_outbox_files(env):
    _seed(env)
    wf = env.service.start_leave_workflow(env.analyst, "Send leave related updates to employees")
    env.service.approve_all(env.approver, wf["id"])
    env.service.resume(env.approver, wf["id"])
    outbox = env.settings.output_dir / "outbox"
    assert len(list(outbox.glob("*.txt"))) == 3


def test_viewer_and_analyst_permissions(env):
    _seed(env)
    with pytest.raises(PermissionDenied):
        env.service.start_leave_workflow(env.viewer, "Send leave related updates to employees")
    wf = env.service.start_leave_workflow(env.analyst, "Send leave related updates to employees")
    with pytest.raises(PermissionDenied):
        env.service.decide(env.analyst, wf["approvals"][0]["id"], "approve")


def test_leave_api(settings):
    from fastapi.testclient import TestClient
    from app import db
    from app.api import create_app
    from app.auth import create_user
    from app.models import Role
    from app.seed import seed_reference_data

    client = TestClient(create_app(settings))
    with db.session_factory()() as s:
        seed_reference_data(s)
        seed_leave_data(s)
        token = create_user(s, "Leave analyst", Role.ANALYST)
    response = client.post("/api/leave-workflows", headers={"X-API-Key": token},
                           json={"request": "Send leave related updates to employees"})
    assert response.status_code == 201
    assert response.json()["status"] == "WAITING_FOR_APPROVAL"
