"""Document-processing workflow.

Steps: check selection -> confirm documents are indexed -> extract fields and summarise ->
propose review tasks for gaps -> wait for approval. Reuses the checkpointing and approval
machinery of WorkflowRunner, so resume and duplicate protection work the same way.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app import repository as repo
from app.models import Document, WorkflowStatus
from app.workflow.graph import WorkflowRunner, _route

DOCUMENT_INTENT = "document_processing"
MAX_DOCUMENTS = 25


class DocumentWorkflowState(TypedDict, total=False):
    workflow_id: str
    request: str
    status: str
    intent: str
    document_ids: list
    plan: dict
    documents: list
    extractions: list
    proposed_tasks: list
    pending_approvals: int
    clarification: str
    error: str


def _plan(document_ids: list[int]) -> dict:
    steps = [
        {"key": "doc_understand", "agent": "orchestrator", "title": "Check request and document selection", "depends_on": [], "requires_approval": False, "phase": "planning"},
        {"key": "doc_load", "agent": "research", "title": "Confirm selected documents are indexed", "depends_on": ["doc_understand"], "requires_approval": False, "phase": "planning"},
        {"key": "doc_extract", "agent": "research", "title": "Extract fields and summarise each document", "depends_on": ["doc_load"], "requires_approval": False, "phase": "planning"},
        {"key": "doc_review", "agent": "task_management", "title": "Propose review tasks for gaps (not created yet)", "depends_on": ["doc_extract"], "requires_approval": True, "phase": "planning"},
        {"key": "doc_request_approval", "agent": "approval_layer", "title": "Present proposals for human review", "depends_on": ["doc_review"], "requires_approval": False, "phase": "planning"},
        {"key": "execute_tasks", "agent": "task_management", "title": "Create approved review tasks", "depends_on": ["doc_request_approval"], "requires_approval": True, "phase": "execution"},
        {"key": "final_summary", "agent": "orchestrator", "title": "Record results and final summary", "depends_on": ["execute_tasks"], "requires_approval": False, "phase": "execution"},
    ]
    return {
        "intent": DOCUMENT_INTENT,
        "objective": "Extract key fields, summarise each document, and propose review tasks for gaps",
        "document_ids": document_ids,
        "steps": steps,
        "assumptions": [
            "Fields are read only from lines such as 'Invoice number: value'. Nothing is guessed.",
            "Summaries are extractive: sentences copied from the document.",
            "Documents do not name an owner, so review tasks are unassigned. Assign them in Task Management.",
            "Tasks are proposals until approved.",
        ],
    }


class DocumentWorkflowRunner(WorkflowRunner):
    STATE_KEYS = frozenset(DocumentWorkflowState.__annotations__)

    def __init__(self, session_factory, settings, email_provider) -> None:
        super().__init__(session_factory, settings, email_provider, intents=None)

    def _build_graph(self):
        order = [
            ("doc_understand", self.doc_understand),
            ("doc_load", self.doc_load),
            ("doc_extract", self.doc_extract),
            ("doc_review", self.doc_review),
            ("doc_request_approval", self.doc_request_approval),
        ]
        graph = StateGraph(DocumentWorkflowState)
        for name, fn in order:
            graph.add_node(name, fn)
        graph.add_edge(START, order[0][0])
        for (name, _), (nxt, _) in zip(order, order[1:]):
            graph.add_conditional_edges(name, _route(nxt), {nxt: nxt, END: END})
        graph.add_edge(order[-1][0], END)
        return graph.compile()

    # ----- nodes ------------------------------------------------------------------

    def doc_understand(self, state):
        return self._step(state, "doc_understand", "orchestrator", "Check request and document selection", self._understand_docs)

    def doc_load(self, state):
        return self._step(state, "doc_load", "research", "Confirm selected documents are indexed", self._load_docs)

    def doc_extract(self, state):
        return self._step(state, "doc_extract", "research", "Extract fields and summarise each document", self._extract_docs)

    def doc_review(self, state):
        return self._step(state, "doc_review", "task_management", "Propose review tasks for gaps (not created yet)", self._propose_reviews)

    def doc_request_approval(self, state):
        return self._step(state, "doc_request_approval", "approval_layer", "Present proposals for human review", self._request_doc_approval)

    # ----- step bodies -----------------------------------------------------------

    def _understand_docs(self, s, state):
        ids = [int(i) for i in state.get("document_ids") or []]
        if not ids:
            return {
                "status": WorkflowStatus.NEEDS_CLARIFICATION,
                "clarification": "Select at least one uploaded document to process. Upload files on the Knowledge Base page, then choose them here.",
            }
        if len(ids) > MAX_DOCUMENTS:
            return {
                "status": WorkflowStatus.NEEDS_CLARIFICATION,
                "clarification": f"Select at most {MAX_DOCUMENTS} documents per run.",
            }
        return {"status": WorkflowStatus.IN_PROGRESS, "intent": DOCUMENT_INTENT, "plan": _plan(sorted(set(ids)))}

    def _load_docs(self, s, state):
        usable, issues = [], []
        for doc_id in state["document_ids"]:
            doc = s.get(Document, int(doc_id))
            if doc is None:
                issues.append(f"Document {doc_id} was not found")
            elif doc.status != "INDEXED":
                issues.append(f"{doc.original_name}: not ready (status {doc.status}), skipped")
            else:
                usable.append({"id": doc.id, "name": doc.original_name})
        if not usable:
            return {
                "status": WorkflowStatus.NEEDS_CLARIFICATION,
                "clarification": "None of the selected documents are ready. " + "; ".join(issues),
            }
        return {"status": WorkflowStatus.IN_PROGRESS, "documents": usable, "issues": issues}

    def _extract_docs(self, s, state):
        ctx = self._ctx(s, state["workflow_id"], "research")
        extractions, issues = [], []
        for doc in state["documents"]:
            fields = self._call(ctx, "extract_document_fields", {"document_id": doc["id"]})
            summary = self._call(ctx, "summarize_document", {"document_id": doc["id"], "max_sentences": 3})
            record = {
                "document_id": doc["id"],
                "name": doc["name"],
                "doc_type": fields["doc_type"],
                "doc_type_label": fields["doc_type_label"],
                "unknown": fields["unknown"],
                "fields": fields["fields"],
                "missing_labels": fields["missing_labels"],
                "invalid_labels": fields["invalid_labels"],
                "summary": [item["text"] for item in summary["sentences"]],
            }
            extractions.append(record)
            if record["unknown"]:
                issues.append(f"{doc['name']}: document type not recognised")
            if record["missing_labels"]:
                issues.append(f"{doc['name']}: missing {', '.join(record['missing_labels'])}")
            if record["invalid_labels"]:
                issues.append(f"{doc['name']}: invalid value for {', '.join(record['invalid_labels'])}")
        return {"status": WorkflowStatus.IN_PROGRESS, "extractions": extractions, "issues": issues}

    def _propose_reviews(self, s, state):
        wid = state["workflow_id"]
        ctx = self._ctx(s, wid, "task_management")
        due = date.today() + timedelta(days=self.settings.task_due_days_after_period)
        open_titles = {t["title"] for t in self._call(ctx, "get_tasks", {"status": "OPEN"})["tasks"]}

        proposed, issues = [], []
        for rec in state.get("extractions", []):
            reasons = []
            if rec["unknown"]:
                reasons.append("confirm document type")
            else:
                if rec["missing_labels"]:
                    reasons.append("missing " + ", ".join(rec["missing_labels"]))
                if rec["invalid_labels"]:
                    reasons.append("check " + ", ".join(rec["invalid_labels"]))
            if not reasons:
                continue

            title = f"Review {rec['name']}: {'; '.join(reasons)}"[:200]
            if title in open_titles:
                issues.append(f"{rec['name']}: duplicate skipped, a review task is already open")
                continue

            amount = rec["fields"].get("total_amount", {})
            priority = "High" if amount.get("status") in ("missing", "invalid") else "Medium"
            first_line = rec["summary"][0] if rec["summary"] else "No summary available."
            payload = {
                "title": title,
                "description": (
                    f"Document: {rec['name']} (type: {rec['doc_type_label'] or 'not recognised'}). "
                    f"Gaps: {'; '.join(reasons)}. Summary: {first_line} "
                    "No owner is recorded in the document; assign one in Task Management."
                ),
                "assignee_id": None,
                "region_id": None,
                "priority": priority,
                "due_date": due.isoformat(),
            }
            repo.create_pending_approval(
                s,
                workflow_id=wid,
                action_type="create_task",
                target_key=f"document:{rec['document_id']}",
                title=f"Create task: {title}",
                payload=payload,
            )
            proposed.append({**payload, "document_id": rec["document_id"]})
        return {"status": WorkflowStatus.IN_PROGRESS, "proposed_tasks": proposed, "issues": issues}

    def _request_doc_approval(self, s, state):
        pending = repo.count_approvals(s, state["workflow_id"], "PENDING")
        status = WorkflowStatus.WAITING_FOR_APPROVAL if pending else WorkflowStatus.COMPLETED
        return {"status": status, "pending_approvals": pending}
