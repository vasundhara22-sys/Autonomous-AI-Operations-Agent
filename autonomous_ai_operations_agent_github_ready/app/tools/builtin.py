"""Builtin tools. Each function receives a ToolContext plus validated keyword arguments."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app import reporting
from app.docproc import engine as docengine
from app.leave import engine as leaveengine
from app.knowledge import engine as kb
from app import repository as repo
from app.analytics import compute_metrics
from app.errors import ProviderTransientError, ToolError, TransientToolError
from app.models import DraftStatus, Employee, EmailDraft, Region, Report, Task, utcnow
from app.providers import DatabaseTaskProvider
from app.tools.registry import Permission, ToolContext, ToolRegistry, ToolSpec

# ----- input schemas -------------------------------------------------------------

class ReadSalesInput(BaseModel):
    week_start: date | None = None


class MetricsInput(BaseModel):
    week_start: date
    threshold: float = Field(gt=0, le=1.5)


class RecipientInput(BaseModel):
    manager_id: int | None = None
    region_id: int | None = None


class TasksQueryInput(BaseModel):
    status: str | None = None
    region_id: int | None = None


class DraftInput(BaseModel):
    region_id: int
    subject: str = Field(min_length=3, max_length=300)
    body: str = Field(min_length=10, max_length=10000)


class SendEmailInput(BaseModel):
    draft_id: int


class ReportInput(BaseModel):
    metrics: dict


class CreateTaskInput(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=5000)
    assignee_id: int | None = None
    region_id: int | None = None
    priority: Literal["High", "Medium", "Low"] = "Medium"
    due_date: date | None = None


class UpdateTaskInput(BaseModel):
    task_id: int
    status: Literal["OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"]


# ----- helpers -------------------------------------------------------------------

def _output_dir(ctx: ToolContext) -> Path:
    sub = Path("workflows") / ctx.workflow_id if ctx.workflow_id else Path("reports")
    folder = ctx.settings.output_dir / sub
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _record_report(ctx: ToolContext, title: str, kind: str, period_week: str, path: Path) -> None:
    ctx.session.add(
        Report(
            workflow_id=ctx.workflow_id,
            title=title,
            kind=kind,
            period_start=date.fromisoformat(period_week),
            file_path=str(path),
        )
    )
    ctx.session.flush()


# ----- read-only tools -----------------------------------------------------------

def read_sales_data(ctx: ToolContext, week_start: date | None = None) -> dict:
    weeks = repo.available_weeks(ctx.session)
    week = week_start or (weeks[-1] if weeks else None)
    records = []
    if week is not None:
        for row in repo.sales_rows(ctx.session):
            if row["week_start"] == week:
                records.append({
                    "region": row["region"],
                    "manager": row["manager"],
                    "actual": row["actual_sales"],  # None stays None: never invented
                    "target": row["target_sales"],
                })
    return {
        "week_start": week.isoformat() if week else None,
        "available_weeks": [w.isoformat() for w in weeks],
        "record_count": len(records),
        "records": records,
    }


def calculate_sales_metrics(ctx: ToolContext, week_start: date, threshold: float) -> dict:
    rows = repo.sales_rows(ctx.session)
    try:
        return compute_metrics(rows, week_start, threshold, severe_threshold=ctx.settings.severe_underperformance_threshold)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def get_recipient_details(ctx: ToolContext, manager_id: int | None = None, region_id: int | None = None) -> dict:
    region_name = None
    if region_id is not None:
        region = ctx.session.get(Region, region_id)
        if region is None:
            raise ToolError("Region not found")
        manager_id, region_name = region.manager_id, region.name
    if manager_id is None:
        return {"manager_id": None, "manager": None, "email": None, "region": region_name}
    manager = ctx.session.get(Employee, manager_id)
    if manager is None:
        raise ToolError("Manager not found")
    return {"manager_id": manager.id, "manager": manager.name, "email": manager.email, "region": region_name}


def get_tasks(ctx: ToolContext, status: str | None = None, region_id: int | None = None) -> dict:
    return {"tasks": DatabaseTaskProvider().list_tasks(ctx.session, status=status, region_id=region_id)}


# ----- internal-write tools ------------------------------------------------------

def save_email_draft(ctx: ToolContext, region_id: int, subject: str, body: str) -> dict:
    if ctx.workflow_id is None:
        raise ToolError("Email drafts must belong to a workflow")
    region = ctx.session.get(Region, region_id)
    if region is None:
        raise ToolError("Region not found")
    manager = ctx.session.get(Employee, region.manager_id) if region.manager_id else None
    target_key = f"region:{region_id}"
    existing = ctx.session.scalar(
        select(EmailDraft).where(EmailDraft.workflow_id == ctx.workflow_id, EmailDraft.target_key == target_key)
    )
    if existing is not None:
        return {"draft_id": existing.id, "to_address": existing.to_address, "status": existing.status}
    draft = EmailDraft(
        workflow_id=ctx.workflow_id,
        target_key=target_key,
        recipient_id=manager.id if manager else None,
        to_address=manager.email if manager else None,
        subject=subject,
        body=body,
        status=DraftStatus.DRAFT,
    )
    ctx.session.add(draft)
    ctx.session.flush()
    return {"draft_id": draft.id, "to_address": draft.to_address, "status": draft.status}


def create_task(
    ctx: ToolContext,
    title: str,
    description: str = "",
    assignee_id: int | None = None,
    region_id: int | None = None,
    priority: str = "Medium",
    due_date: date | None = None,
) -> dict:
    return DatabaseTaskProvider().create(
        ctx.session,
        title=title,
        description=description,
        assignee_id=assignee_id,
        region_id=region_id,
        priority=priority,
        due_date=due_date,
        workflow_id=ctx.workflow_id,
    )


def update_task(ctx: ToolContext, task_id: int, status: str) -> dict:
    try:
        return DatabaseTaskProvider().update_status(ctx.session, task_id, status)
    except Exception as exc:  # NotFound and friends become a clean tool error
        raise ToolError(str(exc)) from exc


def generate_chart(ctx: ToolContext, metrics: dict) -> dict:
    folder = _output_dir(ctx)
    path = folder / f"regional_achievement_{metrics['period_week']}.png"
    reporting.write_chart(metrics, path)
    _record_report(ctx, "Regional achievement chart", "png", metrics["period_week"], path)
    return {"path": str(path)}


def generate_pdf_report(ctx: ToolContext, metrics: dict) -> dict:
    folder = _output_dir(ctx)
    chart = folder / f"regional_achievement_{metrics['period_week']}.png"
    reporting.write_chart(metrics, chart)
    path = folder / f"sales_report_{metrics['period_week']}.pdf"
    reporting.write_pdf(metrics, path, chart)
    _record_report(ctx, "Weekly sales performance report (PDF)", "pdf", metrics["period_week"], path)
    return {"path": str(path)}


def generate_excel_report(ctx: ToolContext, metrics: dict) -> dict:
    path = _output_dir(ctx) / f"sales_report_{metrics['period_week']}.xlsx"
    reporting.write_excel(metrics, path)
    _record_report(ctx, "Weekly sales performance workbook (Excel)", "xlsx", metrics["period_week"], path)
    return {"path": str(path)}


def generate_csv_report(ctx: ToolContext, metrics: dict) -> dict:
    path = _output_dir(ctx) / f"sales_regions_{metrics['period_week']}.csv"
    reporting.write_csv(metrics, path)
    _record_report(ctx, "Regional metrics (CSV)", "csv", metrics["period_week"], path)
    return {"path": str(path)}


# ----- external-action tools -----------------------------------------------------

def send_email(ctx: ToolContext, draft_id: int) -> dict:
    draft = ctx.session.get(EmailDraft, draft_id)
    if draft is None or draft.workflow_id != ctx.workflow_id:
        raise ToolError("Draft not found for this workflow")
    if ctx.approval is None or ctx.approval.payload.get("draft_id") != draft_id:
        raise ToolError("The approval does not cover this draft")
    if draft.status != DraftStatus.APPROVED:
        raise ToolError(f"Draft status is {draft.status}; only approved drafts can be sent")
    if not draft.to_address:
        raise ToolError("Draft has no recipient address on file")
    try:
        message_id = ctx.email_provider.send(to=draft.to_address, subject=draft.subject, body=draft.body)
    except ProviderTransientError as exc:
        raise TransientToolError(str(exc)) from exc
    draft.status = DraftStatus.SENT
    draft.provider_message_id = message_id
    draft.sent_at = utcnow()
    return {
        "draft_id": draft.id,
        "message_id": message_id,
        "provider": ctx.email_provider.name,
        "simulated": ctx.email_provider.name == "simulated",
    }


# ----- knowledge tools -----------------------------------------------------------

class IndexDocumentInput(BaseModel):
    document_id: int


class SearchDocumentsInput(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    document_ids: list[int] | None = None
    extensions: list[str] | None = None


class RetrieveContextInput(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)
    document_ids: list[int] | None = None


class SummarizeDocumentInput(BaseModel):
    document_id: int
    max_sentences: int = Field(default=5, ge=1, le=20)


def index_document(ctx: ToolContext, document_id: int) -> dict:
    return kb.index_document(ctx.session, ctx.settings, document_id)


def search_documents(ctx: ToolContext, query: str, top_k: int = 5, document_ids=None, extensions=None) -> dict:
    return {"results": kb.search(ctx.session, ctx.settings, query, top_k, document_ids, extensions)}


def retrieve_context(ctx: ToolContext, question: str, top_k: int = 3, document_ids=None) -> dict:
    return kb.retrieve_context(ctx.session, ctx.settings, question, top_k, document_ids)


def summarize_document(ctx: ToolContext, document_id: int, max_sentences: int = 5) -> dict:
    return kb.summarize_document(ctx.session, document_id, max_sentences)


class ExtractFieldsInput(BaseModel):
    document_id: int


def extract_document_fields(ctx: ToolContext, document_id: int) -> dict:
    return docengine.extract_document_fields(ctx.session, ctx.settings, document_id)


class LeaveStatusInput(BaseModel):
    as_of: date | None = None
    low_balance_days: float | None = Field(default=None, ge=0)
    upcoming_days: int | None = Field(default=None, ge=1, le=60)


class SaveEmployeeDraftInput(BaseModel):
    employee_id: int
    subject: str = Field(min_length=3, max_length=300)
    body: str = Field(min_length=10, max_length=10000)


def get_leave_status(ctx: ToolContext, as_of: date | None = None, low_balance_days: float | None = None,
                     upcoming_days: int | None = None) -> dict:
    return leaveengine.leave_status(
        ctx.session,
        as_of or date.today(),
        low_balance_days if low_balance_days is not None else ctx.settings.leave_low_balance_days,
        upcoming_days or ctx.settings.leave_upcoming_days,
    )


def save_employee_email_draft(ctx: ToolContext, employee_id: int, subject: str, body: str) -> dict:
    if ctx.workflow_id is None:
        raise ToolError("Email drafts must belong to a workflow")
    employee = ctx.session.get(Employee, employee_id)
    if employee is None:
        raise ToolError("Employee not found")
    target_key = f"employee:{employee_id}"
    existing = ctx.session.scalar(
        select(EmailDraft).where(EmailDraft.workflow_id == ctx.workflow_id, EmailDraft.target_key == target_key)
    )
    if existing is not None:
        return {"draft_id": existing.id, "to_address": existing.to_address, "status": existing.status}
    draft = EmailDraft(
        workflow_id=ctx.workflow_id,
        target_key=target_key,
        recipient_id=employee.id,
        to_address=employee.email,
        subject=subject,
        body=body,
        status=DraftStatus.DRAFT,
    )
    ctx.session.add(draft)
    ctx.session.flush()
    return {"draft_id": draft.id, "to_address": draft.to_address, "status": draft.status}


# ----- registration --------------------------------------------------------------

def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    specs = [
        ToolSpec("read_sales_data", "Read weekly sales records (missing actuals stay null).", Permission.READ_ONLY, ReadSalesInput, read_sales_data),
        ToolSpec("calculate_sales_metrics", "Compute regional KPIs and underperformance flags.", Permission.READ_ONLY, MetricsInput, calculate_sales_metrics),
        ToolSpec("get_recipient_details", "Look up a manager's name and email from the directory.", Permission.READ_ONLY, RecipientInput, get_recipient_details),
        ToolSpec("get_tasks", "List tasks, optionally filtered by status or region.", Permission.READ_ONLY, TasksQueryInput, get_tasks),
        ToolSpec("save_email_draft", "Save an email draft for a region's manager. Does not send.", Permission.INTERNAL_WRITE, DraftInput, save_email_draft),
        ToolSpec("create_task", "Create a follow-up task. Requires approval.", Permission.INTERNAL_WRITE, CreateTaskInput, create_task, requires_approval=True),
        ToolSpec("update_task", "Change a task's status. Requires approval.", Permission.INTERNAL_WRITE, UpdateTaskInput, update_task, requires_approval=True),
        ToolSpec("generate_pdf_report", "Write the PDF report and its chart.", Permission.INTERNAL_WRITE, ReportInput, generate_pdf_report),
        ToolSpec("generate_excel_report", "Write the Excel workbook.", Permission.INTERNAL_WRITE, ReportInput, generate_excel_report),
        ToolSpec("generate_csv_report", "Write the regional CSV.", Permission.INTERNAL_WRITE, ReportInput, generate_csv_report),
        ToolSpec("generate_chart", "Write the regional achievement chart (PNG).", Permission.INTERNAL_WRITE, ReportInput, generate_chart),
        ToolSpec("send_email", "Send an APPROVED draft via the email provider. Requires approval.", Permission.EXTERNAL_ACTION, SendEmailInput, send_email, requires_approval=True),
    ]
    specs += [
        ToolSpec("index_document", "Extract, chunk and index an uploaded document.", Permission.INTERNAL_WRITE, IndexDocumentInput, index_document),
        ToolSpec("search_documents", "Search indexed documents; returns ranked passages with sources.", Permission.READ_ONLY, SearchDocumentsInput, search_documents),
        ToolSpec("retrieve_context", "Answer a question using only uploaded document text, with citations.", Permission.READ_ONLY, RetrieveContextInput, retrieve_context),
        ToolSpec("summarize_document", "Extractive summary of one indexed document.", Permission.READ_ONLY, SummarizeDocumentInput, summarize_document),
    ]
    specs += [
        ToolSpec("extract_document_fields", "Detect a document's type, pull out its key fields, and list missing or invalid ones.", Permission.READ_ONLY, ExtractFieldsInput, extract_document_fields),
    ]
    specs += [
        ToolSpec("get_leave_status", "Find employees with pending leave, upcoming approved leave, or a low balance.", Permission.READ_ONLY, LeaveStatusInput, get_leave_status),
        ToolSpec("save_employee_email_draft", "Save an email draft addressed to an employee's own record. Does not send.", Permission.INTERNAL_WRITE, SaveEmployeeDraftInput, save_employee_email_draft),
    ]
    for spec in specs:
        registry.register(spec)
    return registry
