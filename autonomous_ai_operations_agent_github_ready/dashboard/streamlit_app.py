"""Streamlit operations console. Talks to the FastAPI backend over HTTP, so every action is authorised server-side."""

from __future__ import annotations

import os
from collections import Counter
from datetime import date

import httpx
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Ops Agent Console", page_icon="🧭", layout="wide")

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.4rem;}
    div[data-testid="stMetric"] {background:#F4F7FB;border:1px solid #DDE5F0;border-radius:12px;padding:12px 14px;}
    h1, h2, h3 {color:#1F3A5F;}
    </style>
    """,
    unsafe_allow_html=True,
)

SAMPLE_REQUEST = (
    "Prepare a weekly sales performance report, identify underperforming regions, "
    "create follow-up tasks for the regional managers, and draft emails notifying them about their performance."
)


def call(method: str, path: str, *, params=None, body=None, binary: bool = False, files=None):
    base = st.session_state.get("api_url", "http://localhost:8000").rstrip("/")
    token = st.session_state.get("token", "")
    if not token:
        st.info("Enter your API key in the sidebar to continue.")
        st.stop()
    try:
        response = httpx.request(
            method, f"{base}{path}", params=params, json=body,
            headers={"X-API-Key": token}, timeout=120, files=files,
        )
    except httpx.HTTPError as exc:
        st.error(f"Cannot reach the API at {base} ({exc.__class__.__name__}). Is it running?")
        st.stop()
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        st.error(f"{response.status_code}: {detail}")
        return None
    if binary:
        return response.content
    return response.json()


def status_badge(status: str) -> str:
    colors = {
        "COMPLETED": "#1E8449", "PARTIALLY_COMPLETED": "#B9770E", "WAITING_FOR_APPROVAL": "#1F6F8B",
        "FAILED": "#B03A2E", "CANCELLED": "#7F8C8D", "NEEDS_CLARIFICATION": "#8E44AD",
    }
    return f":{'green' if status == 'COMPLETED' else 'orange' if status == 'PARTIALLY_COMPLETED' else 'blue' if status == 'WAITING_FOR_APPROVAL' else 'red' if status == 'FAILED' else 'gray'}[{status}]"


# ----- pages ------------------------------------------------------------------------

def page_overview() -> None:
    st.header("Overview")
    workflows = call("GET", "/api/workflows", params={"limit": 200}) or []
    approvals = call("GET", "/api/approvals", params={"status": "PENDING"}) or []
    tasks = call("GET", "/api/tasks") or []
    today = date.today().isoformat()
    overdue = [t for t in tasks if t.get("due_date") and t["due_date"] < today and t["status"] in ("OPEN", "IN_PROGRESS")]

    def count(*statuses: str) -> int:
        return sum(1 for w in workflows if w["status"] in statuses)

    cols = st.columns(6)
    cols[0].metric("Workflows", len(workflows))
    cols[1].metric("Completed", count("COMPLETED", "PARTIALLY_COMPLETED"))
    cols[2].metric("In progress", count("WAITING_FOR_APPROVAL", "IN_PROGRESS", "PLANNING"))
    cols[3].metric("Failed", count("FAILED"))
    cols[4].metric("Pending approvals", len(approvals))
    cols[5].metric("Overdue tasks", len(overdue))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Workflow status")
        if workflows:
            counts = Counter(w["status"] for w in workflows)
            st.bar_chart(pd.Series(counts, name="workflows"))
        else:
            st.info("No workflows yet. Start one from AI Operations.")
    with right:
        st.subheader("Recent activity")
        logs = call("GET", "/api/logs", params={"limit": 12}) or []
        if logs:
            st.dataframe(pd.DataFrame(logs)[["timestamp", "agent", "level", "message"]], hide_index=True, use_container_width=True)
        else:
            st.caption("No activity yet.")


def page_ai_operations() -> None:
    st.header("AI Operations")
    st.caption("Describe a business request. The agents plan, read data, and prepare actions. Nothing is created or sent without approval.")
    request = st.text_area("Business request", value=SAMPLE_REQUEST, height=110)
    if st.button("Run workflow", type="primary"):
        with st.spinner("Planning and running read-only steps..."):
            result = call("POST", "/api/workflows", body={"request": request})
        if result:
            st.session_state["last_workflow"] = result["id"]

    wid = st.session_state.get("last_workflow")
    if not wid:
        return
    wf = call("GET", f"/api/workflows/{wid}")
    if not wf:
        return
    st.markdown(f"**Workflow** `{wf['id']}`  ·  status {status_badge(wf['status'])}")
    if wf.get("clarification"):
        st.warning(wf["clarification"])
    if wf.get("error"):
        st.error(wf["error"])
    if wf["steps"]:
        st.dataframe(
            pd.DataFrame(wf["steps"])[["step_key", "agent", "status", "attempts", "duration_ms"]],
            hide_index=True, use_container_width=True,
        )
    if wf.get("approvals"):
        st.info(f"{sum(a['status'] == 'PENDING' for a in wf['approvals'])} action(s) are waiting in the Approval Center.")


def page_planner() -> None:
    st.header("Execution Planner")
    workflows = call("GET", "/api/workflows", params={"limit": 100}) or []
    if not workflows:
        st.info("No workflows yet.")
        return
    options = {f"{w['created_at'][:16]} · {w['request'][:70]}": w["id"] for w in workflows}
    choice = st.selectbox("Workflow", list(options))
    wf = call("GET", f"/api/workflows/{options[choice]}")
    if not wf:
        return
    st.markdown(f"**Request:** {wf['request']}")
    st.markdown(f"**Status:** {status_badge(wf['status'])}  ·  **Intent:** `{wf.get('intent') or 'n/a'}`")
    plan = wf.get("plan") or {}
    if plan:
        status_by_key = {s["step_key"]: s for s in wf["steps"]}
        rows = []
        for step in plan.get("steps", []):
            live = status_by_key.get(step["key"], {})
            rows.append({
                "step": step["key"], "agent": step["agent"], "phase": step.get("phase"),
                "depends on": ", ".join(step.get("depends_on", [])) or "-",
                "needs approval": "yes" if step.get("requires_approval") else "no",
                "status": live.get("status", "PLANNED"), "title": step["title"],
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.subheader("Assumptions")
        for item in plan.get("assumptions", []):
            st.write(f"- {item}")
    if wf["steps"]:
        st.subheader("Step output")
        for step in wf["steps"]:
            if step.get("error"):
                st.error(f"{step['step_key']}: {step['error']}")
    with st.expander("Agent roster"):
        for agent in call("GET", "/api/agents") or []:
            st.write(f"**{agent['name']}**: {agent['responsibility']}")


def page_approvals() -> None:
    st.header("Approval Center")
    st.caption("Review each action. Edits are applied before approval. Only approved actions run.")
    pending = call("GET", "/api/approvals", params={"status": "PENDING"}) or []
    workflow_ids = sorted({a["workflow_id"] for a in pending})
    if not workflow_ids:
        st.success("No pending approvals.")
    else:
        wid = st.selectbox("Workflow", workflow_ids)
        col1, col2 = st.columns(2)
        if col1.button("Approve all pending for this workflow"):
            if call("POST", f"/api/workflows/{wid}/approve-all") is not None:
                st.rerun()
        if col2.button("Execute approved actions"):
            result = call("POST", f"/api/workflows/{wid}/resume")
            if result:
                st.success(f"Workflow is now {result['status']}")

        for ap in [a for a in pending if a["workflow_id"] == wid]:
            with st.container(border=True):
                st.subheader(ap["title"])
                st.caption(f"{ap['action_type']} · {ap['target_key']} · approval #{ap['id']}")
                edits: dict = {}
                if ap["action_type"] == "send_email" and ap.get("draft"):
                    draft = ap["draft"]
                    st.write(f"**To:** {draft['to_address']}")
                    subject = st.text_input("Subject", draft["subject"], key=f"subj_{ap['id']}")
                    body = st.text_area("Body", draft["body"], height=220, key=f"body_{ap['id']}")
                    if subject != draft["subject"]:
                        edits["subject"] = subject
                    if body != draft["body"]:
                        edits["body"] = body
                else:
                    payload = ap["payload"]
                    st.write(f"**Assignee ID:** {payload.get('assignee_id') or 'unassigned'}  ·  **Due:** {payload.get('due_date')}")
                    st.write(payload.get("description", ""))
                    levels = ["High", "Medium", "Low"]
                    priority = st.selectbox("Priority", levels, index=levels.index(payload.get("priority", "Medium")), key=f"prio_{ap['id']}")
                    if priority != payload.get("priority"):
                        edits["priority"] = priority
                note = st.text_input("Note (optional)", key=f"note_{ap['id']}")
                b1, b2 = st.columns(2)
                if b1.button("Approve", key=f"approve_{ap['id']}", type="primary"):
                    if call("POST", f"/api/approvals/{ap['id']}/approve", body={"note": note or None, "edits": edits or None}) is not None:
                        st.rerun()
                if b2.button("Reject", key=f"reject_{ap['id']}"):
                    if call("POST", f"/api/approvals/{ap['id']}/reject", body={"note": note or None}) is not None:
                        st.rerun()

    st.subheader("Recent decisions")
    decided = call("GET", "/api/approvals") or []
    decided = [a for a in decided if a["status"] != "PENDING"][:20]
    if decided:
        st.dataframe(pd.DataFrame(decided)[["id", "title", "status", "decided_at", "execution_error"]], hide_index=True, use_container_width=True)


def page_reports() -> None:
    st.header("Reports & Analytics")
    threshold = st.slider("Underperformance threshold (% of target)", 50, 120, 80, step=1) / 100
    metrics = call("GET", "/api/sales/metrics", params={"threshold": threshold})
    if metrics:
        totals = metrics["totals"]
        cols = st.columns(4)
        cols[0].metric("Period", metrics["period_week"])
        cols[1].metric("Overall achievement", f"{totals['achievement']:.1%}" if totals["achievement"] else "n/a")
        cols[2].metric("Underperforming", len(metrics["underperformers"]))
        cols[3].metric("Data gaps", len(metrics["data_gaps"]))
        df = pd.DataFrame(metrics["regions"])
        st.subheader("Regional achievement")
        chart = df.set_index("region")["achievement"].fillna(0) * 100
        st.bar_chart(chart.rename("achievement %"))
        st.dataframe(
            df[["region", "manager", "actual", "target", "achievement", "wow_growth", "status"]],
            hide_index=True, use_container_width=True,
        )
        st.subheader("Trend (overall achievement)")
        trend = pd.DataFrame(metrics["trend"])
        if not trend.empty:
            st.line_chart(trend.set_index("week_start")["achievement"])
        for item in metrics["observations"]:
            st.write(f"- {item}")

    st.subheader("Downloadable reports")
    if st.button("Generate PDF, Excel and CSV for the latest week"):
        if call("POST", "/api/reports/generate", body={"threshold": threshold}) is not None:
            st.success("Reports generated.")
    for report in call("GET", "/api/reports") or []:
        data = call("GET", f"/api/reports/{report['id']}/download", binary=True)
        if data:
            st.download_button(f"{report['title']} · {report['file_name']}", data, file_name=report["file_name"], key=f"dl_{report['id']}")


def page_tasks() -> None:
    st.header("Task Management")
    status_filter = st.selectbox("Status", ["All", "OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"])
    tasks = call("GET", "/api/tasks", params=None if status_filter == "All" else {"status": status_filter}) or []
    if tasks:
        st.dataframe(pd.DataFrame(tasks)[["id", "title", "priority", "status", "due_date", "assignee_id", "region_id"]], hide_index=True, use_container_width=True)
    else:
        st.info("No tasks match this filter.")

    with st.expander("Create a task"):
        with st.form("create_task"):
            title = st.text_input("Title")
            description = st.text_area("Description")
            priority = st.selectbox("Priority", ["High", "Medium", "Low"], index=1)
            due = st.date_input("Due date", value=None)
            if st.form_submit_button("Create"):
                body = {"title": title, "description": description, "priority": priority, "due_date": due.isoformat() if due else None}
                if call("POST", "/api/tasks", body=body) is not None:
                    st.rerun()

    with st.expander("Update a task status"):
        task_id = st.number_input("Task ID", min_value=1, step=1)
        new_status = st.selectbox("New status", ["OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"])
        if st.button("Update"):
            if call("PUT", f"/api/tasks/{int(task_id)}", body={"status": new_status}) is not None:
                st.rerun()


def page_logs() -> None:
    st.header("Execution Logs")
    workflows = call("GET", "/api/workflows", params={"limit": 100}) or []
    options = {"All workflows": None, **{w["id"][:8] + " · " + w["request"][:50]: w["id"] for w in workflows}}
    choice = st.selectbox("Filter", list(options))
    logs = call("GET", "/api/logs", params={"workflow_id": options[choice], "limit": 300} if options[choice] else {"limit": 300}) or []
    if logs:
        df = pd.DataFrame(logs)
        level = st.multiselect("Level", sorted(df["level"].unique()), default=sorted(df["level"].unique()))
        st.dataframe(df[df["level"].isin(level)][["timestamp", "workflow_id", "agent", "tool", "level", "message"]], hide_index=True, use_container_width=True)
    else:
        st.info("No log entries.")


def page_knowledge() -> None:
    st.header("Knowledge Base")
    st.caption("Search and questions use only the text of uploaded documents. Every answer shows its source.")

    with st.expander("Upload a document (TXT, MD, CSV, PDF, DOCX, up to 10 MB)"):
        uploaded = st.file_uploader("File", type=["txt", "md", "csv", "pdf", "docx"], key="kb_upload")
        if uploaded is not None and st.button("Index document", type="primary"):
            with st.spinner("Extracting and indexing..."):
                result = call("POST", "/api/documents/upload", files={
                    "file": (uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream")
                })
            if result:
                if result.get("duplicate"):
                    st.info("This file was uploaded before. Showing the existing copy.")
                if result["status"] == "INDEXED":
                    st.success(f"Indexed {result['chunks']} passages from {result['pages']} page(s).")
                else:
                    st.error(f"Indexing failed: {result.get('error')}")

    docs = call("GET", "/api/documents") or []
    if docs:
        st.dataframe(
            pd.DataFrame(docs)[["id", "name", "extension", "status", "pages", "chunks", "uploaded_at"]],
            hide_index=True, use_container_width=True,
        )
    else:
        st.info("No documents yet. Upload one above, or run scripts/load_sample_docs.py to load the samples.")
    indexed = {d["name"]: d["id"] for d in docs if d["status"] == "INDEXED"}

    st.subheader("Search")
    query = st.text_input("Search for a topic", placeholder="e.g. receipts for expenses", key="kb_query")
    top_k = st.slider("Results", 1, 10, 5, key="kb_topk")
    if st.button("Search") and query:
        st.session_state["kb_hits"] = call("POST", "/api/knowledge/search", body={"query": query, "top_k": top_k})
    hits = st.session_state.get("kb_hits")
    if hits:
        if not hits["results"]:
            st.info("No matching passages.")
        for hit in hits["results"]:
            with st.container(border=True):
                st.markdown(f"**{hit['document']}**, page {hit['page'] or 'n/a'} · score {hit['score']} · `{hit['chunk_id']}`")
                st.write(hit["excerpt"])
                if hit.get("warning"):
                    st.warning(hit["warning"])

    st.subheader("Ask a question")
    question = st.text_input("Question", placeholder="How many days of annual leave do employees get?", key="kb_question")
    if st.button("Get answer") and question:
        st.session_state["kb_answer"] = call("POST", "/api/knowledge/answer", body={"question": question})
    answer = st.session_state.get("kb_answer")
    if answer:
        if answer["grounded"]:
            st.success(answer["answer"])
        else:
            st.info(answer["answer"])
        for citation in answer["citations"]:
            st.markdown(f"[{citation['ref']}] **{citation['document']}**, page {citation['page'] or 'n/a'}: {citation['quote']}")
        for warning in answer["warnings"]:
            st.warning(warning)
        st.caption(answer["source_note"])

    st.subheader("Summary")
    if indexed:
        choice = st.selectbox("Document", list(indexed), key="kb_summary_doc")
        if st.button("Summarize"):
            summary = call("GET", f"/api/documents/{indexed[choice]}/summary", params={"max_sentences": 5})
            if summary:
                if summary.get("warning"):
                    st.warning(summary["warning"])
                for item in summary["sentences"]:
                    st.write(f"- {item['text']} *(page {item['page'] or 'n/a'})*")
    else:
        st.caption("Index a document to enable summaries.")
    document_processing_section(docs)


def document_processing_section(docs: list[dict]) -> None:
    st.subheader("Process documents")
    st.caption("Finds the document type, pulls out key fields, summarises each one, and proposes review tasks for gaps. Tasks wait for approval.")
    indexed = {d["name"]: d["id"] for d in docs if d["status"] == "INDEXED"}
    if not indexed:
        st.info("Index a document first.")
        return
    chosen = st.multiselect("Documents", list(indexed), key="dp_docs")
    request = st.text_input("Instruction", value="Extract key fields, summarise, and create review tasks for missing fields.", key="dp_request")
    if st.button("Process selected documents", type="primary") and chosen:
        result = call("POST", "/api/document-workflows", body={"request": request, "document_ids": [indexed[n] for n in chosen]})
        if result:
            st.session_state["dp_result"] = result
    result = st.session_state.get("dp_result")
    if not result:
        return
    st.markdown(f"**Status:** {status_badge(result['status'])}")
    if result.get("clarification"):
        st.warning(result["clarification"])
    extract_step = next((s for s in result["steps"] if s["step_key"] == "doc_extract"), None)
    for rec in ((extract_step or {}).get("output") or {}).get("extractions", []):
        with st.container(border=True):
            st.subheader(rec["name"])
            st.write(f"Type: {rec['doc_type_label'] or 'not recognised'}")
            if rec["fields"]:
                rows = [{"field": f["label"], "value": f["value"] or "", "status": f["status"]} for f in rec["fields"].values()]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            for line in rec["summary"]:
                st.write(f"- {line}")
    pending = [a for a in result.get("approvals", []) if a["status"] == "PENDING"]
    if pending:
        st.info(f"{len(pending)} review task(s) are waiting in the Approval Center.")
    for issue in (result.get("summary") or {}).get("unresolved_issues", []):
        st.write(f"- {issue}")


def page_leave_updates() -> None:
    st.header("Leave Updates")
    st.caption("Prepares one update per employee who has a pending request, approved leave starting soon, or a low balance. "
               "Emails are sent only after you approve them in the Approval Center.")
    if st.button("Prepare leave updates", type="primary"):
        result = call("POST", "/api/leave-workflows", body={"request": "Send leave related updates to employees"})
        if result:
            st.session_state["leave_result"] = result
    result = st.session_state.get("leave_result")
    if not result:
        return
    st.markdown(f"**Workflow** `{result['id']}`  ·  status {status_badge(result['status'])}")
    pending = [a for a in result.get("approvals", []) if a["status"] == "PENDING"]
    if pending:
        st.info(f"{len(pending)} leave update(s) are waiting in the Approval Center.")
    else:
        st.success("No leave updates are waiting for approval.")
    for issue in (result.get("summary") or {}).get("unresolved_issues", []):
        st.write(f"- {issue}")


# ----- shell ------------------------------------------------------------------------

PAGES = {
    "Overview": page_overview,
    "AI Operations": page_ai_operations,
    "Execution Planner": page_planner,
    "Approval Center": page_approvals,
    "Reports & Analytics": page_reports,
    "Task Management": page_tasks,
    "Knowledge Base": page_knowledge,
    "Leave Updates": page_leave_updates,
    "Execution Logs": page_logs,
}

with st.sidebar:
    st.title("🧭 Ops Agent")
    st.session_state["api_url"] = st.text_input("API URL", os.getenv("OPS_API_URL", "http://localhost:8000"))
    st.session_state["token"] = st.text_input("API key", type="password", value=os.getenv("OPS_API_KEY", ""))
    choice = st.radio("Navigate", list(PAGES))
    st.caption("Sample data only. Email delivery is simulated.")

PAGES[choice]()
