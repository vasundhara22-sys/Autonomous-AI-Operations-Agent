"""End-to-end demo without the API: plan, retrieve, analyse, prepare, approve, execute, summarise."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.auth import create_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Role  # noqa: E402
from app.seed import seed_reference_data  # noqa: E402
from app.workflow.service import WorkflowService  # noqa: E402

REQUEST = (
    "Prepare a weekly sales performance report, identify underperforming regions, "
    "create follow-up tasks for the regional managers, and draft emails notifying them about their performance."
)


def main() -> None:
    settings = get_settings()
    db.configure(settings.database_url)
    with db.session_factory()() as s:
        seed_reference_data(s)
        analyst_token = create_user(s, "Demo Analyst (run_demo)", Role.ANALYST)
        approver_token = create_user(s, "Demo Approver (run_demo)", Role.APPROVER)

    svc = WorkflowService(db.session_factory(), settings)
    analyst = svc.authenticate(analyst_token)
    approver = svc.authenticate(approver_token)

    wf = svc.start_workflow(analyst, REQUEST)
    print(f"Workflow {wf['id']} -> {wf['status']}")
    for step in wf["steps"]:
        print(f"  [{step['status']:<9}] {step['step_key']:<16} {step['agent']}")
    print("\nPending approvals:")
    for ap in wf["approvals"]:
        print(f"  #{ap['id']} {ap['action_type']:<11} {ap['title']}")

    svc.approve_all(approver, wf["id"])
    wf = svc.resume(approver, wf["id"])
    print(f"\nAfter approval and execution: {wf['status']}")
    summary = wf["summary"]
    print("Executed:", summary["executed_actions"])
    print("Files:", summary["generated_files"])
    print("Unresolved:", summary["unresolved_issues"] or "none")
    print(f"Outputs in: {settings.output_dir}")


if __name__ == "__main__":
    main()
