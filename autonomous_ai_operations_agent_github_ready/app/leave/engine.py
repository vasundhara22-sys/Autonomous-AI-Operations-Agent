"""Leave rules: who has a pending request, an upcoming approved leave, or a low balance."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models import Employee, LeaveBalance, LeaveRequest


def _describe(request: LeaveRequest) -> dict:
    return {
        "type": request.leave_type,
        "start": request.start_date.isoformat(),
        "end": request.end_date.isoformat(),
        "days": request.days,
    }


def leave_status(session, as_of: date, low_balance_days: float, upcoming_days: int) -> dict:
    employees = list(session.scalars(select(Employee).order_by(Employee.name)))
    balances = {b.employee_id: b for b in session.scalars(select(LeaveBalance))}
    by_employee: dict[int, list[LeaveRequest]] = {}
    for request in session.scalars(select(LeaveRequest)):
        by_employee.setdefault(request.employee_id, []).append(request)

    horizon = as_of + timedelta(days=upcoming_days)
    rows = []
    for employee in employees:
        requests = by_employee.get(employee.id, [])
        pending = [r for r in requests if r.status == "PENDING"]
        upcoming = [r for r in requests if r.status == "APPROVED" and as_of <= r.start_date <= horizon]
        balance = balances.get(employee.id)
        remaining = round(balance.annual_entitlement - balance.annual_used, 2) if balance else None
        low = remaining is not None and remaining < low_balance_days
        if not (pending or upcoming or low):
            continue
        rows.append({
            "employee_id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "pending": [_describe(r) for r in pending],
            "upcoming": [_describe(r) for r in upcoming],
            "remaining_days": remaining,
            "low_balance": low,
        })
    return {"as_of": as_of.isoformat(), "employees": rows}
