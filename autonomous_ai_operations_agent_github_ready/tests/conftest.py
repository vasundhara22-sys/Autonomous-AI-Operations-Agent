from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db
from app.auth import create_user
from app.config import Settings
from app.models import Role
from app.providers import SimulatedEmailProvider
from app.seed import seed_reference_data
from app.workflow.service import WorkflowService

SAMPLE_REQUEST = (
    "Prepare a weekly sales performance report, identify underperforming regions, "
    "create follow-up tasks for the regional managers, and draft emails notifying them about their performance."
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        output_dir=tmp_path / "outputs",
        retry_base_delay_seconds=0.0,
        max_retries=3,
        llm_provider="deterministic",
        underperformance_threshold=0.80,
        severe_underperformance_threshold=0.60,
        task_due_days_after_period=7,
        upload_dir=tmp_path / 'uploads',
        email_provider='simulated',
    )


@pytest.fixture
def env(settings):
    """A fresh database with sample data, demo users, and a wired WorkflowService."""
    db.configure(settings.database_url)
    with db.session_factory()() as s:
        seed_reference_data(s)
        analyst_token = create_user(s, "Analyst", Role.ANALYST)
        approver_token = create_user(s, "Approver", Role.APPROVER)
        viewer_token = create_user(s, "Viewer", Role.VIEWER)
    email = SimulatedEmailProvider(settings.output_dir / "outbox")
    service = WorkflowService(db.session_factory(), settings, email_provider=email)
    return SimpleNamespace(
        settings=settings,
        service=service,
        sf=db.session_factory(),
        email=email,
        analyst=service.authenticate(analyst_token),
        approver=service.authenticate(approver_token),
        viewer=service.authenticate(viewer_token),
    )
