"""Thin query helpers shared by tools, graph nodes and the service layer."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Approval, Employee, Region, SalesRecord


def sales_rows(session: Session) -> list[dict]:
    stmt = (
        select(SalesRecord, Region, Employee)
        .join(Region, SalesRecord.region_id == Region.id)
        .outerjoin(Employee, Region.manager_id == Employee.id)
        .order_by(SalesRecord.week_start, Region.name)
    )
    rows = []
    for sales, region, manager in session.execute(stmt):
        rows.append(
            {
                "region_id": region.id,
                "region": region.name,
                "manager_id": manager.id if manager else None,
                "manager": manager.name if manager else None,
                "manager_email": manager.email if manager else None,
                "week_start": sales.week_start,
                "actual_sales": sales.actual_sales,
                "target_sales": sales.target_sales,
            }
        )
    return rows


def available_weeks(session: Session) -> list[date]:
    """Weeks that have at least one recorded actual value, oldest first."""
    stmt = (
        select(SalesRecord.week_start)
        .where(SalesRecord.actual_sales.is_not(None))
        .distinct()
        .order_by(SalesRecord.week_start)
    )
    return list(session.scalars(stmt))


def count_approvals(session: Session, workflow_id: str, status: str) -> int:
    return session.scalar(
        select(func.count()).select_from(Approval).where(
            Approval.workflow_id == workflow_id, Approval.status == status
        )
    ) or 0


def create_pending_approval(
    session: Session, *, workflow_id: str, action_type: str, target_key: str, title: str, payload: dict
) -> Approval:
    """Idempotent: re-running a planning step never creates duplicate approval requests."""
    existing = session.scalar(
        select(Approval).where(
            Approval.workflow_id == workflow_id,
            Approval.action_type == action_type,
            Approval.target_key == target_key,
        )
    )
    if existing is not None:
        return existing
    approval = Approval(
        workflow_id=workflow_id,
        action_type=action_type,
        target_key=target_key,
        title=title,
        payload=payload,
        status="PENDING",
    )
    session.add(approval)
    session.flush()
    return approval
