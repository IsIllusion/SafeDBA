# Operations Guide

Database access, execution policy, incident recovery and observability for SafeDBA.

[Project overview](../README.md) · [Independent executor](ISOLATED_EXECUTOR.md)

## Database provisioning

The included Docker Compose stack runs PostgreSQL 16 on `127.0.0.1:15432`.
It provisions separate observer, maintenance and termination roles through
`sql/init.sql`. Its fixed credentials and seeded data are for local development;
provision separate credentials and least-privilege roles for other deployments.

Initialization scripts run only when the PostgreSQL volume is first created.
After upgrading role definitions, migrate an existing database explicitly; do
not delete a volume containing data you need to retain.

## Safety Model

SafeDBA uses several independent boundaries rather than relying on prompt
instructions alone.

### Evidence discipline

- factual database claims must cite tool evidence references such as
  `[ev-0001]`
- proposal prerequisites must come from a previous model turn
- query, table, column, index, and lock identities must match the evidence
- volatile observations expire and must be refreshed
- duplicate calls, tool-call budgets, output limits, and wall-clock deadlines
  bound the Agent loop
- incomplete, malformed, or failed Agent runs cannot reach controlled
  execution

### Query and database boundaries

- dynamic SQL is restricted to a conservative single-statement `SELECT`
  subset
- locking clauses, `SELECT INTO`, dangerous functions, malformed quoting, and
  multiple statements are rejected
- dynamic plans use PostgreSQL's extended protocol
- unfamiliar queries receive an estimated plan before bounded runtime analysis
- observer transactions are read-only and use statement, lock, and idle
  transaction timeouts
- estimated-cost and output-size gates limit expensive observations

### Separated runtime identities

The database initialization creates distinct PostgreSQL identities for:

- observation
- maintenance execution
- backend termination
- initial database bootstrap

SafeDBA attests the connected roles at runtime and fails before model use if
the identities are shared, privileged beyond policy, or incorrectly
configured.

### Approval and execution

Medium- and high-risk actions require explicit approval. Backend termination
is bound to the blocker PID, backend start, transaction start, original waiter
identities, database, backend type, and current lock relationship. These facts
are checked again at execution time.

The executor reports deterministic outcomes. An optional model review may
explain the result, but it cannot turn a rejected or inconclusive action into a
success.

### Audit and local state

Controlled actions are written to `logs/audit.jsonl`. Query text, credentials,
tokens, and sensitive statistics are redacted or fingerprinted by default.
Every new record carries a sequence number, previous-record digest, and its
own digest. SafeDBA validates the complete retained chain before appending and
fails closed if a chained record was modified, reordered, inserted, or removed
from the middle of the retained history.

```powershell
python src/main.py --verify-audit
```

Existing unchained logs are preserved and their final legacy record becomes a
SHA-256 migration anchor. This detects later changes to the chained portion;
it does not retroactively protect all earlier legacy records.

Readers and writers use the same OS-managed local lock. A long-running writer
cannot lose its lock just because a timestamp is old; process exit releases
the lock automatically. The `.lock` sidecar remains on disk and must not be
deleted while workers are running. Stop all old-version workers before upgrading
from the previous age-based lock implementation. This is a local-filesystem
guarantee, not distributed locking across hosts or arbitrary network shares.
Duplicate JSON keys, non-finite numbers, malformed chain envelopes, and
unterminated final lines are rejected. Preserve damaged logs for investigation;
SafeDBA never silently truncates or repairs them.

Durable Agent, experience, and incident state is stored in local SQLite files
under `logs/`. These files are useful for development and recovery, but they
are not WORM-compliant audit storage.

## IncidentWorkflow

`IncidentWorkflow` addresses lock incidents where several blockers need to be
handled as one operator-reviewed event.

It provides:

- one exact-scope approval for the complete observed batch
- deduplication when one blocker affects multiple waiters
- serial execution with a fresh lock graph before every action
- versioned SQLite checkpoints and compare-and-swap updates
- approval expiry and reapproval states
- execution leases for worker coordination
- conservative crash reconciliation
- fail-closed handling for ambiguous or in-doubt outcomes

The workflow does not treat an approval as permission to terminate any future
blocker. Scope expansion always requires a new incident and approval.

Useful commands:

