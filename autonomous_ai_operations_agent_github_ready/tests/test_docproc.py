"""Document-processing tests: field extraction rules, the workflow, approvals, and permissions."""

from sqlalchemy import func, select

import pytest

from app.docproc.extractor import analyse_text
from app.errors import PermissionDenied
from app.knowledge.service import KnowledgeService
from app.models import Approval, Task
from app.workflow.document_graph import DOCUMENT_INTENT
from tests.conftest import SAMPLE_REQUEST  # noqa: F401  (keeps fixture module imported)

INVOICE_OK = """Supplier invoice
Invoice number: INV-1
Invoice date: 2026-11-03
Supplier name: Acme Ltd
Total amount: 1,250.00
"""
INVOICE_MISSING_DATE = """Supplier invoice
Invoice No: INV-2
Supplier: Beta Co
Amount due: 900.00
"""
ONBOARDING_EMPTY_EMAIL = """Employee onboarding form
Employee name: Rohan Mehta
Start date: 15 December 2026
Department: Sales Operations
Reporting manager: Priya Nair
Work email:
"""
NOTES = "Team meeting notes. Attendees discussed the office layout and the offsite plan."


@pytest.fixture
def kb(env):
    return KnowledgeService(env.sf, env.settings)


def _count(sf, model):
    with sf() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def _step_output(wf, key):
    return next(s for s in wf["steps"] if s["step_key"] == key)["output"]


# ----- extraction rules --------------------------------------------------------------

def test_complete_invoice_has_no_gaps():
    result = analyse_text(INVOICE_OK)
    assert result["doc_type"] == "invoice"
    assert result["missing"] == [] and result["invalid"] == []
    assert result["fields"]["total_amount"]["value"] == "1,250.00"
    assert result["fields"]["total_amount"]["status"] == "found"


def test_missing_invoice_date_is_reported_and_other_fields_found():
    result = analyse_text(INVOICE_MISSING_DATE)
    assert result["doc_type"] == "invoice"
    assert result["missing"] == ["invoice_date"]
    assert result["missing_labels"] == ["Invoice date"]
    assert result["fields"]["invoice_number"]["value"] == "INV-2"  # matched via "Invoice No:"
    assert result["fields"]["total_amount"]["value"] == "900.00"    # matched via "Amount due:"


def test_invalid_date_and_amount_are_flagged():
    text = "Supplier invoice\nInvoice number: X-9\nInvoice date: 31/31/2026\nSupplier name: Gamma\nTotal amount: twelve thousand\n"
    result = analyse_text(text)
    assert set(result["invalid"]) == {"invoice_date", "total_amount"}
    assert result["missing"] == []


def test_currency_prefix_is_accepted():
    text = "Supplier invoice\nInvoice number: X-10\nInvoice date: 2026-11-03\nSupplier name: Delta\nTotal amount: INR 48,250.00\n"
    assert analyse_text(text)["fields"]["total_amount"]["status"] == "found"


def test_onboarding_empty_email_is_missing():
    result = analyse_text(ONBOARDING_EMPTY_EMAIL)
    assert result["doc_type"] == "onboarding_form"
    assert result["missing"] == ["work_email"]


def test_unrelated_document_is_unknown_not_guessed():
    result = analyse_text(NOTES)
    assert result["unknown"] is True
    assert result["doc_type"] is None
    assert result["fields"] == {}


# ----- workflow ----------------------------------------------------------------------

def test_workflow_proposes_tasks_only_for_gaps_and_waits_for_approval(env, kb):
    ok = kb.upload(env.analyst, "invoice_ok.txt", INVOICE_OK.encode())
    missing = kb.upload(env.analyst, "invoice_missing.txt", INVOICE_MISSING_DATE.encode())
    notes = kb.upload(env.analyst, "notes.txt", NOTES.encode())

    wf = env.service.start_document_workflow(env.analyst, "Process these documents and create review tasks",
                                             [ok["id"], missing["id"], notes["id"]])

    assert wf["status"] == "WAITING_FOR_APPROVAL"
    assert wf["intent"] == DOCUMENT_INTENT
    titles = [a["title"] for a in wf["approvals"]]
    assert len(titles) == 2
    assert any("invoice_missing.txt" in t and "invoice date" in t.lower() for t in titles)
    assert any("notes.txt" in t and "confirm document type" in t for t in titles)
    assert not any("invoice_ok.txt" in t for t in titles)
    assert _count(env.sf, Task) == 0  # nothing created before approval

    extractions = _step_output(wf, "doc_extract")["extractions"]
    by_name = {r["name"]: r for r in extractions}
    assert by_name["invoice_missing.txt"]["missing_labels"] == ["Invoice date"]
    assert by_name["notes.txt"]["unknown"] is True


