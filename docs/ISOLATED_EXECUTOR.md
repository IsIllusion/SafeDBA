# Isolated lock executor (experimental)

This opt-in path separates the Agent's model/observer credentials from a
privileged worker process. It supports **persisted lock IncidentWorkflows
only**, not remote SQL, index creation, statistics changes or query rewrites.
The default `combined` profile remains a local development mode.

[Project overview](../README.md) · [Change history](../CHANGELOG.md)

## Trust boundary

| Component | Credentials / files | Authority |
|---|---|---|
| Agent, `SAFEDBA_PROCESS_ROLE=agent` | Model key, observer DB password, API token, shared workflow DB | Observe and submit exact action references |
| Worker, `SAFEDBA_PROCESS_ROLE=executor` | Observer and termination DB passwords; no model/maintenance key | Revalidate and perform an operator-granted lock action |
| Operator | Worker-side CLI and private grant DB | Review targets and grant a bounded batch for 1–300 seconds |

Both isolated profiles ignore project `.env` automatically. Set the process
role in the **launch environment**, not inside `.env`. The Agent refuses to
start if privileged DB passwords or ambient libpq credential/service settings
are inherited, and refuses local privileged connection contexts. The worker
requires an explicit termination password and refuses model API keys and
maintenance/ambient credentials. Neither isolated process can open the
maintenance connection. Fixture/example passwords are for disposable tests;
replace them with independently managed credentials in any real deployment.

These checks prevent accidental credential co-loading; **they are not an OS
sandbox**. Deploy under different restricted OS service identities. The Agent
must not be able to read the worker environment, grant DB (including SQLite
sidecars), audit files, or a development `.env` containing privileged secrets.
Neither identity should be able to modify deployed code, its interpreter,
dependencies, or trusted policy files. Restrict process inspection, debugging,
and privileged-role database access at the OS/network level as appropriate.

The workflow SQLite DB is shared and writable for coordination. It is not an
authority for new privileged targets: the separate operator-owned grant DB
pins incident/revision/action, proposal, blocker identity, waiter identities,
and the observed database endpoint/OID/server-start identity. A database
restart invalidates outstanding grants. Runtime roles must share one fixed
host/port/database endpoint; this initial implementation is not failover-aware.

The HTTP endpoint binds only to `127.0.0.1` and requires a random token of at
least 32 characters. The token authorizes submission, **not new grants**.
Requests cannot supply SQL, DSNs, state paths, approval objects, or execution
credentials. Payloads/results are bounded to 32 KiB; duplicate fields,
non-finite JSON, unsupported fields, and malformed identifiers are rejected.
Connections have timeouts; the worker executes requests serially. This is a
same-host prototype, not a public or multi-tenant HTTP service. Do not publish
the port or use a reverse proxy to expose it remotely.

## Launch configuration

Prepare two separate service environments using the deployment's secret
manager or protected service configuration. Do not place both environments
in one file readable by the Agent. Values below are variable names, not real
secrets, and this procedure does not create OS users or install services.

Common non-secret configuration:

- `SAFEDBA_ENV=production`
- `SAFEDBA_DB_HOST`, `SAFEDBA_DB_PORT`, `SAFEDBA_DB_NAME`, `SAFEDBA_DB_USER`
- matching `SAFEDBA_EXECUTOR_DB_*` / `SAFEDBA_TERMINATOR_DB_*` endpoint/user metadata
- `SAFEDBA_INCIDENT_STATE_DB_PATH`: absolute shared workflow DB path
- `SAFEDBA_RUNTIME_CONTROLS_PATH`: protected deny-only controls file
- `SAFEDBA_RUNTIME_CONTROLS_REQUIRED=true`
- `SAFEDBA_ENABLE_MUTATIONS=true` and `SAFEDBA_ENABLE_TERMINATE_BACKEND=true`

Agent-specific environment:

- `SAFEDBA_PROCESS_ROLE=agent`
- `SAFEDBA_DB_PASSWORD` and the configured primary `SAFEDBA_LLM_*` settings
- `SAFEDBA_EXECUTOR_URL=http://127.0.0.1:18761`
- `SAFEDBA_EXECUTOR_API_TOKEN`: random shared submission token
- its own `SAFEDBA_AUDIT_LOG_PATH`, memory and experience paths
- **no** privileged DB passwords, `PGPASSWORD`, `PGPASSFILE`, or service settings

