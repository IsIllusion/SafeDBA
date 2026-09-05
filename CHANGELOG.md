# Changelog

This file records implemented project changes, not production certifications
or claims that all configured CI platforms have already been exercised.

## 2026-09-05 — Evaluation and execution-isolation groundwork

### Added

- An opt-in independent lock worker with `agent` and `executor` process
  profiles. The Agent has no maintenance/termination password; the worker
  has no model key or maintenance password. Isolated profiles ignore `.env`.
- A bounded, authenticated loopback execution protocol accepting action
  references rather than SQL, DSNs, approval objects or caller-selected paths.
- Operator-private SQLite action grants with a maximum five-minute TTL,
  single-use consumption, exact scope/database binding, and preview-digest
  confirmation. Consumed grants cannot be silently reminted.
- A disposable PostgreSQL runner and per-instance target guard, covering real
  role privileges, lock incidents, action interruption and identity-bound cleanup.
- Real subprocess tests for multi-blocker execution, missing/tampered grants
  and worker exit after a database effect. Transport uncertainty requires
  manual review instead of automatic retry.
- A bounded real-provider smoke evaluator: three synthetic read-only cases,
  at most 12 requests, no SDK retries, thinking, fallback, memory or training.
- GitHub Actions configuration for Windows/Linux Python 3.10/3.12 tests and
  PostgreSQL 16/18 integration jobs. Ordinary CI does not call a live model.
- Environment-specific execution policy, deny-only reloadable stop controls,
  transient-error circuit breakers, optional explicit model fallback, and
  optional metadata-only OpenTelemetry tracing.

### Hardened

- Audit append coordination uses OS-managed local locking and rejects
  malformed JSON and damaged retained chains without silent repair.
- Interrupted lock workflows retain exact approval scopes, action identities,
  fresh-evidence checks and conservative reconciliation behavior.
- Worker grants are rechecked during the atomic workflow execution claim;
  changing Agent-writable workflow state does not broaden the private grant.
- Evaluation setup failures/timeouts cannot inherit a successful deterministic
  test status. Typed token accounting is retained without exposing credentials.

### Verified locally

- 268 portable unit/protocol tests passed; 18 real PostgreSQL integration
  tests passed with zero skips in the dedicated run on PostgreSQL 18.4.
- The real DeepSeek `deepseek-v4-flash` smoke baseline passed its three
  automatic case checks in eight requests, using 57,742 prompt and 2,432
  completion tokens. The synthetic lock remained unmodified by the Agent.
- Temporary PostgreSQL clusters were stopped and removed. The user's `.env`,
  credentials and business databases were not changed by these evaluations.

The live-model baseline predates the final worker/reporter hardening; its
recorded source fingerprint and the scope of subsequent regression checks are
documented in [the evaluation report](docs/LIVE_MODEL_EVALUATION.md).

### Limitations retained

- Three visible smoke cases are not an independent holdout or a production
  accuracy estimate. Manual review found verbosity, language inconsistency,
  and health conclusions broader than the available snapshot justified.
- Independent execution currently supports persisted lock workflows only.
  One operator-side batch grant plus one workflow confirmation can cover
  multiple blockers; the default combined development mode still exists.
- Separate OS accounts, filesystem ACLs and production service deployment
  are not installed automatically. Same-account subprocess tests do not prove
  protection against a compromised process.
- There is no online self-training, multi-tenant approval/RBAC service,
  production-online DDL workflow, or independently anchored/WORM audit store.

See [the project overview](README.md) and
[the executor setup/recovery guide](docs/ISOLATED_EXECUTOR.md).
