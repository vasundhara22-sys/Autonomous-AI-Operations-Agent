"""Static description of the specialised agents, shown in the API and dashboard."""

AGENTS = [
    {
        "id": "orchestrator",
        "name": "Orchestrator Agent",
        "responsibility": "Interprets the request, builds the ordered plan, drives the LangGraph workflow and writes the final summary.",
        "tools": [],
    },
    {
        "id": "research",
        "name": "Research Agent",
        "responsibility": "Retrieves weekly sales records from the approved database and checks data completeness.",
        "tools": ["read_sales_data", "index_document", "extract_document_fields", "search_documents", "retrieve_context", "summarize_document", "get_leave_status"],
    },
    {
        "id": "reporting",
        "name": "Reporting Agent",
        "responsibility": "Calculates KPIs, flags underperforming regions, and produces PDF, Excel, CSV and chart outputs.",
        "tools": ["calculate_sales_metrics", "generate_pdf_report", "generate_excel_report", "generate_csv_report", "generate_chart"],
    },
    {
        "id": "task_management",
        "name": "Task Management Agent",
        "responsibility": "Prepares follow-up tasks, skips duplicates, and creates tasks only after approval.",
        "tools": ["get_tasks", "create_task", "update_task"],
    },
    {
        "id": "communication",
        "name": "Communication Agent",
        "responsibility": "Drafts manager emails from verified figures. Sends only approved drafts through the email provider.",
        "tools": ["get_recipient_details", "save_email_draft", "save_employee_email_draft", "send_email"],
    },
    {
        "id": "approval_layer",
        "name": "Human Approval Layer",
        "responsibility": "Presents proposed actions and records approve or reject decisions by authorised users. Enforced in the service layer. It is not an LLM.",
        "tools": [],
    },
]


def get_agent(agent_id: str) -> dict | None:
    return next((a for a in AGENTS if a["id"] == agent_id), None)
