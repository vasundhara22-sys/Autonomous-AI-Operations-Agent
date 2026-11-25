"""Tool registry: the only sanctioned path from agents to data, files and external systems.

Enforced here (not in prompts or the UI):
  * argument validation against a Pydantic schema
  * permission levels: READ_ONLY, INTERNAL_WRITE, EXTERNAL_ACTION
  * approval gating: tools marked requires_approval need an APPROVED approval for this workflow
  * idempotency keys: a completed action is never re-executed; an interrupted one is flagged for review
  * bounded retries with exponential backoff for transient failures only
  * audit logging with sensitive values masked
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import (
    ApprovalRequired,
    InvalidToolArguments,
    ProviderTransientError,
    ToolError,
    TransientToolError,
)
from app.logging_utils import log_event, mask_sensitive
from app.models import ApprovalStatus, ToolExecution, ToolStatus, utcnow


class Permission:
    READ_ONLY = "read_only"
    INTERNAL_WRITE = "internal_write"
    EXTERNAL_ACTION = "external_action"


@dataclass
class ToolContext:
    session: Session
    settings: Settings
    agent: str
    workflow_id: str | None
    email_provider: Any
    approval: Any = None  # the Approval row authorising this call, if any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission: str
    input_model: type[BaseModel]
    func: Callable[..., Any]
    requires_approval: bool = False


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _short_errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:500]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def describe(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "permission": s.permission,
                "requires_approval": s.requires_approval,
                "input_schema": s.input_model.model_json_schema(),
            }
            for s in self._tools.values()
        ]

    def invoke(self, name: str, arguments: dict | None, ctx: ToolContext, idempotency_key: str | None = None) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(f"Unknown tool '{name}'")

        try:
            parsed = spec.input_model.model_validate(arguments or {})
        except ValidationError as exc:
            raise InvalidToolArguments(f"Invalid arguments for {name}: {_short_errors(exc)}") from None
        call_args_py = parsed.model_dump()          # python objects for the function
        call_args_store = parsed.model_dump(mode="json")  # JSON-safe for storage and logs

        if spec.requires_approval:
            approval = ctx.approval
            authorised = (
                approval is not None
                and approval.status == ApprovalStatus.APPROVED
                and approval.workflow_id == ctx.workflow_id
                and approval.action_type == name
            )
            if not authorised:
                log_event(
                    ctx.session, workflow_id=ctx.workflow_id, agent=ctx.agent, tool=name, level="WARNING",
                    message=f"Blocked '{name}': no approved human decision for this action",
                )
                ctx.session.commit()
                raise ApprovalRequired(f"Tool '{name}' requires an approved human decision")

        row = None
        if idempotency_key:
            row = ctx.session.scalar(select(ToolExecution).where(ToolExecution.idempotency_key == idempotency_key))
            if row is not None:
                if row.status == ToolStatus.SUCCEEDED:
                    log_event(
                        ctx.session, workflow_id=ctx.workflow_id, agent=ctx.agent, tool=name,
                        message="Idempotent replay: action already completed, returning stored result",
                    )
                    ctx.session.commit()
                    return row.result
                if row.status in (ToolStatus.RUNNING, ToolStatus.NEEDS_REVIEW):
                    # The previous attempt may have succeeded before the crash. Never repeat it blindly.
                    row.status = ToolStatus.NEEDS_REVIEW
                    row.error = "Previous attempt was interrupted; outcome unknown. Verify in the target system before retrying."
                    log_event(
                        ctx.session, workflow_id=ctx.workflow_id, agent=ctx.agent, tool=name, level="ERROR",
                        message=f"Blocked retry of '{name}': previous attempt interrupted, manual verification needed",
                    )
                    ctx.session.commit()
                    raise ToolError(row.error)
                # FAILED: safe to retry, reuse the row

        if row is None:
            row = ToolExecution(
                idempotency_key=idempotency_key,
                workflow_id=ctx.workflow_id,
                tool_name=name,
                permission=spec.permission,
                arguments=mask_sensitive(call_args_store),
                status=ToolStatus.RUNNING,
                attempts=0,
            )
            ctx.session.add(row)
        row.status = ToolStatus.RUNNING
        row.error = None
        ctx.session.commit()  # persist RUNNING before the side effect, so a crash is detectable

        max_attempts = ctx.settings.max_retries
        attempt = 0
        while True:
            attempt += 1
            row.attempts = attempt
            ctx.session.commit()
            try:
                result = spec.func(ctx, **call_args_py)
                break
            except (TransientToolError, ProviderTransientError) as exc:
                ctx.session.rollback()
                if attempt >= max_attempts:
                    return self._fail(ctx, row, spec, f"gave up after {attempt} attempts: {exc}")
                delay = ctx.settings.retry_base_delay_seconds * (2 ** (attempt - 1))
                log_event(
                    ctx.session, workflow_id=ctx.workflow_id, agent=ctx.agent, tool=name, level="WARNING",
                    message=f"Transient failure on attempt {attempt}; retrying in {delay:.2f}s",
                )
                ctx.session.commit()
                time.sleep(delay)
            except Exception as exc:  # non-retryable: record and surface a clean message
                ctx.session.rollback()
                return self._fail(ctx, row, spec, str(exc) or exc.__class__.__name__)

        output = _json_safe(result)
        row.status = ToolStatus.SUCCEEDED
        row.result = output
        row.finished_at = utcnow()
        log_event(
            ctx.session, workflow_id=ctx.workflow_id, agent=ctx.agent, tool=name,
            message=f"Tool '{name}' succeeded", data={"permission": spec.permission, "attempts": attempt},
        )
        ctx.session.commit()
        return output

    def _fail(self, ctx: ToolContext, row: ToolExecution, spec: ToolSpec, message: str):
        row.status = ToolStatus.FAILED
        row.error = message[:1000]
        row.finished_at = utcnow()
        log_event(
            ctx.session, workflow_id=ctx.workflow_id, agent=ctx.agent, tool=spec.name, level="ERROR",
            message=f"Tool '{spec.name}' failed: {message}",
        )
        ctx.session.commit()
        raise ToolError(f"{spec.name} failed: {message}")