Worker-specific environment:

- `SAFEDBA_PROCESS_ROLE=executor`
- `SAFEDBA_DB_PASSWORD`, `SAFEDBA_TERMINATOR_DB_PASSWORD`
- `SAFEDBA_EXECUTOR_API_TOKEN`: the same submission token
- `SAFEDBA_EXECUTION_GRANT_DB_PATH`: absolute worker-private SQLite path,
  different from the workflow DB, in an operator-owned directory
- `SAFEDBA_AUDIT_LOG_PATH`: a worker-owned audit log separate from Agent logs
- **no** primary/fallback model keys, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`,
  `SAFEDBA_EXECUTOR_DB_PASSWORD`, or ambient libpq credentials/service settings

Under the worker identity, with its environment already populated:

```powershell
python -B src/executor_worker.py serve --port 18761
```

Create a lock incident using the existing Agent CLI/workflow. Before resuming
it, an operator uses the worker environment to inspect the pending targets:

```powershell
python -B src/executor_worker.py approve --incident-id <incident-uuid> --actor <operator-name>
```

That command prints the targets and exits without issuing a grant. After
reviewing the full scope (including waiter identities and database identity),
repeat with `--confirm --review-digest <shown-digest> --ttl 120`. A changed
scope or missing/mismatched digest refuses issuance and requires a new preview.
A grant batch supports at most ten pending lock actions. The operator identity
is an audit label, not
an authenticated RBAC principal; protect CLI access with OS identities.

Then resume the incident through the normal workflow and approve its exact
scope. **There are two approval boundaries**: operator-side batch grants and
the existing workflow confirmation. One batch grant plus one workflow
confirmation can cover three blockers; this does not ask once per blocker.
Any target/proposal/waiter changes require new operator review. Grants are
deliberately stricter than the workflow's representative-waiter selection;
switching the representative can invalidate an otherwise similar grant.

## Failure and recovery rules

- The worker reads its configured workflow DB, not a caller-chosen file. It
  consumes an exact-scope private grant atomically before invoking execution.
- Existing runtime policy, live stop controls, audit integrity, approval TTL,
  lease, workflow deadline, fresh evidence and final PostgreSQL PID/transaction
  identity checks still apply. The grant scope and expiry are rechecked under
  the workflow write lock before transitioning to `APPLYING`.
- No grant, expired grant or mismatched intent: no SQL action; return to fresh
  approval. A grant consumed by a later failed validation cannot be reissued
  automatically, even if the workflow asks for reapproval. Review both records.
- Timeout, malformed result, wrong operation ID, or worker exit after action:
  **no automatic HTTP retry**. The workflow retains `IN_DOUBT` /
  `REVIEW_REQUIRED`; do not mark it successful just because the lock is gone.
- For manual reconciliation, compare the action/operation ID in shared
  workflow state, private grant `state` / `result_json`, worker audit records,
  and fresh database observations. Do not edit SQLite states to force a retry
  or approve a new incident until the original outcome is understood.
- Worker grant storage and external audit integrity are part of the trusted
  deployment. Consumed grants are single-use and survive process restarts.
  They are not automatically deleted or reset on service startup.

## Verification and remaining work

The portable tests cover protocol/auth rejection, profile credential checks,
expiry/target tampering, concurrent one-time claims, and no client retries.
The disposable PostgreSQL suite launches actual Agent and worker subprocesses
and verifies three-lock completion, missing/tampered grant refusal, and worker
exit after a real database effect. The Agent subprocess has no privileged
database password; these tests run under one test OS account and **do not
certify deployment ACLs or protection against a compromised OS process**.

Final local verification on 2026-09-05: 268 portable tests passed; all 18
PostgreSQL integration tests passed with zero skips in run
`897121ea-8045-4696-897e-04d9ea50e4a0`. The disposable server was stopped and
its cluster removed. The report is retained under
`logs/integration-runs/897121ea-8045-4696-897e-04d9ea50e4a0/report.json`.
These are local test results. The full runtime reports and databases are
intentionally not committed; repository readers can reproduce the tests with
the disposable runner described in the project README.

Still required for production: separate-account/container deployment tests,
OS hardening, authenticated operator/RBAC approval, independently anchored
audit, revocation/result-reconciliation UX, availability/load testing,
backup/recovery drills and security review. Cross-host transport and remote
DDL workflows are intentionally not implemented.
