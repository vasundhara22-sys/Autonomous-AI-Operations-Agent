"""Leave-update workflow: collect leave events, draft one email per employee, wait for approval.

Emails are sent only by the existing approval-gated send step, so nothing goes out
without a person's approval.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app import repository as repo
from app.leave.messages import compose_leave_update
from app.models import WorkflowStatus
from app.workflow.graph import WorkflowRunner, _route

LEAVE_INTENT = "leave_update"


class LeaveWorkflowState(TypedDict, total=False):
    workflow_id: str
    request: str
    status: str
    intent: str
    plan: dict
    as_of: str
    leave_rows: list
    drafts: list
    pending_approvals: int
    clarification: str
    error: str


def _plan() -> dict:
    return {
        "intent": LEAVE_INTENT,
        "objective": "Send each employee a leave update after approval",
        "steps": [
            {"key": "leave_understand", "agent": "orchestrator", "title": "Check request and set up leave updates", "depends_on": [], "requires_approval": False, "phase": "planning"},
            {"key": "leave_collect", "agent": "research", "title": "Find pending requests, upcoming leave and low balances", "depends_on": ["leave_understand"], "requires_approval": False, "phase": "planning"},
            {"key": "leave_draft", "agent": "communication", "title": "Draft one update per employee (not sent yet)", "depends_on": ["leave_collect"], "requires_approval": True, "phase": "planning"},
            {"key": "leave_request_approval", "agent": "approval_layer", "title": "Present updates for human review", "depends_on": ["leave_draft"], "requires_approval": False, "phase": "planning"},
            {"key": "execute_emails", "agent": "communication", "title": "Send approved updates through the email provider", "depends_on": ["leave_request_approval"], "requires_approval": True, "phase": "execution"},
            {"key": "final_summary", "agent": "orchestrator", "title": "Record results and final summary", "depends_on": ["execute_emails"], "requires_approval": False, "phase": "execution"},
        ],
        "assumptions": [
            "Each employee gets at most one update per run, covering all their leave events.",
            "Recipients are the email addresses in the employee record. Missing addresses are reported, not guessed.",
            "Nothing is sent until a person approves it in the Approval Center.",
        ],
    }


class LeaveWorkflowRunner(WorkflowRunner):
    STATE_KEYS = frozenset(LeaveWorkflowState.__annotations__)

    def __init__(self, session_factory, settings, email_provider) -> None:
        super().__init__(session_factory, settings, email_provider, intents=None)

    def _build_graph(self):
        order = [
            ("leave_understand", self.leave_understand),
            ("leave_collect", self.leave_collect),
            ("leave_draft", self.leave_draft),
            ("leave_request_approval", self.leave_request_approval),
        ]
        graph = StateGraph(LeaveWorkflowState)
        for name, fn in order:
            graph.add_node(name, fn)
        graph.add_edge(START, order[0][0])
        for (name, _), (nxt, _) in zip(order, order[1:]):
            graph.add_conditional_edges(name, _route(nxt), {nxt: nxt, END: END})
        graph.add_edge(order[-1][0], END)
        return graph.compile()

    def leave_understand(self, state):
        return self._step(state, "leave_understand", "orchestrator", "Check request and set up leave updates", self._understand_leave)

    def leave_collect(self, state):
        return self._step(state, "leave_collect", "research", "Find pending requests, upcoming leave and low balances", self._collect_leave)

    def leave_draft(self, state):
        return self._step(state, "leave_draft", "communication", "Draft one update per employee (not sent yet)", self._draft_leave)

    def leave_request_approval(self, state):
        return self._step(state, "leave_request_approval", "approval_layer", "Present updates for human review", self._request_leave_approval)

    def _understand_leave(self, s, state):
        return {"status": WorkflowStatus.IN_PROGRESS, "intent": LEAVE_INTENT, "plan": _plan()}

    def _collect_leave(self, s, state):
        ctx = self._ctx(s, state["workflow_id"], "research")
        result = self._call(ctx, "get_leave_status", {})
        return {"status": WorkflowStatus.IN_PROGRESS, "as_of": result["as_of"], "leave_rows": result["employees"]}

    def _draft_leave(self, s, state):
        wid = state["workflow_id"]
        ctx = self._ctx(s, wid, "communication")
        drafts, issues = [], []
        for row in state.get("leave_rows", []):
            if not row["email"]:
                issues.append(f"{row['name']}: no email on file, so no update was drafted; contact them manually")
                continue
            subject, body = compose_leave_update(row, state["as_of"])
            result = self._call(
                ctx,
                "save_employee_email_draft",
                {"employee_id": row["employee_id"], "subject": subject, "body": body},
                key=f"{wid}:save_employee_email_draft:{row['employee_id']}",
            )
            repo.create_pending_approval(
                s,
                workflow_id=wid,
                action_type="send_email",
                target_key=f"employee:{row['employee_id']}",
                title=f"Send leave update to {row['email']} ({row['name']})",
                payload={"draft_id": result["draft_id"], "to": row["email"], "employee_id": row["employee_id"]},
            )
            drafts.append(result["draft_id"])
        return {"status": WorkflowStatus.IN_PROGRESS, "drafts": drafts, "issues": issues}

    def _request_leave_approval(self, s, state):
        pending = repo.count_approvals(s, state["workflow_id"], "PENDING")
        status = WorkflowStatus.WAITING_FOR_APPROVAL if pending else WorkflowStatus.COMPLETED
        return {"status": status, "pending_approvals": pending}
