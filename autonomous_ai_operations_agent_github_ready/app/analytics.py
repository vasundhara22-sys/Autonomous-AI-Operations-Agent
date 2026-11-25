"""Pure sales analytics. No database access here, so every rule is unit-testable."""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import pandas as pd

METHODOLOGY = (
    "Achievement is actual sales divided by target sales for the reporting week. "
    "A region is UNDERPERFORMING when achievement is below the configured threshold. "
    "Regions with a missing actual or target value are marked DATA_MISSING, excluded from totals, "
    "and never estimated. Week-over-week growth compares actual sales with the previous week."
)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return str(value)


def _int_or_none(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def suggest_follow_up(achievement: float, wow_growth: float | None, severe_threshold: float) -> str:
    if achievement < severe_threshold:
        text = (
            "Escalate to a regional recovery review within 48 hours; "
            "confirm pipeline coverage and any blocked deals."
        )
    else:
        text = "Review pipeline coverage and conversion rates, and agree a two-week recovery plan with the regional manager."
    if wow_growth is not None and wow_growth < 0:
        text += " Sales also fell week-over-week, so prioritize a root-cause review."
    return text


def compute_metrics(
    rows: list[dict],
    period_week,
    threshold: float,
    *,
    severe_threshold: float = 0.60,
    trend_weeks: int = 6,
) -> dict:
    """Compute regional KPIs for one reporting week.

    `rows` are dicts with: region_id, region, manager_id, manager, manager_email,
    week_start (date), actual_sales (float|None), target_sales (float).
    """
    if not rows:
        raise ValueError("No sales records are available")

    df = pd.DataFrame(rows)
    df["week_start"] = pd.to_datetime(df["week_start"]).dt.date
    df["actual_sales"] = pd.to_numeric(df["actual_sales"], errors="coerce")
    df["target_sales"] = pd.to_numeric(df["target_sales"], errors="coerce")

    prev_week = period_week - timedelta(weeks=1)
    current = df[df["week_start"] == period_week].sort_values("region")
    if current.empty:
        raise ValueError(f"No sales records for week of {period_week.isoformat()}")
    previous = df[df["week_start"] == prev_week].set_index("region")

    regions: list[dict] = []
    for _, row in current.iterrows():
        name = row["region"]
        actual = _num(row["actual_sales"])
        target = _num(row["target_sales"])
        prev_actual = _num(previous.at[name, "actual_sales"]) if name in previous.index else None

        wow = None
        if actual is not None and prev_actual not in (None, 0):
            wow = (actual - prev_actual) / prev_actual

        if actual is None or target is None or target <= 0:
            status, achievement, gap = "DATA_MISSING", None, None
        else:
            achievement = actual / target
            gap = target - actual
            status = "UNDERPERFORMING" if achievement < threshold else "ON_TRACK"

        regions.append(
            {
                "region_id": _int_or_none(row["region_id"]),
                "region": name,
                "manager_id": _int_or_none(row["manager_id"]),
                "manager": _text(row["manager"]),
                "manager_email": _text(row["manager_email"]),
                "actual": _round(actual),
                "target": _round(target),
                "achievement": _round(achievement, 4),
                "gap_to_target": _round(gap),
                "prev_actual": _round(prev_actual),
                "wow_growth": _round(wow, 4),
                "status": status,
                "suggested_follow_up": (
                    suggest_follow_up(achievement, wow, severe_threshold) if status == "UNDERPERFORMING" else None
                ),
            }
        )

    evaluated = [r for r in regions if r["status"] != "DATA_MISSING"]
    total_actual = sum(r["actual"] for r in evaluated)
    total_target = sum(r["target"] for r in evaluated)
    overall = total_actual / total_target if total_target else None

    underperformers = sorted(
        (r for r in evaluated if r["status"] == "UNDERPERFORMING"), key=lambda r: r["achievement"]
    )
    data_gaps = [
        {
            "region": r["region"],
            "issue": f"No actual sales recorded for week of {period_week.isoformat()}; excluded from totals, not estimated",
        }
        for r in regions
        if r["status"] == "DATA_MISSING"
    ]

    trend = []
    for week in sorted({w for w in df["week_start"] if w <= period_week})[-trend_weeks:]:
        subset = df[df["week_start"] == week].dropna(subset=["actual_sales", "target_sales"])
        t = float(subset["target_sales"].sum())
        a = float(subset["actual_sales"].sum())
        trend.append(
            {
                "week_start": week.isoformat(),
                "achievement": round(a / t, 4) if t else None,
                "regions_evaluated": int(len(subset)),
            }
        )

    declining = [r["region"] for r in evaluated if r["wow_growth"] is not None and r["wow_growth"] < 0]
    observations: list[str] = []
    if overall is not None:
        observations.append(
            f"Overall achievement is {overall:.1%} of target across {len(evaluated)} evaluated region(s)."
        )
    if underperformers:
        names = ", ".join(r["region"] for r in underperformers)
        observations.append(
            f"{len(underperformers)} region(s) are below the {threshold:.0%} threshold: {names}. "
            f"Lowest is {underperformers[0]['region']} at {underperformers[0]['achievement']:.1%}."
        )
    else:
        observations.append(f"No evaluated region is below the {threshold:.0%} threshold.")
    if declining:
        observations.append(f"Sales fell week-over-week in: {', '.join(declining)}.")
    if data_gaps:
        observations.append(
            f"{len(data_gaps)} region(s) have missing actuals and were excluded rather than estimated."
        )

    recommendations = [f"{r['region']}: {r['suggested_follow_up']}" for r in underperformers] or [
        "No region requires escalation this week; maintain current plans and monitor the trend."
    ]

    return {
        "period_week": period_week.isoformat(),
        "period_end": (period_week + timedelta(days=6)).isoformat(),
        "comparison_week": prev_week.isoformat(),
        "threshold": threshold,
        "methodology": f"{METHODOLOGY} Threshold used: {threshold:.0%} of target.",
        "data_sources": ["SalesRecord table (sample data in this build)", "Employee and Region reference tables"],
        "totals": {
            "actual": _round(total_actual),
            "target": _round(total_target),
            "achievement": _round(overall, 4),
            "regions_evaluated": len(evaluated),
            "regions_total": len(regions),
        },
        "regions": regions,
        "underperformers": underperformers,
        "data_gaps": data_gaps,
        "trend": trend,
        "observations": observations,
        "recommendations": recommendations,
    }