```powershell
python src/main.py --incidents
python src/main.py --resume <incident-id>
```

## Configuration

The complete configuration template is documented in `.env.example`.
Important groups include:

| Group | Purpose |
|---|---|
| `SAFEDBA_DB_*` | read-only observer connection and query limits |
| `SAFEDBA_EXECUTOR_DB_*` | maintenance executor identity |
| `SAFEDBA_TERMINATOR_DB_*` | isolated backend-termination identity |
| `SAFEDBA_LLM_*` | provider, model, reasoning, timeout, and token settings |
| `SAFEDBA_AGENT_*` | tool budgets, deadlines, evidence freshness, and memory |
| `SAFEDBA_INCIDENT_*` | workflow database, approval TTL, leases, and deadlines |
| `SAFEDBA_AUDIT_*` | audit path and query-text policy |
| `SAFEDBA_OTEL_*` | optional OTLP/HTTP tracing endpoint, service name, and timeout |
| `SAFEDBA_ENV`, `SAFEDBA_ENABLE_*`, `SAFEDBA_ALLOW_*` | startup execution privilege ceiling |
| `SAFEDBA_RUNTIME_CONTROLS_*` | trusted, reloadable deny-only stop controls |
| `SAFEDBA_PROCESS_ROLE`, `SAFEDBA_EXECUTOR_URL`, `SAFEDBA_EXECUTOR_API_TOKEN` | opt-in process separation and loopback submission |
| `SAFEDBA_EXECUTION_GRANT_DB_PATH` | worker-private operator grants, never Agent-writable |

Configuration values are validated against safety bounds at startup.
`SAFEDBA_SKIP_DOTENV=1` explicitly disables loading the local `.env` file;
the disposable test runner uses this to prevent development settings from
leaking into isolated evaluation processes.

### Independent lock executor

`SAFEDBA_PROCESS_ROLE=agent` loads only the observer password and model key;
`SAFEDBA_PROCESS_ROLE=executor` loads observer/termination credentials and
rejects model and maintenance passwords. Neither isolated profile loads
`.env`. They must be launched with separately populated process environments.
The default `combined` profile preserves local development behavior.

An Agent request contains only incident/action/operation references and a
proposal digest. The worker re-reads configured durable state, consumes an
operator-side exact-scope grant, checks TTL/lease/fresh evidence, and invokes
the existing deterministic executor. It cannot accept arbitrary SQL or DSNs.
Lost or malformed responses are not retried; uncertain actions require review.

One operator batch grant **plus** one existing workflow confirmation can cover
multiple blockers. This adds a separate trust boundary, not per-blocker
prompts. It currently supports lock termination only. See
[configuration, trust boundary and recovery](ISOLATED_EXECUTOR.md).

Subprocess separation is tested; OS isolation is **not automatically
installed**. Use distinct service identities and protect worker secrets,
grant storage, deployed code and controls with ACLs. Two processes under the
same unrestricted account are not a security sandbox.

### Runtime policy

| Environment | Catalogs / estimated plans | Runtime EXPLAIN | Repeated benchmarks | Mutations |
|---|---|---|---|---|
| `development`, `benchmark` | Available | Available by default | Available by default | Existing approval gates |
| `staging` | Available | Available by default | Explicit opt-in | Index/statistics workflows also require benchmark opt-in |
| `production` | Available | Prohibited | Prohibited | Disabled by default; only lock termination can be explicitly enabled |

Production always blocks `CREATE_INDEX`, `ANALYZE_TABLE`, `REWRITE_QUERY`,
rollback `DROP_INDEX`, and query-result comparison. The existing workflows
measure workloads and are not online production deployment workflows.
Deliberate production lock termination requires both
`SAFEDBA_ENABLE_MUTATIONS=true` and `SAFEDBA_ENABLE_TERMINATE_BACKEND=true`;
this never replaces evidence binding, human approval, or final identity checks.
Individual `SAFEDBA_ENABLE_<ACTION_TYPE>` flags can further restrict execution.
Benchmark requests reject non-integer, negative, empty, or more than 20 total
warmup/sample executions. Existing SQL timeouts and Agent budgets still apply.

Inspect effective policy without running a model or database action:

```powershell
python src/runtime_policy.py
# Equivalent through the main CLI:
python src/main.py --runtime-policy
```

