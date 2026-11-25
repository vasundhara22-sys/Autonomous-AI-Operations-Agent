"""Leave update wording. Every date and number comes from the leave records."""

from __future__ import annotations


def _describe(item: dict) -> str:
    return f"{item['type']}: {item['start']} to {item['end']} ({item['days']:g} days)"


def compose_leave_update(row: dict, as_of: str) -> tuple[str, str]:
    name = row["name"]
    lines = [f"Hello {name},", "", f"Here is your leave update as of {as_of}.", ""]
    if row["pending"]:
        lines.append("Leave requests waiting for a decision:")
        lines += [f"  - {_describe(item)}" for item in row["pending"]]
        lines.append("")
    if row["upcoming"]:
        lines.append("Approved leave starting soon:")
        lines += [f"  - {_describe(item)}" for item in row["upcoming"]]
        lines.append("")
    if row["remaining_days"] is not None:
        lines.append(f"Annual leave remaining: {row['remaining_days']:g} days.")
        if row["low_balance"]:
            lines.append("Your balance is low. Please plan ahead and speak to HR if you need more.")
        lines.append("")
    lines += ["Regards,", "HR and Sales Operations", "", "This message was generated from sample data."]

    topics = []
    if row["pending"]:
        topics.append("request awaiting decision")
    if row["upcoming"]:
        topics.append("upcoming leave")
    if row["low_balance"]:
        topics.append("low leave balance")
    subject = f"Leave update for {name}: {', '.join(topics) or 'summary'}"
    return subject[:300], "\n".join(lines)
