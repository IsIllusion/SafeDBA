# Code Structure and Maintenance

SafeDBA separates diagnostic orchestration from database authority. The public
Agent API composes explicit dependencies; graph nodes do not import the
application configuration, database tools, or provider factory directly.

## Diagnostic ownership

```text
agent.py                         Public API and dependency composition
  AgentDependencies              Explicit integration contracts
  AgentRunContext                One run's state, memory and completion lifecycle
  DiagnosticAgent                Runtime preflight, model and answer nodes
    AgentToolExecutor            Serial observations/proposals and tool replies
    agent_graph                  LangGraph transitions and iteration limits
    langchain_bridge             Model messages and StructuredTool interfaces
```

Mutable state belongs to one `AgentRunContext`; it is not shared between runs.
The facade keeps `run_agent`, `review_execution_result`, `call_tool`, registry
access and historical imports compatible. New modules should use the explicit
dependencies rather than import `agent` back into the diagnostic core.

## Module map

| Area | Modules | Responsibility |
|---|---|---|
| Entry points | `main`, `agent`, `agent_example` | CLI routing, public API, direct-module example |
| Run orchestration | `agent_dependencies`, `agent_context`, `agent_runtime`, `agent_tool_execution`, `agent_graph` | Integration boundary, lifecycle, serial graph nodes |
| Model integration | `llm_provider`, `langchain_bridge`, `model_metadata` | Provider resilience, native interfaces, bounded usage/routing metadata |
| Tool contracts | `agent_tool_catalog`, `agent_tools`, `tool_registry` | Exact wire schemas, bindings and capabilities |
| Model instructions | `agent_prompts`, `agent_review` | Diagnostic instructions and read-only execution-result explanation |
| Evidence and diagnosis | `agent_policy`, `diagnostics`, `db_tools`, `query_guard` | Evidence ledger, plan analysis, PostgreSQL observations and SQL policy |
| PostgreSQL observation internals | `db_observation_context`, `db_catalog`, `db_operational`, `db_sessions` | Explicit read-only dependencies, catalog data, operational snapshots, sessions and lock graphs |
| Actions | `actions`, `safety`, `executor` | Proposal construction, risk classification, controlled execution and verification |
| Durable incidents | `incident_workflow`, `incident_approval`, `workflow_store` | Multi-blocker state, exact approval scope, durable claims and recovery |
| Independent execution | `execution_client`, `execution_protocol`, `execution_grants`, `executor_worker` | Reference-only transport and worker-private authorization |
| Memory and learning records | `agent_memory`, `experience_store`, `learning_cli` | Scoped historical context and reviewed offline datasets |
| Reference knowledge | `knowledge_base`, `agent_knowledge`, `knowledge_cli`, `knowledge_evaluate` | Optional reviewed corpus, deployment-scoped retrieval, separate citations and offline evaluation |
| Shared primitives | `serialization`, `identifiers`, `state_database` | Strict canonical JSON, deterministic identifiers, common state-store connection profile |
| Evaluation | `evaluate`, `evaluation_policy`, `live_evaluate`, `rag_ab_evaluate`, `integration_guard` | Benchmark runner, pure grading, bounded live-model/RAG evaluation and disposable-target checks |
| Runtime services | `config`, `runtime_policy`, `audit`, `telemetry` | Configuration, deny-only controls, integrity records and metadata tracing |
| External protocol adapter | `mcp_adapter`, `mcp_server` | Existing protocol exposure and adapter dispatch |

`tests/` contains portable behavior and protocol checks. `tests/integration/`
contains opt-in PostgreSQL scenarios; `scripts/run_postgres_integration.py`
creates and removes their disposable database. `scripts/manual/` retains
manual database diagnostics, which are not duplicates of the automated runner.

## PostgreSQL observation ownership — 2026-09-15

The public `db_tools` interface remains the composition and compatibility
boundary. Nine catalog/runtime observation implementations now live in three
focused modules:

| Module | Owns | Lines |
|---|---|---:|
| `db_catalog` | Indexes, table columns, column metadata and statistics | 183 |
| `db_operational` | Health and connection/VACUUM/replication/storage snapshots | 499 |
| `db_sessions` | Active/open-transaction sessions and identity-bound lock snapshots | 443 |
| `db_observation_context` | Immutable, per-call settings and read-only callbacks | 25 |
| `db_tools` | Public wrappers, connection policy/attestation, query execution gates and mutations | 1,127 |

