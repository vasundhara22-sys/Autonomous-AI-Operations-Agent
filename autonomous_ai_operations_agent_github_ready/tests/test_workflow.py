"""End-to-end workflow tests: planning, approvals, execution, idempotency, retries and recovery."""

import pytest
from sqlalchemy import func, select

from app.errors import ApprovalRequired, InvalidState, PermissionDenied
from app.models import EmailDraft, Region, Task, ToolExecution, Workflow, Approval
from app.tools.registry import ToolContext
from tests.conftest import SAMPLE_REQUEST


def _count(sf, model, *conditions):
    with sf() as s:
        return s.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def _outbox_files(env):
    outbox = env.settings.output_dir / "outbox"
    return list(outbox.glob("*.txt")) if outbox.exists() else []


def _step(wf, key):
    return next(s for s in wf["steps"] if s["step_key"] == key)


def test_full_workflow_stops_at_approval_gate(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)

    assert wf["status"] == "WAITING_FOR_APPROVAL"
    assert wf["plan"]["intent"] == "sales_performance_followup"
    assert all(s["status"] == "SUCCEEDED" for s in wf["steps"])

    metrics = _step(wf, "analyze")["output"]["metrics"]
    assert {r["region"] for r in metrics["underperformers"]} == {"South", "Central"}
    assert any(g["region"] == "Metro" for g in metrics["data_gaps"])

    kinds = sorted(r["kind"] for r in wf["reports"])
    assert kinds == ["csv", "pdf", "png", "xlsx"]

    actions = [a["action_type"] for a in wf["approvals"]]
    assert actions.count("create_task") == 2     # South and Central
    assert actions.count("send_email") == 1      # Central has no email on file, so no draft
    assert all(a["status"] == "PENDING" for a in wf["approvals"])

    # Nothing has been created or sent yet.
    assert _count(env.sf, Task) == 0
    assert _count(env.sf, EmailDraft) == 1
    assert _outbox_files(env) == []
    assert any("no email on file" in issue for issue in wf["summary"]["unresolved_issues"])