An operator-owned JSON file at `SAFEDBA_RUNTIME_CONTROLS_PATH` can restrict
running processes without restarting them. The default path is
`logs/runtime_controls.json`, resolved relative to the project root. Example:

```json
{
  "version": 1,
  "disable_agent": false,
  "disable_mutations": true,
  "disable_runtime_analysis": false,
  "disable_benchmarks": true,
  "disabled_actions": ["TERMINATE_BACKEND"]
}
```

Set `disable_agent` to stop subsequent model turns and database-tool dispatches.
Controls are checked again at execution boundaries and between benchmark
samples. They cannot grant permissions denied at startup. Unknown keys,
duplicate keys, wrong types, unreadable files, and oversized files fail closed.
Use UTF-8 without BOM and atomically replace the file on the same filesystem;
a partially written file deliberately blocks new operations. Protect its
directory using OS permissions and keep it outside untrusted tool access.

For deployments, set `SAFEDBA_RUNTIME_CONTROLS_REQUIRED=true` after provisioning
the file: a missing/deleted file then blocks operations. With the default
`false`, absence means no additional live restrictions and restores the startup
ceiling. Changing `.env` alone does not update a running process.

These are cooperative stop controls, not cancellation of already-issued SQL
or an in-flight provider request (including that request's retries/fallback),
or a security boundary against a process that can edit its own code/config.
If the policy blocks an incident step before its execution claim, the pending
steps return to fresh approval; completed steps are not repeated on resume.
If a claim or side effect may already have happened, the existing reconciliation
and manual-review rules remain. Stop controls also apply to rollback mutations:
if cleanup is blocked, inspect the retained state before performing manual
recovery. Removing a stop does not automatically resume a workflow.

### Model resilience

Each configured model route has a circuit breaker. It counts only final
transient failures after the provider client's own retry policy is exhausted:
connection failures, timeouts, rate limits, HTTP 408, and HTTP 5xx responses.
Authentication, permission, malformed-request, and local adapter errors fail
immediately and never trigger automatic fallback.

The fallback route is disabled by default and requires an explicit provider,
model, endpoint when required, and a separate API key. Successful and failed
Agent results record the selected provider/model, circuit state, and failover
reason without recording credentials or provider error messages.

### Observability

OpenTelemetry tracing is disabled by default. When enabled, Agent runs
correlate runtime security attestation, model requests, and tool calls. Durable
lock-workflow passes correlate approval, fresh observations, deterministic
execution, and post-action verification. Both returned results include their
trace ID. The exporter uses OTLP over HTTP and is intended to point at an
OpenTelemetry Collector.

Span attributes contain bounded operational metadata only. SafeDBA does not
attach prompts, SQL, tool arguments/results, credentials, thread IDs, or
session IDs to spans. Existing structured `model_trace`, `tool_trace`, and
audit records remain available for local inspection.

## Operational Requirements and Scope

- the SQL guard is a conservative lexer/policy, not a complete PostgreSQL AST
  parser
- `EXPLAIN ANALYZE` executes accepted read-only queries and cannot eliminate
  all workload risk
- cost estimates are not perfect predictors of runtime or resource use
- runtime observations are point-in-time snapshots
- rewrite comparison checks one database snapshot; it does not prove semantic
  equivalence for every possible state
- index creation uses before/after workload benchmarks; online index lifecycle
  management is outside the current workflow
- backend termination has no application-level compensating rollback
- SQLite memory, workflow, and experience files need external backup,
  retention, and tamper protection in production
- the local audit hash chain detects retained-chain corruption but cannot
  prevent whole-file deletion, tail truncation, or privileged local tampering;
  production still needs externally anchored or WORM audit storage
- default combined mode still loads multiple DB identities; the independent
  lock worker is opt-in and requires separate-account/ACL deployment testing
- evaluation results apply to the documented cases; deployment-specific
  validation requires representative workloads and held-out cases
- the CLI has no multi-tenant RBAC or asynchronous approval service
- fallback models can produce materially different reasoning and must be
  evaluated against the same safety and regression suites before enablement

Before connecting a deployment to a managed database, configure least-privilege
roles, protected secrets and state, process isolation, backups, and operator
approval procedures. Validate the enabled workflows against that environment's
workload and recovery requirements.