Previously, `db_tools.py` alone contained 2,364 lines. These five files now total
2,277 lines, a net reduction of 87. The primary benefit is separating ownership
and dependencies, not a claim that moving code makes database queries faster.
Counts include SQL, comments and formatting. Across `src/*.py`, there are now
55 modules and 25,212 lines, including evaluation code.

`ObservationDependencies` supplies a read-only connection factory, redaction,
health and clock callbacks, database name, row limit and triage thresholds.
It contains no credential dictionary or write-operation callback. The extracted
modules do not import deployment configuration, `psycopg`, executors or the
public facade. They can be imported and tested without a configured database.
This is a code-ownership boundary, not an OS sandbox or tenant authorization.

The facade composes dependencies on every invocation, so existing public
overrides and test seams still work. The original `readonly_connection` retains
the runtime policy check, read-only transaction setup and timeouts. No pooled
connection, cached snapshot or global mutable observation context was added.

Compatibility is anchored to the pre-extraction `db_tools.py` from commit
`7fbc51badaa1c3aae5f1f83091edc24a81a9262a`; that file was unchanged by the RAG
work. `tests/fixtures/db_observation_contract.json` records all 26 function AST
and signature hashes captured before editing. The nine moved functions are
checked after reversing only explicit dependency wiring. SQL literals, return
fields, branches, statement order and digest serialization are not normalized.
The remaining 17 function bodies and every existing signature must stay equal.

The alignment suite also reconstructs the hash-verified original functions in
test memory and compares results, SQL/parameter transcripts, exception types,
connection cleanup and unconsumed rows. It covers missing/partial catalog data,
primary/standby snapshots, redaction, nulls, truncated/empty lock graphs and
failures at each operational query. A negative-control test detects SQL/output
field edits; separate checks cover policy refusal and independent concurrent
contexts. No duplicate legacy application runtime is retained.

To modify an observation, work in its owner above; keep the public wrapper,
tool contract and policy boundary stable. Update the frozen comparison only as
an explicitly reviewed behavior change, not to hide a regression. Execution
and durable workflow modules remain separate and were not refactored here.

```powershell
python -m unittest discover -s tests -p test_db_observation_alignment.py -v
```

Verification after extraction, on Windows:

- 18 new alignment tests passed. Full discovery on Python 3.10 and 3.12 each
  ran 470 tests: 449 passed and 21 opt-in database tests skipped. Both installed
  dependency environments passed consistency checks.
- The separate PostgreSQL 18.4 runner passed all 21 integration tests in three
  consecutive passes, with no skips/errors/failures. These include native
  LangChain three-lock proposals, RAG-prefetched three-lock approval, independent
  worker grant denial and crash recovery. The temporary server was stopped and
  its cluster removed successfully.
- The 13-case retrieval fixture still passed. No paid model evaluation ran;
  earlier RAG scores do not constitute a new accuracy measurement for this
  refactor. Existing RAG and framework-alignment tests passed in the full suite.
- All 55 source modules passed syntax compilation. The final source fingerprint
  matched the database integration report after verification.

Local report: `logs/integration-runs/b832ce11-605e-4cb7-9c1d-cb822f25409c/report.json`.
Source/integration fingerprint:
`b8d7da3b53f9354844753fa39d7de0aa360c86a0d3b8a317c7a4c403227e18fe`.
These checks cover the tested contracts, not arbitrary future database workloads.

## Consolidated implementations

- Tool schemas use common envelope/parameter builders without changing the
  15 names, descriptions, parameter order, requirements or capability metadata.
- Database bindings use one dispatch table. Positional and keyword argument
  shapes are preserved, including the special multi-stage `analyze_query` path.
- Invalid-argument replies and policy/duplicate rejections use shared helpers.
  Error types, evidence-reference allocation, trace fields and ordering remain
  unchanged. Completion reuses one proposal-type projection.
- Audit, incident approval/state and worker-protocol hashing share strict
  canonical JSON. Historical encodings and therefore stored hashes remain
  compatible. Experience storage retains its domain-specific exception wrapper.
