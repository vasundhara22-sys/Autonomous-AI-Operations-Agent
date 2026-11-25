from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Role:
    VIEWER = "VIEWER"
    ANALYST = "ANALYST"
    APPROVER = "APPROVER"


class WorkflowStatus:
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    IN_PROGRESS = "IN_PROGRESS"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ApprovalStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DraftStatus:
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SENT = "SENT"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class TaskStatus:
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ToolStatus:
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Region(Base):
    __tablename__ = "regions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)


class SalesRecord(Base):
    __tablename__ = "sales_records"
    __table_args__ = (
        UniqueConstraint("region_id", "week_start", name="uq_sales_region_week"),
        Index("ix_sales_week", "week_start"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    week_start: Mapped[date] = mapped_column(Date)
    actual_sales: Mapped[float | None] = mapped_column(Float, nullable=True)  # NULL = not recorded
    target_sales: Mapped[float] = mapped_column(Float)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="Medium")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.OPEN, index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True)
    intent: Mapped[str | None] = mapped_column(String(60), nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    clarification: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("workflow_id", "step_key", name="uq_step_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(60))
    agent: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("workflow_id", "action_type", "target_key", name="uq_approval_action"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(60))
    target_key: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmailDraft(Base):
    __tablename__ = "email_drafts"
    __table_args__ = (UniqueConstraint("workflow_id", "target_key", name="uq_draft_target"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    target_key: Mapped[str] = mapped_column(String(120))
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    to_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(60))
    permission: Mapped[str] = mapped_column(String(30))
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionLog(Base):
    __tablename__ = "execution_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    agent: Mapped[str] = mapped_column(String(60))
    tool: Mapped[str | None] = mapped_column(String(60), nullable=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    file_path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_name: Mapped[str] = mapped_column(String(200))
    stored_path: Mapped[str] = mapped_column(String(500))
    extension: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunk_position"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    term_counts: Mapped[dict] = mapped_column(JSON)
    suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), index=True)  # PENDING | APPROVED | REJECTED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LeaveBalance(Base):
    __tablename__ = "leave_balances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), unique=True)
    annual_entitlement: Mapped[float] = mapped_column(Float)
    annual_used: Mapped[float] = mapped_column(Float, default=0.0)
