# Changelog

This file records implemented project changes, not production certifications
or claims that all configured CI platforms have already been exercised.

## 2026-09-08 — Modular refactor and compatibility consolidation

### Changed

- Reduced the public Agent module from 3,691 to 204 lines by extracting
  run-local state, dependency composition, graph nodes, serial tool processing,
  prompts, tool contracts and execution-result review into focused modules.
- Consolidated repeated tool replies, strict JSON/hash encoding, identifier
  predicates, deterministic index naming and identical state-store connections.
- Separated pure benchmark grading from model/database execution and reporting.
- Preserved public calls, prompts, all 15 tool contracts, result shapes,
  persistence schemas, runtime policy and independent approval/execution gates.
- Added a [module ownership and maintenance guide](docs/CODE_STRUCTURE.md).

### Verified locally

- 365 portable tests passed on Python 3.10.20 and 3.12.14, including 42 new
  refactor checks against frozen commit `d42113b`; both dependency environments
  passed consistency checks.
- All 19 PostgreSQL 18.4 integration tests passed in three consecutive runs,
  including real three-blocker approval and cross-process execution recovery.
- No observed regression in covered behavior; no live-model requests, schema
  migration or new Agent capability is part of this refactor.

## 2026-09-08 — LangGraph and LangChain migration

### Changed

- Replaced the custom diagnostic iteration loop with a compiled LangGraph
  `StateGraph`, with separate model, serial-tool, and evidence-citation nodes.
- Routed compatible providers through a LangChain `BaseChatModel` adapter and
  added native `chat_model` injection to diagnosis and execution-result review.
- Added `StructuredTool` dispatch using the registry's exact schemas and
  capability metadata, retaining all deterministic authorization checks.
- Preserved existing CLI/result contracts, SQLite memory and incident schemas,
  provider resilience, execution approval, and independent-worker isolation.
- Kept raw malformed calls and reasoning extensions available to validation;
  native message history retains provider-specific and signed content blocks.
- Separated graph steps from model-turn limits and disabled framework node
  retry, model caching, and automatic payload tracing in diagnostic runs.

### Verified locally

- 323 portable tests passed on Windows with Python 3.10.20 and 3.12.14;
  dependency consistency checks passed in both environments.
- 31 differential tests compare the migrated runtime with a frozen reference
  from commit `415486a`, including memory lifecycle and deadline behavior.
- 19 LangChain contract tests and five graph-scheduler tests cover native
  models, exact schemas, message preservation, budget alignment, and isolation.
- 19 PostgreSQL 18.4 integration tests passed in three consecutive final runs,
  including a native LangChain
  graph producing three real blocker proposals before one workflow approval,
  and existing cross-process grant/crash safety checks.
- Stabilized a Windows HTTP test's request framing without changing executor
  authentication or application behavior. CI now checks dependency consistency.

No paid model requests were made for this migration. The previous live-model
evaluation remains a historical baseline, not a post-migration model score.
Diagnostic LangGraph checkpoint/replay is not enabled; existing durable lock
recovery remains authoritative. See [the migration guide](docs/LANGGRAPH_MIGRATION.md).

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
