# Architecture and workflow contract

## Runtime flow

```text
Streamlit UI
   | X-API-Key
   v
FastAPI routes -> WorkflowService (RBAC, lifecycle, approvals)
                         |
                         v
                  LangGraph state graph
                         |
          +--------------+----------------+
          |              |                |
      Research        Reporting       Task/Comms
          |              |                |
          +--------------+----------------+
                         v
                  ToolRegistry
       schema validation / permission / approval
       idempotency / retries / audit events
                         |
             SQLAlchemy + SQLite/PostgreSQL
                         |
             reports / simulated email outbox
```

## Sales workflow stages

1. Interpret request and validate intent/threshold.
2. Retrieve the requested or latest available sales period.
3. Calculate regional actuals, targets, achievement, growth, and data gaps.
4. Identify regions below the configured threshold.
5. Generate PDF, XLSX, CSV, and chart artifacts.
6. Prepare proposed tasks and email drafts without dispatching them.
7. Create approval records and pause in `WAITING_FOR_APPROVAL`.
8. An approver reviews and authorizes the proposed actions.
9. Resume the workflow; only specifically approved actions can pass the registry gate.
10. Persist action results and a final execution summary.

## State and failure semantics

- Each graph node has a persisted workflow step and output checkpoint.
- Completed steps are reused on resume; failed steps can be retried.
- Transient provider failures receive bounded exponential retries.
- A tool call left in `RUNNING` after interruption is treated as uncertain and requires manual verification.
- Missing source data is surfaced as a gap or clarification request; the agent must not invent values, owners, or email addresses.

## Agent contracts

| Agent | Responsibility | Must not do |
|---|---|---|
| Orchestrator | Classify intent, build plan, manage state | Execute arbitrary tools from LLM text |
| Research | Retrieve approved records/documents and cite evidence | Fill missing source values by guessing |
| Reporting | Calculate deterministic metrics and produce artifacts | Change source business records |
| Task management | Propose and manage follow-up tasks | Create approved-only actions without approval |
| Communication | Draft contextual messages | Send without a matching approval |
| Approval layer | Capture explicit human decisions | Treat UI visibility as authorization |

## Evaluation protocol

Use the included tests as regression checks. For a meaningful evaluation, add a held-out set of at least 30 business requests and measure: intent accuracy, plan validity, KPI correctness against hand-calculated fixtures, retrieval precision@k/recall@k, workflow completion rate, unauthorized-action block rate (target 100%), duplicate-action rate (target 0%), and recovery success after injected transient failures. Record model/provider, dataset version, thresholds, and run timestamp with every benchmark.
