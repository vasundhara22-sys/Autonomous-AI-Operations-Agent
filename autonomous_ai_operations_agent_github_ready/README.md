# Autonomous AI Operations Agent for Business Workflow Management

A working prototype that turns a natural-language business request into a planned, multi-agent workflow. It reads sales data, calculates KPIs, produces reports, prepares follow-up tasks and email drafts, and then waits for a human to approve every consequential action before anything runs.

> **Sample data only.** The sales records, managers and email addresses are synthetic. Email delivery is **simulated**: messages are written to `outputs/outbox/` and never leave the machine.

## Quick start

### Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python scripts/seed_demo.py
```

You can also double-click `run_windows.bat` for setup, then use `run_api.bat` and
`run_dashboard.bat` in separate terminals. `run_demo.bat` runs the terminal demo.

### macOS / Linux

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python scripts/seed_demo.py              # creates the DB, sample data, and two demo API keys (printed once)

# Option A: end-to-end demo in the terminal
python scripts/run_demo.py

# Option B: full stack
uvicorn app.api:create_app --factory --port 8000     # Swagger UI at http://localhost:8000/docs
streamlit run dashboard/streamlit_app.py              # paste an API key in the sidebar

python -m pytest -q                                  # automated regression tests
python scripts/verify_environment.py                 # dependency/configuration check
```

Use the analyst key to start workflows and the approver key to approve and execute. The `VIEWER` role can read but cannot change anything.

## What the sample workflow does

Request: *"Prepare a weekly sales performance report, identify underperforming regions, create follow-up tasks for the regional managers, and draft emails notifying them about their performance."*

| Step | Agent | Result |
|---|---|---|
| `understand` | Orchestrator | Intent `sales_performance_followup`, threshold 80%, latest week with data |
| `retrieve_sales` | Research | Weekly records. Missing values stay null. |
| `analyze` | Reporting | Achievement, WoW growth, totals, trend. Missing regions are excluded, never estimated. |
| `identify` | Reporting | South (72%) and Central (73%) fall below threshold |
| `report` | Reporting | PDF with chart, Excel workbook, CSV, PNG |
| `prepare_tasks` | Task Mgmt | Two proposed tasks. Duplicate open tasks are skipped and logged. |
| `draft_emails` | Communication | One draft. Central has no email on file, so it is flagged, not guessed. |
| `request_approval` | Approval layer | Status becomes `WAITING_FOR_APPROVAL` |
| *(after approval)* | Task Mgmt / Communication | `resume` executes only approved actions and records the outcome |

Nothing is created or sent during planning. The planning and execution phases are separated on purpose.

## Controls

- **Approval is enforced in the tool registry, not the UI.** A tool marked `requires_approval` raises `ApprovalRequired` unless an APPROVED decision for that exact workflow and action exists. The LLM has no path around this.
- **Roles.** `ANALYST` starts workflows and manages tasks. `APPROVER` approves, rejects, cancels, and executes. `VIEWER` reads only.
- **Idempotency.** Each external action has a key (`workflow:action:target`). A completed action is never repeated. An action that was interrupted mid-call is marked `NEEDS_REVIEW` and blocked until someone verifies it, because it may already have happened.
- **Retries.** Only transient failures are retried, with exponential backoff, up to `MAX_RETRIES`.
- **Checkpoints.** Each graph node is a recorded step. On resume, succeeded steps are reused and failed steps are re-run.
- **Auth.** API keys are random, shown once, and stored only as SHA-256 hashes. Errors never expose tracebacks, and email addresses are masked in logs.
- **Untrusted input.** Request text is treated as data. LLM output must validate against a Pydantic schema, and any failure falls back to deterministic rules.

## Architecture

```
Streamlit console ──HTTP──► FastAPI (auth, validation, error mapping)
                                   │
                          WorkflowService (roles, approvals, execution, summary)
                                   │
            ┌──────────────────────┴────────────────────────┐
     LangGraph workflow                                Approval gate
  (checkpointed nodes)                          (enforced in ToolRegistry)
            │
     ToolRegistry  ── READ_ONLY / INTERNAL_WRITE / EXTERNAL_ACTION
            │
  SQLAlchemy DB · pandas analytics · report writers · simulated email · DB task store
```