- Proposal creation and benchmark grading use the same UTF-8-safe PostgreSQL
  index naming algorithm. UUID and positive-integer predicates are shared.
- Memory and incident stores share their identical SQLite connection profile:
  autocommit, foreign keys, 5-second busy timeout, and `synchronous=FULL`.
- Pure evaluation rules live in `evaluation_policy`; `evaluate` keeps database
  setup, case execution, reporting and compatible grading exports.

## Boundaries intentionally kept separate

Similar validation does not always mean redundant validation. Proposal shape,
fresh evidence, operator approval, worker grant and atomic execution-claim
checks protect different trust boundaries. Removing one would change the
authorization model, so each remains in its original owner.

The experience store's transaction/connection policy differs from the memory
and incident stores. It is not forced onto their shared connection helper.
Likewise, lossy/redacted observation serialization and strict hash
serialization have different contracts and remain separate.

Prompts are relocated unchanged, not shortened. Frozen references under
`tests/fixtures/` are intentional test-only duplication; there is no legacy
runtime selection path in the application. No persistent database schema,
approval protocol, environment option or LangGraph replay policy changes.

## Refactor size and verification — 2026-09-08 baseline

The measurements below describe the completed modular refactor, before the
optional knowledge feature. See [controlled retrieval](KNOWLEDGE_RETRIEVAL.md)
for its current module responsibilities, scope rules and evaluation.

Reference commit: `d42113b26db74d51f064f86a5ac4607a34241845`.
All 32 original application modules were inventoried for responsibilities and
duplicate function bodies before selecting the extractions.

| Measurement | Before | After |
|---|---:|---:|
| `src/agent.py` lines | 3,691 | 204 |
| Public `run_agent` function lines | 1,490 | 45 |
| `src/evaluate.py` lines | 1,844 | 444 |
| All `src/*.py` lines | 25,021 | 23,742 |
| Application modules | 32 | 46 |

Line counts include comments, prompts and formatting, exclude tests, and are
not a complexity score. More focused modules reduce responsibility coupling;
the net source reduction is 1,279 lines (about 5.1%). The largest new lifecycle
module is 588 lines, the model-node module 298, and serial tool execution 269.

Local verification on Windows, 2026-09-08:

- 365 portable tests passed on Python 3.10.20 and 3.12.14; both dependency
  environments passed consistency checks. Default discovery includes 19
  opt-in database tests, for 384 tests in total.
- 42 new refactor checks compare the pinned runtime, exact prompt/schema
  hashes, every tool binding, native LangChain messages, persistence results,
  failure handling, shared primitives and grading results. Earlier migration
  alignment and interface tests remain unchanged.
- Frozen runtime/helper/grading function ASTs were checked against the pinned
  source. Runtime comparisons exclude wall-clock durations; stored memory
  comparisons also exclude generated memory-row IDs. No business fields are
  normalized away.
- All 19 PostgreSQL 18.4 integration tests passed in three consecutive runs,
  with zero skips: real multi-blocker approval, independent worker grants,
  crash reconciliation, role boundaries and index catalog behavior.

No regression was observed in the covered contracts. This is not a proof of
equivalence for every possible provider or database workload. No paid model
requests were made. A behavioral regression should block publication and be
fixed or reverted to the reference behavior, not hidden by updating an oracle.

## Working on this structure

1. Change schemas in `agent_tool_catalog` and bindings in `agent_tools`; keep
   schema and dispatch contracts covered together.
2. Change model orchestration in `agent_runtime` or `agent_graph`; keep database
   authority outside both. A graph transition does not grant execution rights.
3. Change run persistence in `agent_context` and the relevant store; check both
   terminal results and durable records, including failure paths.
4. Change grading independently in `evaluation_policy`. Grading must not
   initialize a model, open a database, or import CLI configuration.
5. Run portable tests and the disposable integration suite before publishing
   changes to execution, persistence, hashing or orchestration.
6. Keep catalog, operational and session observations in their domain modules;
   inject `ObservationDependencies` instead of importing `db_tools` or `config`
   back into those modules.

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_refactor_alignment.py -v
python scripts/run_postgres_integration.py --pg-bin "C:\Program Files\PostgreSQL\18\bin" --repeat 3
```
