from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import ExecutionLog, utcnow

logger = logging.getLogger("ops_agent")

_SENSITIVE_KEYS = ("token", "secret", "password", "api_key", "authorization")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_MAX_TEXT = 500


def mask_sensitive(value: Any) -> Any:
    """Mask credentials and partially mask email addresses before anything is persisted."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _SENSITIVE_KEYS):
                out[key] = "***"
            else:
                out[key] = mask_sensitive(item)
        return out
    if isinstance(value, (list, tuple)):
        return [mask_sensitive(item) for item in value]
    if isinstance(value, str):
        masked = _EMAIL.sub(lambda m: f"{m.group(1)}***{m.group(2)}", value)
        return masked if len(masked) <= _MAX_TEXT else masked[:_MAX_TEXT] + "...[truncated]"
    return value


def log_event(
    session: Session,
    *,
    workflow_id: str | None,
    agent: str,
    message: str,
    tool: str | None = None,
    level: str = "INFO",
    data: dict | None = None,
) -> None:
    """Add an execution-log row. The caller is responsible for committing."""
    session.add(
        ExecutionLog(
            workflow_id=workflow_id,
            timestamp=utcnow(),
            agent=agent,
            tool=tool,
            level=level,
            message=message[:2000],
            data=mask_sensitive(data) if data is not None else None,
        )
    )
    logger.log(getattr(logging, level, logging.INFO), "[%s] %s", agent, message)
