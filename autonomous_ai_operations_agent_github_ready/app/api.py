"""FastAPI backend. Thin HTTP layer: validation, authentication, and mapping errors to status codes.

All business rules live in WorkflowService. Responses never include stack traces or file paths.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Header, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import db
from app.agents_catalog import AGENTS, get_agent
from app.auth import Principal
from app.config import Settings, get_settings
from app.errors import NotFound, OpsError, Unauthenticated
from app.tools.builtin import build_registry
from app.knowledge.service import KnowledgeService
from app.seed import seed_leave_data
from app.workflow.service import WorkflowService


class WorkflowCreate(BaseModel):
    request: str = Field(min_length=5, max_length=2000)


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    edits: dict[str, Any] | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=5000)
    assignee_id: int | None = None
    region_id: int | None = None
    priority: Literal["High", "Medium", "Low"] = "Medium"
    due_date: date | None = None


class TaskUpdate(BaseModel):
    status: Literal["OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"]


class DocumentWorkflowCreate(BaseModel):
    request: str = Field(min_length=5, max_length=2000)
    document_ids: list[int] = Field(min_length=1, max_length=25)


class KnowledgeSearch(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    document_ids: list[int] | None = None
    extensions: list[str] | None = None


class KnowledgeAnswer(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)
    document_ids: list[int] | None = None


class LeaveWorkflowCreate(BaseModel):
    request: str = Field(min_length=5, max_length=2000)


class ReportIn(BaseModel):
    week_start: date | None = None
    threshold: float | None = Field(default=None, gt=0, le=1.5)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db.configure(settings.database_url)
    service = WorkflowService(db.session_factory(), settings)
    knowledge = KnowledgeService(db.session_factory(), settings)
    with db.session_factory()() as _seed_session:
        seed_leave_data(_seed_session)

    app = FastAPI(
        title="Autonomous AI Operations Agent API",
        version="0.1.0",
        description="Natural-language business workflows with planning, tool calling, and human approval.",
    )

    @app.exception_handler(OpsError)
    async def ops_error_handler(request, exc: OpsError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    def principal(x_api_key: str | None = Header(default=None)) -> Principal:
        if not x_api_key:
            raise Unauthenticated("Missing X-API-Key header")
        return service.authenticate(x_api_key)

    # ----- system -----------------------------------------------------------------

    @app.get("/api/health", tags=["system"])
    def health():
        return service.health()

    @app.get("/api/tools", tags=["system"])
    def tools(_: Principal = Depends(principal)):
        return build_registry().describe()

    # ----- workflows --------------------------------------------------------------

    @app.post("/api/workflows", status_code=201, tags=["workflows"])
    def create_workflow(body: WorkflowCreate, p: Principal = Depends(principal)):
        return service.start_workflow(p, body.request)

    @app.get("/api/workflows", tags=["workflows"])
    def list_workflows(limit: int = Query(50, ge=1, le=500), p: Principal = Depends(principal)):
        return service.list_workflows(p, limit)

    @app.get("/api/workflows/{workflow_id}", tags=["workflows"])
    def get_workflow(workflow_id: str, p: Principal = Depends(principal)):
        return service.get_workflow(p, workflow_id)

    @app.post("/api/workflows/{workflow_id}/resume", tags=["workflows"])
    def resume_workflow(workflow_id: str, p: Principal = Depends(principal)):
        return service.resume(p, workflow_id)

    @app.post("/api/workflows/{workflow_id}/cancel", tags=["workflows"])
    def cancel_workflow(workflow_id: str, p: Principal = Depends(principal)):
        return service.cancel(p, workflow_id)

    @app.post("/api/workflows/{workflow_id}/approve-all", tags=["workflows"])
    def approve_all(workflow_id: str, p: Principal = Depends(principal)):
        return service.approve_all(p, workflow_id)

    # ----- approvals --------------------------------------------------------------

    @app.get("/api/approvals", tags=["approvals"])
    def list_approvals(
        status: str | None = None, workflow_id: str | None = None, p: Principal = Depends(principal)
    ):
        return service.list_approvals(p, status=status, workflow_id=workflow_id)

    @app.post("/api/approvals/{approval_id}/approve", tags=["approvals"])
    def approve(approval_id: int, body: DecisionIn | None = None, p: Principal = Depends(principal)):
        body = body or DecisionIn()
        return service.decide(p, approval_id, "approve", note=body.note, edits=body.edits)

    @app.post("/api/approvals/{approval_id}/reject", tags=["approvals"])
    def reject(approval_id: int, body: DecisionIn | None = None, p: Principal = Depends(principal)):
        body = body or DecisionIn()
        return service.decide(p, approval_id, "reject", note=body.note)

    # ----- agents -----------------------------------------------------------------

    @app.get("/api/agents", tags=["agents"])
    def list_agents(_: Principal = Depends(principal)):
        return AGENTS

    @app.get("/api/agents/{agent_id}", tags=["agents"])
    def agent_detail(agent_id: str, _: Principal = Depends(principal)):
        agent = get_agent(agent_id)
        if agent is None:
            raise NotFound("Agent not found")
        return agent

    # ----- tasks ------------------------------------------------------------------

    @app.get("/api/tasks", tags=["tasks"])
    def list_tasks(status: str | None = None, region_id: int | None = None, p: Principal = Depends(principal)):
        return service.list_tasks(p, status=status, region_id=region_id)

    @app.post("/api/tasks", status_code=201, tags=["tasks"])
    def create_task(body: TaskCreate, p: Principal = Depends(principal)):
        return service.create_task(p, body.model_dump())

    @app.put("/api/tasks/{task_id}", tags=["tasks"])
    def update_task(task_id: int, body: TaskUpdate, p: Principal = Depends(principal)):
        return service.update_task(p, task_id, body.status)

    # ----- analytics, reports, logs -----------------------------------------------

    @app.get("/api/sales/metrics", tags=["analytics"])
    def sales_metrics(
        week_start: date | None = None,
        threshold: float | None = Query(None, gt=0, le=1.5),
        p: Principal = Depends(principal),
    ):
        return service.sales_metrics(p, week_start, threshold)

    @app.get("/api/reports", tags=["reports"])
    def list_reports(p: Principal = Depends(principal)):
        return service.list_reports(p)

    @app.post("/api/reports/generate", status_code=201, tags=["reports"])
    def generate_report(body: ReportIn | None = None, p: Principal = Depends(principal)):
        body = body or ReportIn()
        return service.generate_report(p, body.week_start, body.threshold)

    @app.get("/api/reports/{report_id}/download", tags=["reports"])
    def download_report(report_id: int, p: Principal = Depends(principal)):
        path = service.report_path(p, report_id)
        return FileResponse(path, filename=path.name)

    @app.get("/api/logs", tags=["logs"])
    def list_logs(
        workflow_id: str | None = None, limit: int = Query(200, ge=1, le=1000), p: Principal = Depends(principal)
    ):
        return service.list_logs(p, workflow_id=workflow_id, limit=limit)

    # ----- leave updates -----------------------------------------------------------

    @app.post("/api/leave-workflows", status_code=201, tags=["workflows"])
    def create_leave_workflow(body: LeaveWorkflowCreate, p: Principal = Depends(principal)):
        return service.start_leave_workflow(p, body.request)

    # ----- document processing workflow -------------------------------------------

    @app.post("/api/document-workflows", status_code=201, tags=["workflows"])
    def create_document_workflow(body: DocumentWorkflowCreate, p: Principal = Depends(principal)):
        return service.start_document_workflow(p, body.request, body.document_ids)

    # ----- knowledge base (document search) ---------------------------------------

    @app.post("/api/documents/upload", status_code=201, tags=["knowledge"])
    def upload_document(file: UploadFile = File(...), p: Principal = Depends(principal)):
        # Read one byte past the limit so the service can reject oversized files without loading them all.
        content = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        return knowledge.upload(p, file.filename, content)

    @app.get("/api/documents", tags=["knowledge"])
    def list_documents(p: Principal = Depends(principal)):
        return knowledge.list_documents(p)

    @app.get("/api/documents/{document_id}", tags=["knowledge"])
    def get_document(document_id: int, p: Principal = Depends(principal)):
        return knowledge.get_document(p, document_id)

    @app.get("/api/documents/{document_id}/summary", tags=["knowledge"])
    def summarize_document(
        document_id: int, max_sentences: int = Query(5, ge=1, le=20), p: Principal = Depends(principal)
    ):
        return knowledge.summarize(p, document_id, max_sentences)

    @app.post("/api/knowledge/search", tags=["knowledge"])
    def search_knowledge(body: KnowledgeSearch, p: Principal = Depends(principal)):
        return knowledge.search(p, body.query, body.top_k, body.document_ids, body.extensions)

    @app.post("/api/knowledge/answer", tags=["knowledge"])
    def answer_knowledge(body: KnowledgeAnswer, p: Principal = Depends(principal)):
        return knowledge.answer(p, body.question, body.top_k, body.document_ids)

    return app
