"""Turns an intent into an explicit, ordered execution plan with dependency validation."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.llm import INTENT_SALES, IntentExtraction


class PlanStep(BaseModel):
    key: str
    agent: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    phase: str = "planning"  # planning | execution


class ExecutionPlan(BaseModel):
    intent: str
    objective: str
    threshold: float
    period_week: date | None = None
    steps: list[PlanStep]
    assumptions: list[str] = Field(default_factory=list)


class PlanValidationError(ValueError):
    pass


SALES_STEPS = [
    PlanStep(key="understand", agent="orchestrator", title="Interpret request and build execution plan"),
    PlanStep(key="retrieve_sales", agent="research", title="Retrieve weekly sales records", depends_on=["understand"]),
    PlanStep(key="analyze", agent="reporting", title="Calculate regional KPIs", depends_on=["retrieve_sales"]),
    PlanStep(key="identify", agent="reporting", title="Flag underperforming regions", depends_on=["analyze"]),
    PlanStep(key="report", agent="reporting", title="Generate PDF, Excel, CSV and chart", depends_on=["identify"]),
    PlanStep(
        key="prepare_tasks",
        agent="task_management",
        title="Prepare follow-up tasks (not created yet)",
        depends_on=["identify"],
        requires_approval=True,
    ),
    PlanStep(
        key="draft_emails",
        agent="communication",
        title="Save manager email drafts (not sent yet)",
        depends_on=["prepare_tasks"],
        requires_approval=True,
    ),
    PlanStep(
        key="request_approval",
        agent="approval_layer",
        title="Present proposals for human review",
        depends_on=["report", "draft_emails"],
    ),
]

POST_APPROVAL_STEPS = [
    PlanStep(
        key="execute_tasks",
        agent="task_management",
        title="Create approved tasks",
        depends_on=["request_approval"],
        requires_approval=True,
        phase="execution",
    ),
    PlanStep(
        key="send_emails",
        agent="communication",
        title="Send approved emails (simulated provider)",
        depends_on=["request_approval"],
        requires_approval=True,
        phase="execution",
    ),
    PlanStep(
        key="final_summary",
        agent="orchestrator",
        title="Record results and final summary",
        depends_on=["execute_tasks", "send_emails"],
        phase="execution",
    ),
]


def validate_plan(plan: ExecutionPlan) -> None:
    """Dependency check: every dependency must exist and appear earlier. No duplicate keys."""
    seen: set[str] = set()
    for step in plan.steps:
        if step.key in seen:
            raise PlanValidationError(f"Duplicate step key: {step.key}")
        for dependency in step.depends_on:
            if dependency not in seen:
                raise PlanValidationError(
                    f"Step '{step.key}' depends on '{dependency}', which is missing or comes later"
                )
        seen.add(step.key)


def build_plan(extraction: IntentExtraction, default_threshold: float) -> ExecutionPlan:
    if extraction.intent != INTENT_SALES:
        raise ValueError(f"No plan template for intent '{extraction.intent}' in this build")
    threshold = extraction.threshold or default_threshold
    assumptions = []
    if extraction.week_start is None:
        assumptions.append("Reporting period not specified: using the latest week with recorded actual sales.")
    assumptions.append(
        f"Underperformance threshold: {threshold:.0%} of target"
        + (" (from the request)." if extraction.threshold else " (configured default).")
    )
    assumptions.append("Missing actual sales are reported as data gaps and are never estimated.")
    assumptions.append(
        "Tasks and emails are proposals until approved. Email delivery is simulated in this build."
    )
    plan = ExecutionPlan(
        intent=extraction.intent,
        objective=extraction.objective or "Weekly sales performance follow-up",
        threshold=threshold,
        period_week=extraction.week_start,
        steps=[*SALES_STEPS, *POST_APPROVAL_STEPS],
        assumptions=assumptions,
    )
    validate_plan(plan)
    return plan
