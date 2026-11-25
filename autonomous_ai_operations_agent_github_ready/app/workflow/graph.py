"""LangGraph workflow for the weekly sales follow-up.

Every node runs through `_step`, which provides checkpointing: a step that already
succeeded is skipped on resume and its stored output is reused. Failed steps are
retried on the next run. Side effects only happen through the tool registry.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from time import perf_counter
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app import repository as repo
from app.communication import compose_sales_follow_up_email
from app.config import Settings
from app.errors import OpsError
from app.llm import INTENT_SALES, IntentService
from app.logging_utils import log_event
from app.models import StepStatus, Workflow, WorkflowStatus, WorkflowStep, utcnow
from app.planner import build_plan
from app.tools.builtin import build_registry
from app.tools.registry import ToolContext, ToolRegistry

log = logging.getLogger("ops_agent.graph")


class WorkflowState(TypedDict, total=False):
    workflow_id: str
    request: str
    status: str
    intent: str
    intent_source: str
    plan: dict
    threshold: float
    period_week: str | None
    metrics: dict
    underperformers: list
    proposed_tasks: list
    draft_ids: list
    report_files: list
    pending_approvals: int
    clarification: str
    error: str


_STATE_KEYS = frozenset(WorkflowState.__annotations__)


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _state_update(out: dict) -> dict:
    return {k: v for k, v in out.items() if k in _STATE_KEYS}


def clarification_for(intent: str) -> str:
    if intent == "unknown":
        return (
            "I could not match this request to a supported workflow in this build. "
            "Supported today: the weekly sales performance workflow, for example: "
            "'Prepare a weekly sales performance report, identify underperforming regions, "
            "create follow-up tasks, and draft emails.' Please rephrase or confirm."
        )
    return (
        f"The '{intent}' workflow is planned but not implemented in this build. "
        "Only the weekly sales performance workflow is available right now."
    )


def _route(next_node: str) -> Callable[[WorkflowState], str]:
    def route(state: WorkflowState) -> str:
        if state.get("status") in (WorkflowStatus.NEEDS_CLARIFICATION, WorkflowStatus.FAILED):
            return END
        return next_node

    return route


class WorkflowRunner:
    STATE_KEYS = _STATE_KEYS

    def _filter_state(self, out):
        return {k: v for k, v in out.items() if k in self.STATE_KEYS}

    def __init__(self, session_factory, settings: Settings, email_provider, intents: IntentService) -> None:
        self.sf = session_factory
        self.settings = settings
        self.email = email_provider
        self.intents = intents
        self.registry: ToolRegistry = build_registry()
        self.graph = self._build_graph()

    # ----- plumbing ---------------------------------------------------------------

    def _ctx(self, session, workflow_id: str, agent: str, approval=None) -> ToolContext:
        return ToolContext(
            session=session,
            settings=self.settings,
            agent=agent,
            workflow_id=workflow_id,
            email_provider=self.email,
            approval=approval,
        )

    def _call(self, ctx: ToolContext, tool: str, args: dict, key: str | None = None):
        return self.registry.invoke(tool, args, ctx, idempotency_key=key)

    def _step(self, state: WorkflowState, key: str, agent: str, title: str, body) -> dict:
        wid = state["workflow_id"]
        with self.sf() as s:
            step = s.scalar(select(WorkflowStep).where(WorkflowStep.workflow_id == wid, WorkflowStep.step_key == key))
            if step is not None and step.status == StepStatus.SUCCEEDED:
                log_event(s, workflow_id=wid, agent=agent, message=f"Checkpoint reused: '{title}' already completed")
                s.commit()
                return self._filter_state(step.output_json or {})

            if step is None:
                step = WorkflowStep(workflow_id=wid, step_key=key, agent=agent, title=title, status=StepStatus.PENDING, attempts=0)
                s.add(step)
                s.flush()
            step.status = StepStatus.RUNNING
            step.started_at = utcnow()
            step.attempts = (step.attempts or 0) + 1
            step.error = None
            step_id = step.id
            log_event(s, workflow_id=wid, agent=agent, message=f"Started: {title}")
            s.commit()

            started = perf_counter()
            try:
                out = _jsonable(body(s, state))
                s.commit()
            except OpsError as exc:
                s.rollback()
                return self._fail(s, step_id, wid, agent, key, str(exc))
            except Exception:
                log.exception("Unexpected failure in step '%s'", key)
                s.rollback()
                return self._fail(s, step_id, wid, agent, key, "Unexpected internal error; see execution logs.")

            step = s.get(WorkflowStep, step_id)
            step.status = StepStatus.SUCCEEDED
            step.output_json = out
            step.finished_at = utcnow()
            step.duration_ms = (perf_counter() - started) * 1000
            log_event(s, workflow_id=wid, agent=agent, message=f"Completed: {title}")
            s.commit()
            return self._filter_state(out)

    def _fail(self, s, step_id: int, wid: str, agent: str, key: str, message: str) -> dict:
        step = s.get(WorkflowStep, step_id)
        if step is not None:
            step.status = StepStatus.FAILED
            step.error = message[:2000]
            step.finished_at = utcnow()
        log_event(s, workflow_id=wid, agent=agent, level="ERROR", message=f"Step '{key}' failed: {message}")
        s.commit()
        return {"status": WorkflowStatus.FAILED, "error": message}

    # ----- step bodies ------------------------------------------------------------

    def _understand(self, s, state: WorkflowState) -> dict:
        wid = state["workflow_id"]
        extraction, source = self.intents.extract(state["request"])
        log_event(
            s, workflow_id=wid, agent="orchestrator",
            message=f"Intent classified as '{extraction.intent}' via {source}",
            data={"intent": extraction.intent, "source": source},
        )
        if extraction.intent != INTENT_SALES:
            return {
                "status": WorkflowStatus.NEEDS_CLARIFICATION,
                "intent": extraction.intent,
                "intent_source": source,
                "clarification": clarification_for(extraction.intent),
            }
        plan = build_plan(extraction, self.settings.underperformance_threshold)
        return {
            "status": WorkflowStatus.IN_PROGRESS,
            "intent": plan.intent,
            "intent_source": source,
            "threshold": plan.threshold,
            "period_week": plan.period_week.isoformat() if plan.period_week else None,
            "plan": plan.model_dump(mode="json"),
        }

    def _retrieve(self, s, state: WorkflowState) -> dict:
        wid = state["workflow_id"]
        ctx = self._ctx(s, wid, "research")
        requested = state["plan"].get("period_week")
        result = self._call(ctx, "read_sales_data", {"week_start": requested})
        if not result["week_start"] or result["record_count"] == 0:
            available = ", ".join(result["available_weeks"]) or "none"
            return {
                "status": WorkflowStatus.NEEDS_CLARIFICATION,
                "clarification": (
                    f"No sales records found for week {requested or 'latest'}. "
                    f"Weeks with recorded data: {available}. Which week should the report cover?"
                ),
            }
        plan = dict(state["plan"])
        plan["period_week"] = result["week_start"]
        return {"status": WorkflowStatus.IN_PROGRESS, "period_week": result["week_start"], "plan": plan}

    def _analyze(self, s, state: WorkflowState) -> dict:
        ctx = self._ctx(s, state["workflow_id"], "reporting")
        metrics = self._call(
            ctx, "calculate_sales_metrics", {"week_start": state["period_week"], "threshold": state["threshold"]}
        )
        issues = [f"{g['region']}: {g['issue']}" for g in metrics["data_gaps"]]
        return {"status": WorkflowStatus.IN_PROGRESS, "metrics": metrics, "issues": issues}

    def _identify(self, s, state: WorkflowState) -> dict:
        under = state["metrics"]["underperformers"]
        log_event(
            s, workflow_id=state["workflow_id"], agent="reporting",
            message=f"{len(under)} underperforming region(s) below {state['threshold']:.0%}",
        )
        return {"status": WorkflowStatus.IN_PROGRESS, "underperformers": under}

    def _report(self, s, state: WorkflowState) -> dict:
        ctx = self._ctx(s, state["workflow_id"], "reporting")
        files = [
            self._call(ctx, tool, {"metrics": state["metrics"]})["path"]
            for tool in ("generate_pdf_report", "generate_excel_report", "generate_csv_report", "generate_chart")
        ]
        return {"status": WorkflowStatus.IN_PROGRESS, "report_files": files}

    def _prepare_tasks(self, s, state: WorkflowState) -> dict:
        wid = state["workflow_id"]
        metrics = state["metrics"]
        ctx = self._ctx(s, wid, "task_management")
        due = date.fromisoformat(metrics["period_end"]) + timedelta(days=self.settings.task_due_days_after_period)
        open_titles = {t["title"] for t in self._call(ctx, "get_tasks", {"status": "OPEN"})["tasks"]}

        proposed, issues = [], []
        for row in state.get("underperformers", []):
            title = f"Recovery plan: {row['region']} at {row['achievement']:.1%} of target"
            if title in open_titles:
                issues.append(f"{row['region']}: duplicate skipped, an open task with this title already exists")
                continue
            if row["manager_id"] is None:
                issues.append(f"{row['region']}: no manager assigned, so the task is unassigned")
            priority = "High" if row["achievement"] < self.settings.severe_underperformance_threshold else "Medium"
            payload = {
                "title": title,
                "description": (
                    f"{row['suggested_follow_up']} Actual {row['actual']:,.0f} vs target "
                    f"{row['target']:,.0f} for the week of {metrics['period_week']}."
                ),
                "assignee_id": row["manager_id"],
                "region_id": row["region_id"],
                "priority": priority,
                "due_date": due.isoformat(),
            }
            repo.create_pending_approval(
                s,
                workflow_id=wid,
                action_type="create_task",
                target_key=f"region:{row['region_id']}",
                title=f"Create task: {title}",
                payload=payload,
            )
            proposed.append({**payload, "region": row["region"]})
        return {"status": WorkflowStatus.IN_PROGRESS, "proposed_tasks": proposed, "issues": issues}

    def _draft_emails(self, s, state: WorkflowState) -> dict:
        wid = state["workflow_id"]
        metrics = state["metrics"]
        ctx = self._ctx(s, wid, "communication")
        tasks_by_region = {t["region_id"]: t for t in state.get("proposed_tasks", [])}

        drafts, issues = [], []
        for row in state.get("underperformers", []):
            recipient = self._call(ctx, "get_recipient_details", {"region_id": row["region_id"]})
            if not recipient.get("email"):
                issues.append(
                    f"{row['region']}: no email on file for {row['manager'] or 'the manager'}; "
                    "draft not created, so contact them manually"
                )
                continue
            subject, body = compose_sales_follow_up_email(row, metrics, tasks_by_region.get(row["region_id"]))
            result = self._call(
                ctx,
                "save_email_draft",
                {"region_id": row["region_id"], "subject": subject, "body": body},
                key=f"{wid}:save_email_draft:region:{row['region_id']}",
            )
            repo.create_pending_approval(
                s,
                workflow_id=wid,
                action_type="send_email",
                target_key=f"region:{row['region_id']}",
                title=f"Send email to {recipient['email']} ({row['region']})",
                payload={"draft_id": result["draft_id"], "to": recipient["email"], "region": row["region"]},
            )
            drafts.append(result["draft_id"])
        return {"status": WorkflowStatus.IN_PROGRESS, "draft_ids": drafts, "issues": issues}

    def _request_approval(self, s, state: WorkflowState) -> dict:
        pending = repo.count_approvals(s, state["workflow_id"], "PENDING")
        status = WorkflowStatus.WAITING_FOR_APPROVAL if pending else WorkflowStatus.COMPLETED
        return {"status": status, "pending_approvals": pending}

    # ----- graph ------------------------------------------------------------------

    def understand(self, state: WorkflowState) -> dict:
        return self._step(state, "understand", "orchestrator", "Interpret request and build execution plan", self._understand)

    def retrieve_sales(self, state: WorkflowState) -> dict:
        return self._step(state, "retrieve_sales", "research", "Retrieve weekly sales records", self._retrieve)

    def analyze(self, state: WorkflowState) -> dict:
        return self._step(state, "analyze", "reporting", "Calculate regional KPIs", self._analyze)

    def identify(self, state: WorkflowState) -> dict:
        return self._step(state, "identify", "reporting", "Flag underperforming regions", self._identify)

    def report(self, state: WorkflowState) -> dict:
        return self._step(state, "report", "reporting", "Generate PDF, Excel, CSV and chart", self._report)

    def prepare_tasks(self, state: WorkflowState) -> dict:
        return self._step(state, "prepare_tasks", "task_management", "Prepare follow-up tasks (not created yet)", self._prepare_tasks)

    def draft_emails(self, state: WorkflowState) -> dict:
        return self._step(state, "draft_emails", "communication", "Save manager email drafts (not sent yet)", self._draft_emails)

    def request_approval(self, state: WorkflowState) -> dict:
        return self._step(state, "request_approval", "approval_layer", "Present proposals for human review", self._request_approval)

    def _build_graph(self):
        order = [
            ("understand", self.understand),
            ("retrieve_sales", self.retrieve_sales),
            ("analyze", self.analyze),
            ("identify", self.identify),
            ("report", self.report),
            ("prepare_tasks", self.prepare_tasks),
            ("draft_emails", self.draft_emails),
            ("request_approval", self.request_approval),
        ]
        graph = StateGraph(WorkflowState)
        for name, fn in order:
            graph.add_node(name, fn)
        graph.add_edge(START, order[0][0])
        for (name, _), (nxt, _) in zip(order, order[1:]):
            graph.add_conditional_edges(name, _route(nxt), {nxt: nxt, END: END})
        graph.add_edge(order[-1][0], END)
        return graph.compile()