More detailed contracts, failure semantics, evaluation metrics, and security boundaries are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/SECURITY.md`](docs/SECURITY.md).

Layout: `app/analytics.py` (pure KPIs), `app/planner.py` (plan and dependency validation), `app/llm.py` (intent classifier with OpenAI/Gemini adapters and rule fallback), `app/workflow/` (graph and service), `app/tools/` (registry and builtin tools), `app/reporting.py`, `app/api.py`, `dashboard/streamlit_app.py`.

## Configuration

Set in `.env` or the environment. See `.env.example`.

- `UNDERPERFORMANCE_THRESHOLD` (default 0.80). The request can override it, e.g. "below 75%".
- `SEVERE_UNDERPERFORMANCE_THRESHOLD` (0.60). Below this, tasks are High priority and the follow-up is escalated.
- `TASK_DUE_DAYS_AFTER_PERIOD` (7)
- `LLM_PROVIDER`: `deterministic` (default), `openai`, or `gemini`. Keys go in the environment, never in code.

## Document search (Knowledge Base)

Upload policies, manuals, CSVs, PDFs or Word files, then search them or ask questions. Answers are built only from document text and always show their source.

```bash
python scripts/load_sample_docs.py          # index the sample policies in sample_docs/
python scripts/evaluate_retrieval.py 3      # precision@3 and recall@3 on labelled questions
```

What it does:
- **Accepts** TXT, MD, CSV, PDF, DOCX up to 10 MB. Checks the file type and signature, so a renamed file is rejected.
- **Extracts** text page by page for PDFs, row by row for CSVs, and keeps page numbers for citations.
- **Splits** text into overlapping passages of whole sentences, so no answer starts mid-sentence.
- **Searches** with TF-IDF scoring and optional filters by document or file type.
- **Answers** with the best sentences from the matching passages, each numbered and linked to its document and page. If nothing matches, it says so instead of guessing.
- **Summarises** a document by picking its most informative sentences, in their original order.
- **Protects against instructions inside documents.** Text such as "ignore previous instructions" is flagged, shown as content, and never acted on.
- **Duplicates.** Uploading the same file twice reuses the first copy.

Limits to know:
- Search matches **words**, not meaning. "Requirement" will not find a sentence that says "must be at least". A sentence-embedding model would fix this, and the code is ready for one to be added.
- Answers are **extractive**: they quote the document rather than rewriting it. This keeps them from making things up, but they can read less smoothly than a generated answer.
- Scanned PDFs without a text layer are rejected. OCR is not included.
- The evaluation set has 10 questions written by the developer and used during tuning, so the scores are optimistic. Treat them as a regression check, not a benchmark.

## Document processing workflow

Checks uploaded forms and documents, works out what each one is, pulls out the key fields, summarises it, and proposes review tasks for anything missing or wrong. Nothing is created until a person approves it.

How to use it: upload documents on the Knowledge Base page, choose them under **Process documents**, and click **Process selected documents**. Approve the proposed tasks in the Approval Center, then click **Execute approved actions**.

Document types are defined in `app/docproc/schemas.py`. Two are included: supplier invoices and employee onboarding forms. Each lists its required fields and the label text that introduces each one. Add a new type there.

Rules to know:
- Fields are read only from lines shaped like `Label: value`. If the label is missing, the field is reported as missing. Nothing is guessed.
- Values are checked for format. Dates must look like a date, and amounts must look like a number, with an optional currency prefix. A bad value is reported as invalid.
- A document that matches no type is reported as "not recognised" and gets a task to confirm what it is.
- Review tasks have no owner, because documents do not name one. Assign the owner in Task Management after approval.
- Running the same documents again does not create duplicate tasks while the first ones are still open.

Sample forms are in `sample_docs/forms/`. Run `python scripts/load_sample_docs.py` to index them.

## Leave updates by email (real sending)

The **Leave Updates** page prepares one email per employee who has a pending leave request, approved leave starting soon, or a low balance. Emails are sent only after approval.

Setup (done once, in `.env`):
1. Use an email account for sending. For Gmail, turn on 2-Step Verification, then create an App Password at https://myaccount.google.com/apppasswords. Use the 16-character App Password, not your normal password.
2. Set these lines in `.env`:
   ```
   EMAIL_PROVIDER=smtp
   SMTP_USERNAME=sender@gmail.com
   SMTP_PASSWORD=the16characterpassword
   SMTP_FROM=sender@gmail.com
   ```
3. Run `python scripts/send_test_email.py`. It sends one test message from the sender account to itself.

Sending to a test address: each employee's address is in the employee record. To test with your own inbox, set an employee's address (for example Priya Nair) to yours with a one-line update, then run the leave updates again.

Rules:
- Nothing is sent until someone approves it in the Approval Center.
- Passwords are read only from `.env` and are never written to logs, messages, or the database.
- `EMAIL_PROVIDER=simulated` (the default) keeps saving emails to `outputs/outbox` instead of sending them. Tests always use simulated mode.

## Project status and honest limitations

This is a substantial working prototype with sales, document-processing, knowledge-base,
and leave-update workflows; role-based access; approval gates; idempotent tool execution;
retries; checkpointed graph steps; API; dashboard; reports; and automated regression tests.
The default configuration is deliberately local and safe for demonstrations.

Not yet built:
- **Semantic (embedding) search** using a sentence-embedding model and FAISS or pgvector.
- **Employee task coordination workflow** (overdue tasks, workload, reassignment). The classifier recognises these requests and returns a clear "not implemented" message.
- **Alembic migrations.** The schema is created with `create_all` for now.
- **Live Gemini and OpenAI calls.** The adapters are written and schema-validated, but were not exercised against live APIs in this environment. The fallback path is tested.
- **Real email and task integrations.** Email is simulated. Tasks use a local database behind a provider class, so Jira, Trello or Asana adapters can drop in.
- **Docker Compose and a dedicated Multi-Agent Monitor page** with per-agent timing and I/O views. The execution planner already exposes step status and duration through workflow details.
- **Authentication hardening.** API keys are sufficient for a prototype. Use SSO/OIDC, token rotation, rate limiting, tenant isolation, and centralized secret management for production.


## Portfolio demonstration checklist

1. Run the sample sales request from the dashboard or `python scripts/run_demo.py`.
2. Show the generated ordered plan and the Research, Reporting, Task Management, and Communication steps.
3. Show the report files and the exact sales period/threshold used.
4. Open the Approval Center as an approver; approve or reject the proposed actions.
5. Resume the workflow and show the created internal tasks and simulated email outbox.
6. Show execution logs, idempotency records, and the final workflow summary.
7. Demonstrate a knowledge-base answer with source citations and a document-processing workflow.

**Important:** The project is a prototype, not a production deployment. The sample data is synthetic.
Email defaults to simulation. Live SMTP is optional and should only be enabled after explicit review.
The current knowledge search is lexical TF-IDF, not embedding-based semantic retrieval.
