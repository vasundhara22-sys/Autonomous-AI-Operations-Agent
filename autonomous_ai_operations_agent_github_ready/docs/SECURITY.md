# Security and approval model

## Trust boundaries

1. Natural-language requests, uploaded files, retrieved passages, and LLM responses are untrusted input.
2. The planner produces a validated plan; the LLM does not receive direct database or provider credentials.
3. All sanctioned tool calls pass through `ToolRegistry` for schema validation, permission classification, approval checks, idempotency, retry policy, and audit logging.
4. External actions are separate from preparation. The default email provider is simulated and writes to the local outbox.

## Approval requirements

- `READ_ONLY`: data retrieval and analysis.
- `INTERNAL_WRITE`: writes to the local application database; role checks still apply.
- `EXTERNAL_ACTION`: sending or changing information outside the application; requires a matching approved decision for the workflow and action.
- Approval decisions are associated with a specific workflow/action. The UI is not the security boundary; the tool registry enforces the gate.

## Operational safeguards

- API tokens are randomly generated; only SHA-256 hashes are stored.
- Do not commit `.env`, database files, uploaded business documents, report outputs, or API keys.
- Keep `EMAIL_PROVIDER=simulated` for demonstrations. Configure SMTP only in a controlled test account after reviewing recipients and drafts.
- Idempotency prevents duplicate completed actions. An interrupted external action is moved to `NEEDS_REVIEW` instead of being blindly retried.
- Transient failures are retried with bounded exponential backoff. Validation and other non-transient failures are not retried.

## Before production

This is a portfolio prototype, not a production security certification. Add SSO/OIDC, token rotation and revocation, per-tenant authorization, rate limiting, CSRF protections for browser sessions, encrypted secrets, database migrations, retention policies, malware scanning and isolated parsing for uploads, centralized monitoring, backup/restore drills, and a formal privacy/security review. Use a transactional outbox and provider-side idempotency where supported for external actions.
