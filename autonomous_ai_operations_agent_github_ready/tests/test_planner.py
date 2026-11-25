from datetime import date

import pytest

from app.llm import INTENT_SALES, INTENT_TASKS, INTENT_UNKNOWN, IntentExtraction, IntentService, rules_extract
from app.planner import ExecutionPlan, PlanStep, PlanValidationError, build_plan, validate_plan
from tests.conftest import SAMPLE_REQUEST


def test_sample_request_is_sales_intent():
    assert rules_extract(SAMPLE_REQUEST).intent == INTENT_SALES


def test_threshold_and_week_are_extracted():
    ext = rules_extract("Identify regions below 75% of target for the week of 2026-11-02 in our sales report")
    assert ext.threshold == pytest.approx(0.75)
    assert ext.week_start == date(2026, 11, 2)


def test_unsupported_request_is_unknown():
    assert rules_extract("Book a flight to Delhi for Monday").intent == INTENT_UNKNOWN


def test_overdue_task_request_is_recognised_but_not_implemented():
    assert rules_extract("Find overdue tasks and reassign them to balance workload").intent == INTENT_TASKS


def test_plan_is_ordered_and_marks_approval_steps():
    plan = build_plan(rules_extract(SAMPLE_REQUEST), default_threshold=0.80)
    keys = [s.key for s in plan.steps]
    assert keys.index("retrieve_sales") < keys.index("analyze") < keys.index("identify")
    assert keys.index("prepare_tasks") < keys.index("draft_emails") < keys.index("request_approval")
    approval_steps = {s.key for s in plan.steps if s.requires_approval}
    assert {"prepare_tasks", "draft_emails", "execute_tasks", "send_emails"} <= approval_steps
    assert plan.threshold == 0.80
    assert any("never estimated" in a for a in plan.assumptions)


def test_plan_validation_rejects_missing_dependency():
    bad = ExecutionPlan(
        intent=INTENT_SALES, objective="x", threshold=0.8,
        steps=[PlanStep(key="a", agent="x", title="a", depends_on=["ghost"])],
    )
    with pytest.raises(PlanValidationError):
        validate_plan(bad)


def test_plan_validation_rejects_duplicate_keys():
    bad = ExecutionPlan(
        intent=INTENT_SALES, objective="x", threshold=0.8,
        steps=[PlanStep(key="a", agent="x", title="a"), PlanStep(key="a", agent="x", title="b")],
    )
    with pytest.raises(PlanValidationError):
        validate_plan(bad)


class _BrokenModel:
    name = "broken"

    def extract(self, request):
        raise ValueError("model returned invalid JSON")


def test_invalid_llm_output_falls_back_to_rules():
    service = IntentService(_BrokenModel())
    extraction, source = service.extract(SAMPLE_REQUEST)
    assert source == "rules_fallback"
    assert extraction.intent == INTENT_SALES


def test_llm_output_is_schema_validated():
    with pytest.raises(ValueError):
        IntentExtraction(intent="delete_everything")