def test_approve_then_execute_creates_tasks_and_sends_email(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    env.service.approve_all(env.approver, wf["id"])
    wf = env.service.resume(env.approver, wf["id"])

    assert wf["status"] == "COMPLETED"
    assert _count(env.sf, Task) == 2
    outbox = list((env.settings.output_dir / "outbox").glob("*.txt"))
    assert len(outbox) == 1
    assert "SIMULATED DELIVERY" in outbox[0].read_text(encoding="utf-8")
    assert all(a["status"] == "EXECUTED" for a in wf["approvals"])


def test_resume_is_idempotent(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    env.service.approve_all(env.approver, wf["id"])
    env.service.resume(env.approver, wf["id"])
    env.service.resume(env.approver, wf["id"])
    env.service.resume(env.approver, wf["id"])

    assert _count(env.sf, Task) == 2
    assert len(list((env.settings.output_dir / "outbox").glob("*.txt"))) == 1


def test_editing_an_email_before_approval_changes_what_is_sent(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    email = next(a for a in wf["approvals"] if a["action_type"] == "send_email")
    env.service.decide(env.approver, email["id"], "approve", edits={"subject": "EDITED: South recovery plan"})
    for a in wf["approvals"]:
        if a["action_type"] == "create_task":
            env.service.decide(env.approver, a["id"], "approve")
    env.service.resume(env.approver, wf["id"])

    outbox = list((env.settings.output_dir / "outbox").glob("*.txt"))
    assert any("EDITED: South recovery plan" in p.read_text(encoding="utf-8") for p in outbox)


def test_rejected_email_is_never_sent(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    for a in wf["approvals"]:
        env.service.decide(env.approver, a["id"], "reject" if a["action_type"] == "send_email" else "approve")
    final = env.service.resume(env.approver, wf["id"])

    assert final["status"] == "COMPLETED"
    assert _outbox_files(env) == []
    assert _count(env.sf, Task) == 2
    assert final["summary"]["rejected_actions"]


def test_analyst_cannot_approve(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    approval_id = wf["approvals"][0]["id"]
    with pytest.raises(PermissionDenied):
        env.service.decide(env.analyst, approval_id, "approve")
    with pytest.raises(PermissionDenied):
        env.service.resume(env.analyst, wf["id"])


def test_viewer_cannot_start_workflow(env):
    with pytest.raises(PermissionDenied):
        env.service.start_workflow(env.viewer, SAMPLE_REQUEST)


def test_registry_blocks_external_action_without_approval(env):
    with env.sf() as s:
        ctx = ToolContext(session=s, settings=env.settings, agent="communication", workflow_id="none",
                          email_provider=env.email, approval=None)
        with pytest.raises(ApprovalRequired):
            env.service.runner.registry.invoke("send_email", {"draft_id": 1}, ctx)


def test_transient_provider_failure_is_retried(env):
    env.email.fail_times = 1  # first send attempt times out, the retry succeeds
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    env.service.approve_all(env.approver, wf["id"])
    final = env.service.resume(env.approver, wf["id"])

    assert final["status"] == "COMPLETED"
    with env.sf() as s:
        row = s.scalar(select(ToolExecution).where(ToolExecution.tool_name == "send_email"))
        assert row.status == "SUCCEEDED"
        assert row.attempts == 2


def test_interrupted_external_action_is_never_repeated_blindly(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    with env.sf() as s:
        south = s.scalar(select(Region).where(Region.name == "South"))
        # Simulate a crash mid-call: the row says RUNNING, so the outcome is unknown.
        s.add(ToolExecution(
            idempotency_key=f"{wf['id']}:create_task:region:{south.id}", workflow_id=wf["id"],
            tool_name="create_task", permission="internal_write", arguments={}, status="RUNNING", attempts=1,
        ))
        s.commit()

    env.service.approve_all(env.approver, wf["id"])
    final = env.service.resume(env.approver, wf["id"])

    assert final["status"] == "PARTIALLY_COMPLETED"
    south_approval = next(a for a in final["approvals"] if a["action_type"] == "create_task" and "South" in a["title"])
    assert south_approval["status"] == "FAILED"
    assert "verify" in south_approval["execution_error"].lower()
    assert _count(env.sf, Task, Task.title.like("%South%")) == 0


def test_unsupported_request_asks_for_clarification(env):
    wf = env.service.start_workflow(env.analyst, "Book a flight to Delhi for Monday")
    assert wf["status"] == "NEEDS_CLARIFICATION"
    assert "supported" in wf["clarification"].lower()
    assert wf["approvals"] == []


def test_missing_week_asks_for_clarification_with_available_weeks(env):
    wf = env.service.start_workflow(env.analyst, "Prepare a sales performance report for the week of 2026-01-05")
    assert wf["status"] == "NEEDS_CLARIFICATION"
    assert "Weeks with recorded data" in wf["clarification"]


def test_cancel_withdraws_pending_actions(env):
    wf = env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    cancelled = env.service.cancel(env.approver, wf["id"])
    assert cancelled["status"] == "CANCELLED"
    assert all(a["status"] == "CANCELLED" for a in cancelled["approvals"])
    with pytest.raises(InvalidState):
        env.service.decide(env.approver, wf["approvals"][0]["id"], "approve")


def test_manual_task_creation_is_deduplicated(env):
    data = {"title": "Quarterly pipeline review", "description": "", "priority": "High", "due_date": None}
    first = env.service.create_task(env.analyst, data)
    second = env.service.create_task(env.analyst, data)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["task_id"] == second["task_id"]


def test_logs_mask_email_addresses(env):
    env.service.start_workflow(env.analyst, SAMPLE_REQUEST)
    logs = env.service.list_logs(env.analyst, limit=500)
    blob = " ".join(str(row["data"]) + row["message"] for row in logs)
    assert "rahul.menon@example.com" not in blob
