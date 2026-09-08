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
| Actions | `actions`, `safety`, `executor` | Proposal construction, risk classification, controlled execution and verification |
| Durable incidents | `incident_workflow`, `incident_approval`, `workflow_store` | Multi-blocker state, exact approval scope, durable claims and recovery |
| Independent execution | `execution_client`, `execution_protocol`, `execution_grants`, `executor_worker` | Reference-only transport and worker-private authorization |
| Memory and learning records | `agent_memory`, `experience_store`, `learning_cli` | Scoped historical context and reviewed offline datasets |
| Shared primitives | `serialization`, `identifiers`, `state_database` | Strict canonical JSON, deterministic identifiers, common state-store connection profile |
| Evaluation | `evaluate`, `evaluation_policy`, `live_evaluate`, `integration_guard` | Benchmark runner, pure grading, bounded live-model evaluation and disposable-target checks |
| Runtime services | `config`, `runtime_policy`, `audit`, `telemetry` | Configuration, deny-only controls, integrity records and metadata tracing |
| External protocol adapter | `mcp_adapter`, `mcp_server` | Existing protocol exposure and adapter dispatch |

`tests/` contains portable behavior and protocol checks. `tests/integration/`
contains opt-in PostgreSQL scenarios; `scripts/run_postgres_integration.py`
creates and removes their disposable database. `scripts/manual/` retains
manual database diagnostics, which are not duplicates of the automated runner.

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

## Refactor size and verification

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

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_refactor_alignment.py -v
python scripts/run_postgres_integration.py --pg-bin "C:\Program Files\PostgreSQL\18\bin" --repeat 3
```
