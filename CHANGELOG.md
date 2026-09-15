# Changelog

This file records implemented project changes, not production certifications
or claims that all configured CI platforms have already been exercised.

## 2026-09-15 — Public project documentation

- Reorganized the README around capabilities, architecture, installation,
  usage, knowledge retrieval, deployment and development workflows.
- Moved detailed execution policies, audit handling and runtime configuration
  into `docs/OPERATIONS.md`, preserving operational requirements.
- Kept evaluation methodology and historical results in the dedicated reports;
  the project overview does not present regression scores as production accuracy.

## 2026-09-15 — PostgreSQL observation module separation

- Extracted nine observations into catalog, operational and session/lock
  modules, composed through immutable per-call read-only dependencies. These
  modules have no deployment config, driver or executor imports.
- Retained all 26 existing `db_tools` function signatures, exact SQL and result
  contracts, redaction, lock identity/digest handling and connection policy.
  Execution, approval and durable workflow implementations were not changed.
- Reduced `db_tools.py` from 2,364 to 1,127 lines. The facade and four new modules
  total 2,277 lines, a net reduction of 87; this is a maintainability change, not
  a database-speed or model-accuracy claim.
- Added 18 alignment tests with pre-edit function hashes anchored to `7fbc51b`,
  exact transcript/result/error replay, unchanged security/mutation bodies,
  import isolation, policy refusal and independent observation contexts.
- Verified 449 portable tests on Python 3.10 and 3.12, dependency consistency in
  both environments, 21 PostgreSQL 18.4 tests in three consecutive runs, and the
  13-case retrieval fixture. See `docs/CODE_STRUCTURE.md` for evidence and limits.
- No paid model calls, business database access, dependency or configuration
  changes. Earlier RAG scores remain historical, not newly measured here.

## 2026-09-15 — RAG reliability repairs

- Added budgeted, explicitly attributed initial knowledge lookup for internal
  reference questions, using the existing scoped LangChain tool. Pure diagnosis
  and explicit lookup opt-outs do not trigger it.
- Added bounded completion checks for mixed knowledge/current-observation
  requests; reading a runbook does not replace requested database observations.
- Normalized exact citation-format placeholders without accepting fabricated
  source IDs. Empty evidence ledgers now reject invented numeric citations,
  including with RAG disabled; this is an intentional validation tightening.
- Added runtime applicability timestamps and a narrow check for contradictory
  present-tense expiry claims. This is not general semantic entailment checking.
- Added 21 portable tests and a real three-lock prefetch/approval integration
  case. Retried only transient Windows sharing violations during owned test
  workspace cleanup, preserving other cleanup failures.
- Verified 431 portable tests on Python 3.10 and 3.12, 21 PostgreSQL 18.4 tests
  in three consecutive final runs, and all 13 retrieval fixture cases.
- Final paired regression: RAG disabled 10/24, enabled 24/24; enabled average
  reported tokens fell from the earlier 17,433.1 to 13,101.3 per session.
  Retained intermediate failures and an automatically unscored semantic error
  in `docs/RAG_REPAIR_VERIFICATION.md`; the reused cases are not a holdout.
- No new dependency, database schema or approval authority; no deployed
  knowledge or `.env` changes, and no automatic training.

## 2026-09-15 — Paired real-model RAG evaluation

- Added an opt-in isolated evaluator using the existing diagnostic graph and
  reference integration, with synthetic observations and no database adapters.
- Added 12 fixed bilingual tasks, alternating off/on order, two bounded repeats,
  independent fact/citation grading, request accounting and source fingerprints.
- Ran 48 Agent sessions against the configured DeepSeek model: strict passes
  were 8/24 without RAG and 17/24 with RAG; internal-knowledge passes were 0/12
  and 9/12. Refusal cases regressed from 4/6 to 3/6. Retained all failures.
- Recorded missed retrieval, placeholder-citation repair loops, irrelevant
  retrieval noise, and semantic limitations in `docs/RAG_AB_EVALUATION.md`.
- Verified 410 portable tests on Python 3.10 and 3.12 (11 new evaluator tests).
  The 20 opt-in PostgreSQL tests were skipped, not rerun, in this evaluation.
- No changes to production Agent behavior, deployed knowledge or `.env`.

## 2026-09-12 — Controlled reference knowledge retrieval

### Added

- Optional, default-off `search_knowledge` integration using the existing
  LangChain/LangGraph path and a run-local registry; no new dependencies.
- Operator-reviewed local JSON bundles with source/revision metadata, expiry,
  deployment scope, environment and PostgreSQL-major filtering before ranking.
- Bounded English/CJK lexical BM25 search, independent `kb-...` citations,
  delivery/expiry/revocation checks and safe unavailable/empty-result behavior.
- Operator-only validation and exclusive-file publication, with private knowledge
  files ignored by Git and common credential-pattern checks.
- A replaceable retriever interface, documented setup, and a 13-case synthetic
  retrieval evaluator with an explicit no-knowledge baseline.

### Verified locally

- 399 portable tests passed on Windows Python 3.10.20 and 3.12.14, including
  34 additional retrieval, integration-adapter, evaluation and configuration checks.
- 20 PostgreSQL 18.4 tests passed in three runs, including an additional
  knowledge-enabled three-blocker case retaining one workflow approval.
- Existing frozen runtime/prompt/tool alignment tests still pass when knowledge
  is disabled. No database schema, approval gate or independent-worker authority
  changes; no paid model calls or automatic training.

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