def test_approved_review_tasks_are_created_unassigned_with_due_date(env, kb):
    doc = kb.upload(env.analyst, "invoice_missing.txt", INVOICE_MISSING_DATE.encode())
    wf = env.service.start_document_workflow(env.analyst, "Process this invoice", [doc["id"]])
    env.service.approve_all(env.approver, wf["id"])
    final = env.service.resume(env.approver, wf["id"])

    assert final["status"] == "COMPLETED"
    with env.sf() as s:
        tasks = s.scalars(select(Task)).all()
    assert len(tasks) == 1
    assert tasks[0].assignee_id is None            # no owner invented
    assert tasks[0].due_date is not None
    assert tasks[0].priority == "Medium"
    assert "Invoice date" in tasks[0].title
    assert not (env.settings.output_dir / "outbox").exists()  # no email involved


def test_rejected_review_task_is_not_created(env, kb):
    doc = kb.upload(env.analyst, "invoice_missing.txt", INVOICE_MISSING_DATE.encode())
    wf = env.service.start_document_workflow(env.analyst, "Process this invoice", [doc["id"]])
    env.service.decide(env.approver, wf["approvals"][0]["id"], "reject")
    final = env.service.resume(env.approver, wf["id"])
    assert final["status"] == "COMPLETED"
    assert _count(env.sf, Task) == 0


def test_second_run_skips_duplicate_review_tasks(env, kb):
    doc = kb.upload(env.analyst, "invoice_missing.txt", INVOICE_MISSING_DATE.encode())
    first = env.service.start_document_workflow(env.analyst, "Process this invoice", [doc["id"]])
    env.service.approve_all(env.approver, first["id"])
    env.service.resume(env.approver, first["id"])

    second = env.service.start_document_workflow(env.analyst, "Process this invoice again", [doc["id"]])
    assert second["approvals"] == []
    assert second["status"] == "COMPLETED"
    assert any("duplicate" in issue for issue in second["summary"]["unresolved_issues"])
    assert _count(env.sf, Task) == 1


def test_no_documents_asks_for_clarification(env):
    wf = env.service.start_document_workflow(env.analyst, "Process my documents", [])
    assert wf["status"] == "NEEDS_CLARIFICATION" or wf["clarification"] is not None
    assert wf["approvals"] == []


def test_unindexed_document_is_skipped_with_reason(env, kb):
    doc = kb.upload(env.analyst, "broken.pdf", b"%PDF-1.4 not really a pdf")
    assert doc["status"] == "FAILED"
    wf = env.service.start_document_workflow(env.analyst, "Process this file", [doc["id"]])
    assert wf["status"] == "NEEDS_CLARIFICATION"
    assert "not ready" in wf["clarification"]


def test_viewer_cannot_start_document_workflow(env, kb):
    doc = kb.upload(env.analyst, "invoice_ok.txt", INVOICE_OK.encode())
    with pytest.raises(PermissionDenied):
        env.service.start_document_workflow(env.viewer, "Process this invoice", [doc["id"]])


def test_document_workflow_over_http(settings):
    from fastapi.testclient import TestClient
    from app import db
    from app.api import create_app
    from app.auth import create_user
    from app.models import Role
    from app.seed import seed_reference_data

    client = TestClient(create_app(settings))
    with db.session_factory()() as s:
        seed_reference_data(s)
        analyst = create_user(s, "DP analyst", Role.ANALYST)
    headers = {"X-API-Key": analyst}
    up = client.post("/api/documents/upload", headers=headers,
                     files={"file": ("invoice_missing.txt", INVOICE_MISSING_DATE.encode(), "text/plain")})
    assert up.status_code == 201
    response = client.post("/api/document-workflows", headers=headers,
                           json={"request": "Process this invoice", "document_ids": [up.json()["id"]]})
    assert response.status_code == 201
    assert response.json()["status"] == "WAITING_FOR_APPROVAL"
    assert client.post("/api/document-workflows", headers=headers,
                       json={"request": "Process this invoice", "document_ids": []}).status_code == 422
