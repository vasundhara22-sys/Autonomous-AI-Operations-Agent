"""Application service: the single entry point for the API and dashboard.

Authorisation, approval enforcement, execution and status bookkeeping live here.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app import repository as repo
from app.auth import Principal, authenticate
from app.config import Settings
from app.errors import InvalidInput, InvalidState, NotFound, OpsError, PermissionDenied, Unauthenticated
from app.llm import IntentService, build_intent_service
from app.logging_utils import log_event
from app.models import (
    Approval, ApprovalStatus, DraftStatus, EmailDraft, ExecutionLog, Report, Role, StepStatus,
    Task, TaskStatus, Workflow, WorkflowStatus, WorkflowStep, utcnow,
)
from app.providers import DatabaseTaskProvider, SimulatedEmailProvider, build_email_provider
from app.tools.registry import ToolContext
from app.workflow.document_graph import DOCUMENT_INTENT, DocumentWorkflowRunner
from app.workflow.leave_graph import LEAVE_INTENT, LeaveWorkflowRunner
from app.workflow.graph import WorkflowRunner

log = logging.getLogger("ops_agent.service")

ANALYST_ROLES = (Role.ANALYST, Role.APPROVER)
APPROVER_ROLES = (Role.APPROVER,)
ANY_ROLE = (Role.VIEWER, Role.ANALYST, Role.APPROVER)
AGENT_FOR_ACTION = {"create_task": "task_management", "send_email": "communication"}
TERMINAL_STATUSES = (
    WorkflowStatus.COMPLETED, WorkflowStatus.PARTIALLY_COMPLETED, WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED, WorkflowStatus.NEEDS_CLARIFICATION,
)


def _require(principal: Principal, roles: tuple[str, ...]) -> None:
    if principal.role not in roles:
        raise PermissionDenied(f"Role '{principal.role}' is not allowed to perform this action")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


class WorkflowService:
    def __init__(
        self,
        session_factory,
        settings: Settings,
        email_provider=None,
        intent_service: IntentService | None = None,
    ) -> None:
        self.sf = session_factory
        self.settings = settings
        self.email = email_provider or build_email_provider(settings, settings.output_dir / "outbox")
        self.runner = WorkflowRunner(
            session_factory, settings, self.email, intent_service or build_intent_service(settings)
        )
        self.doc_runner = DocumentWorkflowRunner(session_factory, settings, self.email)
        self.leave_runner = LeaveWorkflowRunner(session_factory, settings, self.email)

    # ----- auth and health --------------------------------------------------------

    def authenticate(self, token: str) -> Principal:
        with self.sf() as s:
            principal = authenticate(s, token)
        if principal is None:
            raise Unauthenticated("Invalid API key")
        return principal

    def health(self) -> dict:
        with self.sf() as s:
            s.execute(select(1))
        return {
            "status": "ok",
            "database": "ok",
            "llm_provider": self.settings.llm_provider,
            "email_provider": self.email.name,
        }

    # ----- workflows --------------------------------------------------------------

    def start_workflow(self, principal: Principal, request_text: str) -> dict:
        _require(principal, ANALYST_ROLES)
        text = (request_text or "").strip()
        if len(text) < 5:
            raise InvalidInput("Request is too short")
        if len(text) > self.settings.max_request_chars:
            raise InvalidInput(f"Request exceeds {self.settings.max_request_chars} characters")

        wid = str(uuid4())
        with self.sf() as s:
            s.add(Workflow(id=wid, user_request=text, status=WorkflowStatus.PLANNING, created_by=principal.id))
            log_event(s, workflow_id=wid, agent="orchestrator", message=f"Workflow created by {principal.name}")
            s.commit()

        try:
            final = self.runner.graph.invoke({"workflow_id": wid, "request": text, "status": WorkflowStatus.PLANNING})
        except Exception:
            log.exception("Graph execution crashed for workflow %s", wid)
            final = {"status": WorkflowStatus.FAILED, "error": "Unexpected internal error; see execution logs."}
        self._finalise(wid, final)
        return self.get_workflow(principal, wid)

    def start_document_workflow(self, principal: Principal, request_text: str, document_ids: list[int]) -> dict:
        _require(principal, ANALYST_ROLES)
        text = (request_text or "").strip()
        if len(text) < 5:
            raise InvalidInput("Request is too short")
        if len(text) > self.settings.max_request_chars:
            raise InvalidInput(f"Request exceeds {self.settings.max_request_chars} characters")
        ids = sorted({int(i) for i in document_ids})

        wid = str(uuid4())
        with self.sf() as s:
            s.add(Workflow(id=wid, user_request=text, status=WorkflowStatus.PLANNING,
                           intent=DOCUMENT_INTENT, created_by=principal.id))
            log_event(s, workflow_id=wid, agent="orchestrator", message=f"Document workflow created by {principal.name}")
            s.commit()
        try:
            final = self.doc_runner.graph.invoke({
                "workflow_id": wid, "request": text, "status": WorkflowStatus.PLANNING, "document_ids": ids,
            })
        except Exception:
            log.exception("Document graph crashed for workflow %s", wid)
            final = {"status": WorkflowStatus.FAILED, "error": "Unexpected internal error; see execution logs."}
        self._finalise(wid, final)
        return self.get_workflow(principal, wid)

    def start_leave_workflow(self, principal: Principal, request_text: str) -> dict:
        _require(principal, ANALYST_ROLES)
        text = (request_text or "").strip()
        if len(text) < 5:
            raise InvalidInput("Request is too short")
        if len(text) > self.settings.max_request_chars:
            raise InvalidInput(f"Request exceeds {self.settings.max_request_chars} characters")

        wid = str(uuid4())
        with self.sf() as s:
            s.add(Workflow(id=wid, user_request=text, status=WorkflowStatus.PLANNING,
                           intent=LEAVE_INTENT, created_by=principal.id))
            log_event(s, workflow_id=wid, agent="orchestrator", message=f"Leave workflow created by {principal.name}")
            s.commit()
        try:
            final = self.leave_runner.graph.invoke({"workflow_id": wid, "request": text, "status": WorkflowStatus.PLANNING})
        except Exception:
            log.exception("Leave graph crashed for workflow %s", wid)
            final = {"status": WorkflowStatus.FAILED, "error": "Unexpected internal error; see execution logs."}
        self._finalise(wid, final)
        return self.get_workflow(principal, wid)

    def _finalise(self, wid: str, final: dict) -> None:
        with self.sf() as s:
            wf = s.get(Workflow, wid)
            wf.status = final.get("status", WorkflowStatus.FAILED)
            if final.get("intent"):
                wf.intent = final["intent"]
            if final.get("plan"):
                wf.plan = final["plan"]
            wf.clarification = final.get("clarification")
            wf.error = final.get("error")
            wf.updated_at = utcnow()
            if wf.status in TERMINAL_STATUSES:
                wf.completed_at = utcnow()
            s.flush()
            wf.summary = build_summary(s, wf)
            log_event(s, workflow_id=wid, agent="orchestrator", message=f"Workflow status: {wf.status}")
            s.commit()

    def list_workflows(self, principal: Principal, limit: int = 50) -> list[dict]:
        with self.sf() as s:
            rows = s.scalars(select(Workflow).order_by(Workflow.created_at.desc()).limit(limit)).all()
            return [self._workflow_brief(w) for w in rows]

    def get_workflow(self, principal: Principal, wid: str) -> dict:
        with self.sf() as s:
            wf = s.get(Workflow, wid)
            if wf is None:
                raise NotFound("Workflow not found")
            steps = s.scalars(select(WorkflowStep).where(WorkflowStep.workflow_id == wid).order_by(WorkflowStep.id)).all()
            approvals = s.scalars(select(Approval).where(Approval.workflow_id == wid).order_by(Approval.id)).all()
            reports = s.scalars(select(Report).where(Report.workflow_id == wid).order_by(Report.id)).all()
            return {
                **self._workflow_brief(wf),
                "plan": wf.plan,
                "summary": wf.summary,
                "steps": [
                    {
                        "step_key": st.step_key, "agent": st.agent, "title": st.title, "status": st.status,
                        "attempts": st.attempts, "duration_ms": round(st.duration_ms or 0, 1),
                        "error": st.error, "output": st.output_json,
                    }
                    for st in steps
                ],
                "approvals": [self._approval_view(s, a) for a in approvals],
                "reports": [self._report_view(r) for r in reports],
            }

    @staticmethod
    def _workflow_brief(wf: Workflow) -> dict:
        return {
            "id": wf.id,
            "request": wf.user_request,
            "status": wf.status,
            "intent": wf.intent,
            "clarification": wf.clarification,
            "error": wf.error,
            "created_at": _iso(wf.created_at),
            "updated_at": _iso(wf.updated_at),
            "completed_at": _iso(wf.completed_at),
        }

    def resume(self, principal: Principal, wid: str) -> dict:
        """Retry failed steps from checkpoints, then execute approved actions."""
        _require(principal, APPROVER_ROLES)
        with self.sf() as s:
            wf = s.get(Workflow, wid)
            if wf is None:
                raise NotFound("Workflow not found")
            if wf.status == WorkflowStatus.CANCELLED:
                raise InvalidState("Workflow was cancelled")
            request_text = wf.user_request
            intent = wf.intent
            doc_ids = list((wf.plan or {}).get("document_ids", []))
            has_failed_steps = s.scalar(
                select(WorkflowStep.id).where(WorkflowStep.workflow_id == wid, WorkflowStep.status == StepStatus.FAILED)
            ) is not None

        if has_failed_steps:
            payload = {"workflow_id": wid, "request": request_text, "status": WorkflowStatus.PLANNING}
            runner = {DOCUMENT_INTENT: self.doc_runner, LEAVE_INTENT: self.leave_runner}.get(intent, self.runner)
            if intent == DOCUMENT_INTENT:
                payload["document_ids"] = doc_ids
            final = runner.graph.invoke(payload)
            self._finalise(wid, final)

        for approval_id in self._approved_ids(wid):
            self._execute_one(wid, approval_id)
        self._refresh_status(wid)
        return self.get_workflow(principal, wid)

    def _approved_ids(self, wid: str) -> list[int]:
        with self.sf() as s:
            return list(s.scalars(
                select(Approval.id).where(
                    Approval.workflow_id == wid, Approval.status.in_([ApprovalStatus.APPROVED, ApprovalStatus.FAILED])
                ).order_by(Approval.id)
            ))

    def _execute_one(self, wid: str, approval_id: int) -> None:
        with self.sf() as s:
            ap = s.get(Approval, approval_id)
            if ap.status == ApprovalStatus.FAILED:
                ap.status = ApprovalStatus.APPROVED  # explicit retry of a failed, already-approved action
                s.commit()
            tool, args, key = self._action_call(ap)
            ctx = ToolContext(
                session=s,
                settings=self.settings,
                agent=AGENT_FOR_ACTION[ap.action_type],
                workflow_id=wid,
                email_provider=self.email,
                approval=ap,
            )
            try:
                self.runner.registry.invoke(tool, args, ctx, idempotency_key=key)
            except Exception as exc:  # OpsError is expected; anything else is recorded too
                s.rollback()
                ap = s.get(Approval, approval_id)
                ap.status = ApprovalStatus.FAILED
                ap.execution_error = (str(exc) if isinstance(exc, OpsError) else "Unexpected error")[:1000]
                log_event(s, workflow_id=wid, agent="orchestrator", level="ERROR",
                          message=f"Approved action failed: {ap.title}", data={"error": ap.execution_error})
                s.commit()
                return
            ap = s.get(Approval, approval_id)
            ap.status = ApprovalStatus.EXECUTED
            ap.execution_error = None
            log_event(s, workflow_id=wid, agent="orchestrator", message=f"Executed approved action: {ap.title}")
            s.commit()

    @staticmethod
    def _action_call(ap: Approval) -> tuple[str, dict, str]:
        key_base = f"{ap.workflow_id}:{ap.action_type}:{ap.target_key}"
        if ap.action_type == "create_task":
            return "create_task", dict(ap.payload), key_base
        if ap.action_type == "send_email":
            return "send_email", {"draft_id": ap.payload["draft_id"]}, key_base
        raise InvalidState(f"Unknown approval action '{ap.action_type}'")

    def _refresh_status(self, wid: str) -> None:
        with self.sf() as s:
            wf = s.get(Workflow, wid)
            if wf.status == WorkflowStatus.CANCELLED:
                return
            approvals = s.scalars(select(Approval).where(Approval.workflow_id == wid)).all()
            statuses = Counter(a.status for a in approvals)
            step_failed = s.scalar(
                select(WorkflowStep.id).where(WorkflowStep.workflow_id == wid, WorkflowStep.status == StepStatus.FAILED)
            ) is not None
            done_steps = s.scalar(
                select(WorkflowStep.id).where(WorkflowStep.workflow_id == wid, WorkflowStep.status == StepStatus.SUCCEEDED)
            ) is not None
            if statuses[ApprovalStatus.PENDING]:
                new_status = WorkflowStatus.WAITING_FOR_APPROVAL
            elif statuses[ApprovalStatus.FAILED] or step_failed:
                executed = statuses[ApprovalStatus.EXECUTED]
                new_status = WorkflowStatus.PARTIALLY_COMPLETED if (executed or done_steps) else WorkflowStatus.FAILED
            else:
                new_status = WorkflowStatus.COMPLETED
            wf.status = new_status
            wf.updated_at = utcnow()
            if new_status != WorkflowStatus.WAITING_FOR_APPROVAL:
                wf.completed_at = utcnow()
            s.flush()
            wf.summary = build_summary(s, wf)
            log_event(s, workflow_id=wid, agent="orchestrator", message=f"Workflow status: {new_status}")
            s.commit()

    def cancel(self, principal: Principal, wid: str) -> dict:
        _require(principal, APPROVER_ROLES)
        with self.sf() as s:
            wf = s.get(Workflow, wid)
            if wf is None:
                raise NotFound("Workflow not found")
            if wf.status in (WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED):
                raise InvalidState(f"Workflow is already {wf.status}")
            for ap in s.scalars(select(Approval).where(Approval.workflow_id == wid, Approval.status == ApprovalStatus.PENDING)):
                ap.status = ApprovalStatus.CANCELLED
            for draft in s.scalars(select(EmailDraft).where(EmailDraft.workflow_id == wid, EmailDraft.status == DraftStatus.DRAFT)):
                draft.status = DraftStatus.CANCELLED
            wf.status = WorkflowStatus.CANCELLED
            wf.completed_at = utcnow()
            log_event(s, workflow_id=wid, agent="approval_layer", message=f"Cancelled by {principal.name}")
            s.commit()
        self._refresh_summary(wid)
        return self.get_workflow(principal, wid)

    def _refresh_summary(self, wid: str) -> None:
        with self.sf() as s:
            wf = s.get(Workflow, wid)
            wf.summary = build_summary(s, wf)
            s.commit()

    # ----- approvals --------------------------------------------------------------

    def list_approvals(self, principal: Principal, status: str | None = None, workflow_id: str | None = None) -> list[dict]:
        with self.sf() as s:
            stmt = select(Approval).order_by(Approval.id.desc())
            if status:
                stmt = stmt.where(Approval.status == status)
            if workflow_id:
                stmt = stmt.where(Approval.workflow_id == workflow_id)
            return [self._approval_view(s, a) for a in s.scalars(stmt.limit(200))]

    def decide(self, principal: Principal, approval_id: int, decision: str, note: str | None = None, edits: dict | None = None) -> dict:
        _require(principal, APPROVER_ROLES)
        if decision not in ("approve", "reject"):
            raise InvalidInput("Decision must be 'approve' or 'reject'")
        with self.sf() as s:
            ap = s.get(Approval, approval_id)
            if ap is None:
                raise NotFound("Approval not found")
            if ap.status != ApprovalStatus.PENDING:
                raise InvalidState(f"Approval is {ap.status}, not PENDING")
            draft = s.get(EmailDraft, ap.payload.get("draft_id")) if ap.action_type == "send_email" else None
            if decision == "approve":
                if edits:
                    self._apply_edits(ap, draft, edits)
                ap.status = ApprovalStatus.APPROVED
                if draft is not None:
                    draft.status = DraftStatus.APPROVED
            else:
                ap.status = ApprovalStatus.REJECTED
                if draft is not None:
                    draft.status = DraftStatus.REJECTED
            ap.decided_by = principal.id
            ap.decided_at = utcnow()
            ap.decision_note = (note or None) and note[:500]
            log_event(
                s, workflow_id=ap.workflow_id, agent="approval_layer",
                message=f"{principal.name} {decision}d: {ap.title}",
                data={"approval_id": ap.id, "action": ap.action_type, "edited": bool(edits)},
            )
            s.commit()
            return self._approval_view(s, ap)

    def approve_all(self, principal: Principal, wid: str) -> dict:
        _require(principal, APPROVER_ROLES)
        with self.sf() as s:
            pending = list(s.scalars(select(Approval.id).where(Approval.workflow_id == wid, Approval.status == ApprovalStatus.PENDING)))
        for approval_id in pending:
            self.decide(principal, approval_id, "approve", note="Approved in bulk")
        return {"approved": len(pending)}

    @staticmethod
    def _apply_edits(ap: Approval, draft: EmailDraft | None, edits: dict) -> None:
        if ap.action_type == "create_task":
            allowed = {"title", "description", "priority", "due_date"}
            if set(edits) - allowed:
                raise InvalidInput(f"Editable fields for tasks: {sorted(allowed)}")
            if "priority" in edits and edits["priority"] not in ("High", "Medium", "Low"):
                raise InvalidInput("Priority must be High, Medium or Low")
            if "title" in edits and len(str(edits["title"]).strip()) < 3:
                raise InvalidInput("Task title is too short")
            payload = dict(ap.payload)
            payload.update(edits)
            ap.payload = payload
            ap.title = f"Create task: {payload['title']}"
        elif ap.action_type == "send_email":
            allowed = {"subject", "body"}
            if set(edits) - allowed:
                raise InvalidInput(f"Editable fields for emails: {sorted(allowed)}")
            if draft is None:
                raise InvalidState("Email draft not found")
            if "subject" in edits:
                if len(str(edits["subject"]).strip()) < 3:
                    raise InvalidInput("Subject is too short")
                draft.subject = str(edits["subject"])[:300]
            if "body" in edits:
                if len(str(edits["body"]).strip()) < 10:
                    raise InvalidInput("Email body is too short")
                draft.body = str(edits["body"])[:10000]

    def _approval_view(self, s, ap: Approval) -> dict:
        view = {
            "id": ap.id,
            "workflow_id": ap.workflow_id,
            "action_type": ap.action_type,
            "target_key": ap.target_key,
            "title": ap.title,
            "status": ap.status,
            "payload": ap.payload,
            "requested_at": _iso(ap.requested_at),
            "decided_by": ap.decided_by,
            "decided_at": _iso(ap.decided_at),
            "decision_note": ap.decision_note,
            "execution_error": ap.execution_error,
        }
        if ap.action_type == "send_email":
            draft = s.get(EmailDraft, ap.payload.get("draft_id"))
            if draft is not None:
                view["draft"] = {
                    "id": draft.id, "to_address": draft.to_address, "subject": draft.subject,
                    "body": draft.body, "status": draft.status,
                }
        return view

    # ----- tasks (human-initiated, audited) ---------------------------------------

    def list_tasks(self, principal: Principal, status: str | None = None, region_id: int | None = None) -> list[dict]:
        with self.sf() as s:
            return DatabaseTaskProvider().list_tasks(s, status=status, region_id=region_id)

    def create_task(self, principal: Principal, data: dict) -> dict:
        _require(principal, ANALYST_ROLES)
        with self.sf() as s:
            result = DatabaseTaskProvider().create(
                s, title=data["title"], description=data.get("description", ""),
                assignee_id=data.get("assignee_id"), region_id=data.get("region_id"),
                priority=data.get("priority", "Medium"), due_date=data.get("due_date"), workflow_id=None,
            )
            log_event(s, workflow_id=None, agent="task_management", message=f"Task created by {principal.name}",
                      data={"task_id": result["task_id"]})
            s.commit()
            return result

    def update_task(self, principal: Principal, task_id: int, status: str) -> dict:
        _require(principal, ANALYST_ROLES)
        with self.sf() as s:
            result = DatabaseTaskProvider().update_status(s, task_id, status)
            log_event(s, workflow_id=None, agent="task_management", message=f"Task {task_id} set to {status} by {principal.name}")
            s.commit()
            return result

    # ----- analytics, reports and logs --------------------------------------------

    def sales_metrics(self, principal: Principal, week_start: date | None, threshold: float | None) -> dict:
        _require(principal, ANY_ROLE)
        with self.sf() as s:
            ctx = ToolContext(session=s, settings=self.settings, agent="reporting", workflow_id=None, email_provider=self.email)
            if week_start is None:
                week_start = self.runner.registry.invoke("read_sales_data", {}, ctx)["week_start"]
                if week_start is None:
                    raise NotFound("No sales data recorded")
            return self.runner.registry.invoke(
                "calculate_sales_metrics",
                {"week_start": week_start, "threshold": threshold or self.settings.underperformance_threshold},
                ctx,
            )

    def generate_report(self, principal: Principal, week_start: date | None, threshold: float | None) -> dict:
        _require(principal, ANALYST_ROLES)
        metrics = self.sales_metrics(principal, week_start, threshold)
        with self.sf() as s:
            ctx = ToolContext(session=s, settings=self.settings, agent="reporting", workflow_id=None, email_provider=self.email)
            files = [
                self.runner.registry.invoke(tool, {"metrics": metrics}, ctx)["path"]
                for tool in ("generate_pdf_report", "generate_excel_report", "generate_csv_report")
            ]
            log_event(s, workflow_id=None, agent="reporting", message=f"Report generated by {principal.name}")
            s.commit()
        return {"period_week": metrics["period_week"], "files": [Path(f).name for f in files]}

    def list_reports(self, principal: Principal) -> list[dict]:
        with self.sf() as s:
            return [self._report_view(r) for r in s.scalars(select(Report).order_by(Report.id.desc()).limit(200))]

    def report_path(self, principal: Principal, report_id: int) -> Path:
        with self.sf() as s:
            report = s.get(Report, report_id)
            if report is None:
                raise NotFound("Report not found")
            path = Path(report.file_path).resolve()
        root = self.settings.output_dir.resolve()
        if root not in path.parents or not path.exists():
            raise NotFound("Report file not found")
        return path

    @staticmethod
    def _report_view(r: Report) -> dict:
        return {
            "id": r.id, "workflow_id": r.workflow_id, "title": r.title, "kind": r.kind,
            "period_start": _iso(r.period_start), "created_at": _iso(r.created_at),
            "file_name": Path(r.file_path).name,
        }

    def list_logs(self, principal: Principal, workflow_id: str | None = None, limit: int = 200) -> list[dict]:
        _require(principal, ANY_ROLE)
        with self.sf() as s:
            stmt = select(ExecutionLog).order_by(ExecutionLog.id.desc()).limit(limit)
            if workflow_id:
                stmt = stmt.where(ExecutionLog.workflow_id == workflow_id)
            return [
                {
                    "id": row.id, "workflow_id": row.workflow_id, "timestamp": _iso(row.timestamp),
                    "agent": row.agent, "tool": row.tool, "level": row.level, "message": row.message,
                    "data": row.data,
                }
                for row in s.scalars(stmt)
            ]


def build_summary(s, wf: Workflow) -> dict:
    """Final-summary payload: completed/pending/failed steps, approvals, files, duration, open issues."""
    steps = s.scalars(select(WorkflowStep).where(WorkflowStep.workflow_id == wf.id).order_by(WorkflowStep.id)).all()
    approvals = s.scalars(select(Approval).where(Approval.workflow_id == wf.id)).all()
    reports = s.scalars(select(Report).where(Report.workflow_id == wf.id)).all()

    issues: list[str] = []
    for st in steps:
        issues.extend((st.output_json or {}).get("issues", []))
    failed_actions = [{"title": a.title, "error": a.execution_error} for a in approvals if a.status == ApprovalStatus.FAILED]
    pending_actions = [a.title for a in approvals if a.status == ApprovalStatus.PENDING]
    unresolved = issues + [f"Failed action: {x['title']} ({x['error']})" for x in failed_actions]
    unresolved += [f"Awaiting approval: {t}" for t in pending_actions]
    for st in steps:
        if st.status == StepStatus.FAILED:
            unresolved.append(f"Failed step: {st.title} ({st.error})")

    end = wf.completed_at or utcnow()
    return {
        "workflow_status": wf.status,
        "completed_steps": [st.step_key for st in steps if st.status == StepStatus.SUCCEEDED],
        "pending_steps": [st.step_key for st in steps if st.status in (StepStatus.PENDING, StepStatus.RUNNING)],
        "failed_steps": [{"step": st.step_key, "error": st.error} for st in steps if st.status == StepStatus.FAILED],
        "approval_counts": dict(Counter(a.status for a in approvals)),
        "executed_actions": [a.title for a in approvals if a.status == ApprovalStatus.EXECUTED],
        "rejected_actions": [a.title for a in approvals if a.status == ApprovalStatus.REJECTED],
        "failed_actions": failed_actions,
        "pending_actions": pending_actions,
        "generated_files": [Path(r.file_path).name for r in reports],
        "duration_seconds": round((end - wf.created_at).total_seconds(), 2) if wf.created_at else None,
        "unresolved_issues": unresolved,
    }
