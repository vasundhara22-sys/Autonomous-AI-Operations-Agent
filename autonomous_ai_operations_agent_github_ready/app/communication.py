"""Email composition from verified figures only. Every number comes from the metrics dict."""

from __future__ import annotations


def compose_sales_follow_up_email(row: dict, metrics: dict, task: dict | None) -> tuple[str, str]:
    region = row["region"]
    manager = row.get("manager") or "Regional Manager"
    achievement = row["achievement"]
    wow = row.get("wow_growth")
    wow_line = (
        f"Week-over-week change: {wow:+.1%}"
        if wow is not None
        else "Week-over-week change: not available (prior week data missing)"
    )

    subject = f"{region} region: sales at {achievement:.1%} of target, week of {metrics['period_week']}"
    if task:
        task_lines = [
            "Follow-up task:",
            f"  {task['title']}",
            f"  Due: {task['due_date']} | Priority: {task['priority']}",
        ]
    else:
        task_lines = ["Follow-up task: none proposed (see note from Sales Operations)."]

    body = "\n".join(
        [
            f"Hello {manager},",
            "",
            f"This note covers {region} sales for the week of {metrics['period_week']} to {metrics['period_end']}.",
            "",
            f"Actual sales: {row['actual']:,.0f}",
            f"Target: {row['target']:,.0f}",
            f"Achievement: {achievement:.1%} (threshold {metrics['threshold']:.0%})",
            f"Gap to target: {row['gap_to_target']:,.0f}",
            wow_line,
            "",
            "Recommended action:",
            f"  {row['suggested_follow_up']}",
            "",
            *task_lines,
            "",
            "Regards,",
            "Sales Operations",
            "",
            "[DRAFT: requires manager approval before sending]",
        ]
    )
    return subject, body
