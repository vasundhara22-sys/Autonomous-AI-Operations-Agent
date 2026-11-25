"""Deterministic sample data: 6 regions, 6 managers, 7 weeks, one intentional data gap.

This is SAMPLE data for demonstration only. It is not real company data.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from sqlalchemy import func, select

from app.models import Employee, LeaveBalance, LeaveRequest, Region, SalesRecord

# region, manager, manager email (None = missing on file), achievement profile
REGION_PROFILES = [
    ("North", "Asha Verma", "asha.verma@example.com", 1.06),
    ("South", "Rahul Menon", "rahul.menon@example.com", 0.70),
    ("East", "Priya Nair", "priya.nair@example.com", 0.96),
    ("West", "Karan Shah", "karan.shah@example.com", 0.88),
    ("Central", "Neha Joshi", None, 0.74),
    ("Metro", "Vikram Rao", "vikram.rao@example.com", 1.10),
]
FIRST_WEEK = date(2026, 9, 28)
WEEKS = 7
MISSING_ACTUALS = {("Metro", date(2026, 11, 9))}


def seed_reference_data(session, seed: int = 42) -> bool:
    """Insert sample data once. Returns False if data already exists."""
    if session.scalar(select(func.count()).select_from(Region)):
        return False
    rng = random.Random(seed)
    for region_name, manager, email, factor in REGION_PROFILES:
        employee = Employee(name=manager, email=email, title="Regional Manager")
        session.add(employee)
        session.flush()
        region = Region(name=region_name, manager_id=employee.id)
        session.add(region)
        session.flush()
        for week_index in range(WEEKS):
            week = FIRST_WEEK + timedelta(weeks=week_index)
            target = float(rng.randrange(110_000, 150_000, 1_000))
            noise = rng.uniform(0.97, 1.03)
            if (region_name, week) in MISSING_ACTUALS:
                actual = None
            else:
                actual = float(round(target * factor * noise, -2))
            session.add(SalesRecord(region_id=region.id, week_start=week, actual_sales=actual, target_sales=target))
    session.commit()
    return True


# employee name: (annual leave used, [(type, days from as_of, length in days, status)])
LEAVE_PROFILES = {
    "Rahul Menon": (4.0, [("Annual leave", 10, 3, "PENDING")]),
    "Priya Nair": (6.0, [("Annual leave", 3, 2, "APPROVED")]),
    "Karan Shah": (16.0, []),
    "Neha Joshi": (5.0, [("Sick leave", 9, 1, "PENDING")]),
}


def seed_leave_data(session, as_of: date | None = None) -> bool:
    """Insert sample leave balances and requests once. Dates are relative to as_of (default: today)."""
    if session.scalar(select(func.count()).select_from(LeaveBalance)):
        return False
    employees = list(session.scalars(select(Employee)))
    if not employees:
        return False
    start = as_of or date.today()
    for employee in employees:
        used, requests = LEAVE_PROFILES.get(employee.name, (0.0, []))
        session.add(LeaveBalance(employee_id=employee.id, annual_entitlement=18.0, annual_used=used))
        for leave_type, offset, length, status in requests:
            first = start + timedelta(days=offset)
            session.add(LeaveRequest(
                employee_id=employee.id, leave_type=leave_type, start_date=first,
                end_date=first + timedelta(days=length - 1), days=float(length), status=status,
            ))
    session.commit()
    return True
