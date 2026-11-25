from datetime import date

import pytest

from app.analytics import compute_metrics

W1, W2 = date(2026, 11, 2), date(2026, 11, 9)


def _row(region_id, region, week, actual, target, manager="Mgr", email="m@example.com"):
    return {
        "region_id": region_id, "region": region, "manager_id": region_id, "manager": manager,
        "manager_email": email, "week_start": week, "actual_sales": actual, "target_sales": target,
    }


ROWS = [
    _row(1, "A", W1, 100, 100), _row(1, "A", W2, 70, 100),   # 70% -> underperforming, WoW -30%
    _row(2, "B", W1, 90, 100), _row(2, "B", W2, 95, 100),    # 95% -> on track, WoW +5.6%
    _row(3, "C", W1, 80, 100), _row(3, "C", W2, None, 100),  # missing actual -> DATA_MISSING
]


def test_achievement_and_status_classification():
    m = compute_metrics(ROWS, W2, 0.80)
    by_region = {r["region"]: r for r in m["regions"]}
    assert by_region["A"]["achievement"] == pytest.approx(0.70)
    assert by_region["A"]["status"] == "UNDERPERFORMING"
    assert by_region["B"]["status"] == "ON_TRACK"
    assert [r["region"] for r in m["underperformers"]] == ["A"]


def test_missing_actuals_are_never_estimated():
    m = compute_metrics(ROWS, W2, 0.80)
    c = next(r for r in m["regions"] if r["region"] == "C")
    assert c["actual"] is None
    assert c["achievement"] is None
    assert c["status"] == "DATA_MISSING"
    assert any(g["region"] == "C" for g in m["data_gaps"])


def test_totals_exclude_missing_regions():
    m = compute_metrics(ROWS, W2, 0.80)
    assert m["totals"]["regions_evaluated"] == 2
    assert m["totals"]["actual"] == 165.0
    assert m["totals"]["target"] == 200.0
    assert m["totals"]["achievement"] == pytest.approx(0.825)


def test_week_over_week_growth():
    m = compute_metrics(ROWS, W2, 0.80)
    a = next(r for r in m["regions"] if r["region"] == "A")
    assert a["wow_growth"] == pytest.approx(-0.30)
    b = next(r for r in m["regions"] if r["region"] == "B")
    assert b["wow_growth"] == pytest.approx(0.0556, abs=1e-4)


def test_threshold_is_configurable():
    strict = compute_metrics(ROWS, W2, 0.96)
    assert {r["region"] for r in strict["underperformers"]} == {"A", "B"}
    lenient = compute_metrics(ROWS, W2, 0.50)
    assert lenient["underperformers"] == []


def test_follow_up_escalates_severe_cases():
    m = compute_metrics(ROWS, W2, 0.80, severe_threshold=0.60)
    assert "Escalate" not in m["underperformers"][0]["suggested_follow_up"]  # 70% is not severe
    severe = compute_metrics(ROWS, W2, 0.80, severe_threshold=0.75)
    assert "Escalate" in severe["underperformers"][0]["suggested_follow_up"]


def test_missing_period_raises():
    with pytest.raises(ValueError):
        compute_metrics(ROWS, date(2027, 1, 4), 0.80)
