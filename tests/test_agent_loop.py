from collections import deque
from contextlib import contextmanager
import importlib.util
import json
import math
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


CALLS = []


def _load_agent_module():
    stubs = {}

    llm_provider = ModuleType("llm_provider")
    llm_provider.get_llm_provider = lambda: None
    stubs["llm_provider"] = llm_provider

    db_tools = ModuleType("db_tools")

    def record(name, result):
        def function(*args, **kwargs):
            CALLS.append((name, args, kwargs))
            return result
        return function

    db_tools.get_active_sessions = record(
        "get_active_sessions",
        [],
    )
    db_tools.get_column_info = record(
        "get_column_info",
        {"data_type": "timestamp"},
    )
    db_tools.get_column_stats = record(
        "get_column_stats",
        {"n_mod_since_analyze": 100},
    )
    db_tools.get_database_health = record(
        "get_database_health",
        {"active_sessions": 0},
    )
    db_tools.get_estimated_query_plan = record(
        "get_estimated_query_plan",
        {"Plan": {"Total Cost": 1}},
    )
    db_tools.get_indexes = record(
        "get_indexes",
        [],
    )
    db_tools.get_lock_waits = record(
        "get_lock_waits",
        [
            {
                "blocked_pid": 1,
                "blocker_pid": 2,
            }
        ],
    )
    db_tools.get_operational_snapshot = record(
        "get_operational_snapshot",
        {
            "runtime_health": {"active_sessions": 0},
            "connection_capacity": {
                "max_connection_utilization_pct": 10.0,
            },
            "vacuum": {"tables": []},
            "replication": {"server_role": "primary", "standbys": []},
            "storage_usage": {"database_bytes": 1_000_000},
        },
    )
    db_tools.get_query_plan = record(
        "get_query_plan",
        {"Plan": {"Node Type": "Seq Scan"}},
    )
    db_tools.get_transaction_sessions = record(
        "get_transaction_sessions",
        [],
    )
    db_tools.verify_runtime_security = lambda: {
        "observer": {"user": "observer"},
        "executor": {"user": "executor"},
        "terminator": {"user": "terminator"},
    }
    stubs["db_tools"] = db_tools

    diagnostics = ModuleType("diagnostics")
    diagnostics.analyze_query_plan = lambda plan: {
        "scan_nodes": [
            {
                "scan_type": "Sequential Scan",
                "table": "orders",
                "filter": "customer_id = 1",
                "predicate_columns": ["customer_id"],
                "rows_examined": 1_000,
                "selectivity": 0.001,
            }
        ]
    }
    diagnostics.detect_cardinality_anomalies = (
        lambda analysis: []
    )
    diagnostics.detect_non_sargable_predicates = (
        lambda analysis: []
    )
    stubs["diagnostics"] = diagnostics

    actions = ModuleType("actions")
    actions.build_create_index_proposal = (
        lambda **kwargs: {
            "type": "CREATE_INDEX",
            **kwargs,
            "index_name": "idx_orders_customer_id",
            "risk": "MEDIUM",
        }
    )
    actions.build_query_rewrite_proposal = (
        lambda **kwargs: {
            "type": "REWRITE_QUERY",
            **kwargs,
            "risk": "LOW",
        }
    )
    actions.build_analyze_table_proposal = (
        lambda **kwargs: {
            "type": "ANALYZE_TABLE",
            **kwargs,
            "risk": "MEDIUM",
        }
    )
    actions.build_terminate_backend_proposal = (
        lambda **kwargs: {
            "type": "TERMINATE_BACKEND",
            **kwargs,
            "risk": "HIGH",
        }
    )
    actions.validate_proposal_shape = lambda proposal: {
        "valid": bool(
            isinstance(proposal, dict)
            and isinstance(proposal.get("reason"), str)
            and proposal.get("reason").strip()
            and isinstance(proposal.get("confidence"), (int, float))
            and not isinstance(proposal.get("confidence"), bool)
            and math.isfinite(float(proposal.get("confidence")))
        ),
        "errors": ["invalid built proposal"],
    }
    stubs["actions"] = actions

    config = ModuleType("config")
    config.AGENT_DEADLINE_SECONDS = 60.0
    config.AGENT_MAX_TOOL_CALLS_PER_TURN = 6
    config.AGENT_MAX_TOOL_OUTPUT_CHARS = 60_000
    config.AGENT_MAX_TOTAL_TOOL_CALLS = 16
    config.AGENT_RUNTIME_EVIDENCE_TTL_SECONDS = 15.0
    stubs["config"] = config

    telemetry = ModuleType("telemetry")

    class NoopTelemetryRun:
        trace_id = None

        @contextmanager
        def span(self, name, attributes=None):
            yield None

        def finish(self, **kwargs):
            return None

    class NoopTelemetryManager:
        def start_run(self, **kwargs):
            return NoopTelemetryRun()

    telemetry.get_telemetry_manager = lambda: NoopTelemetryManager()
    stubs["telemetry"] = telemetry

    previous = {
        name: sys.modules.get(name)
        for name in stubs
    }
    sys.modules.update(stubs)

    try:
        spec = importlib.util.spec_from_file_location(
            "agent_under_test",
            SRC / "agent.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        for name, prior in previous.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior

    return module


AGENT = _load_agent_module()


def tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def response(
    *,
    content=None,
    calls=None,
    finish_reason="stop",
    usage=None,
):
    message = SimpleNamespace(
        content=content,
        tool_calls=calls or [],
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


class FakeProvider:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.tool_sets = []
        self.message_batches = []

    def complete(self, *, messages, tools, tool_choice):
        self.message_batches.append(list(messages))
        self.tool_sets.append({
            item["function"]["name"]
            for item in tools
        })
        return self.responses.popleft()

    @staticmethod
    def assistant_message_to_dict(message):
        serialized = {
            "role": "assistant",
            "content": message.content,
        }
        if message.tool_calls:
            serialized["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        return serialized


class CapturingTelemetryRun:
    trace_id = "1234567890abcdef1234567890abcdef"

    def __init__(self):
        self.spans = []
        self.finished = None

    @contextmanager
    def span(self, name, attributes=None):
        self.spans.append((name, attributes or {}))
        yield SimpleNamespace()

    def finish(self, **kwargs):
        self.finished = kwargs


class CapturingTelemetryManager:
    def __init__(self):
        self.started = None
        self.run = CapturingTelemetryRun()

    def start_run(self, **kwargs):
        self.started = kwargs
        return self.run


class AgentLoopTests(unittest.TestCase):

    def test_model_trace_records_selected_fallback_without_secrets(self):
        provider = FakeProvider([
            response(content="Fallback diagnosis completed."),
        ])
        provider.model = "primary-model"
        provider.last_call_metadata = {
            "selected_provider": "fallback-provider",
            "selected_model": "fallback-model",
            "fallback_configured": True,
            "fallback_used": True,
            "failover_reason": "primary_transient_error",
            "primary_error_type": "TimeoutError",
            "primary_circuit_state": "closed",
            "fallback_circuit_state": "closed",
            "api_key": "must-not-leak",
        }

        result = AGENT.run_agent(
            "Investigate without database evidence.",
            provider=provider,
        )

        trace = result["model_trace"][0]
        self.assertEqual(trace["model"], "fallback-model")
        self.assertTrue(trace["provider_route"]["fallback_used"])
        self.assertNotIn("api_key", trace["provider_route"])

    def test_general_incident_can_collect_broad_snapshot_in_one_tool_call(self):
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "snapshot-1",
                        "get_operational_snapshot",
                        {},
                    )
                ],
                finish_reason="tool_calls",
            ),
            response(
                content="Operational evidence collected [ev-0001]."
            ),
        ])

        result = AGENT.run_agent(
            "Investigate general database degradation.",
            provider=provider,
        )

        snapshot_calls = [
            call
            for call in CALLS
            if call[0] == "get_operational_snapshot"
        ]
        self.assertEqual(len(snapshot_calls), 1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["tool_calls_attempted"], 1)
        self.assertIn(
            "get_operational_snapshot",
            provider.tool_sets[0],
        )

    def test_agent_emits_correlated_telemetry_without_payloads(self):
        telemetry = CapturingTelemetryManager()
        provider = FakeProvider([
            response(calls=[
                tool_call(
                    "health-1",
                    "get_database_health",
                    {},
                ),
            ], finish_reason="tool_calls"),
            response(
                content="Database health was observed [ev-0001]."
            ),
        ])

        result = AGENT.run_agent(
            "Sensitive user prompt that must not enter telemetry.",
            provider=provider,
            capture_experience=False,
            telemetry_manager=telemetry,
        )

        self.assertEqual(telemetry.run.trace_id, result["trace_id"])
        self.assertEqual("completed", telemetry.run.finished["status"])
        self.assertEqual(
            [
                "safedba.runtime_security.verify",
                "safedba.llm.complete",
                "safedba.tool.call",
                "safedba.llm.complete",
            ],
            [name for name, _ in telemetry.run.spans],
        )
        self.assertNotIn(
            "Sensitive user prompt",
            repr(telemetry.run.spans),
        )

    def test_model_trace_and_token_usage_are_structured(self):
        provider = FakeProvider([
            response(
                content="Diagnosis completed.",
                usage=SimpleNamespace(
                    prompt_tokens=11,
                    completion_tokens=7,
                    total_tokens=18,
                ),
            ),
        ])

        result = AGENT.run_agent(
            "Explain the current state.",
            provider=provider,
            capture_experience=False,
            verify_environment=False,
        )

        self.assertEqual(result["usage"]["total_tokens"], 18)
        self.assertEqual(result["model_trace"][0]["status"], "success")
        self.assertEqual(
            result["model_trace"][0]["usage"]["prompt_tokens"],
            11,
        )

    def test_persistent_session_memory_is_loaded_as_untrusted_context(self):
        from agent_memory import SQLiteAgentMemory

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteAgentMemory(
                Path(directory) / "memory.sqlite3"
            )
            first_provider = FakeProvider([
                response(content="The first diagnosis completed."),
            ])
            first = AGENT.run_agent(
                "Remember that this is database alpha.",
                thread_id="tenant-a",
                session_id="session-a",
                memory_store=store,
                provider=first_provider,
                capture_experience=False,
                verify_environment=False,
            )

            self.assertTrue(first["memory"]["persisted"])
            self.assertEqual(
                store.get_run(first["run_id"])["status"],
                "COMPLETED",
            )

            second_provider = FakeProvider([
                response(content="The follow-up diagnosis completed."),
            ])
            second = AGENT.run_agent(
                "Continue the database investigation.",
                thread_id="tenant-a",
                session_id="session-a",
                memory_store=store,
                provider=second_provider,
                capture_experience=False,
                verify_environment=False,
            )

            self.assertGreaterEqual(
                second["memory"]["recent_turns_loaded"],
                2,
            )
            memory_messages = [
                item["content"]
                for item in second_provider.message_batches[0]
                if item["role"] == "system"
                and "<agent_memory>" in item["content"]
            ]
            self.assertEqual(len(memory_messages), 1)
            self.assertIn("historical, untrusted", memory_messages[0])
            self.assertIn("database alpha", memory_messages[0])

    def test_completed_run_can_be_captured_for_offline_feedback(self):
        from experience_store import SQLiteExperienceStore

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteExperienceStore(
                Path(directory) / "experience.sqlite3"
            )
            provider = FakeProvider([
                response(content="A sanitized diagnosis summary."),
            ])
            result = AGENT.run_agent(
                "Diagnose SELECT * FROM private_table.",
                provider=provider,
                experience_store=store,
                capture_experience=True,
                verify_environment=False,
            )

            self.assertTrue(result["experience_recorded"])
            saved = store.get_run(result["run_id"])
            self.assertEqual(saved["outcome"], "completed")
            self.assertTrue(saved["summary"]["prompt"]["redacted"])

    def test_optional_experience_store_failure_does_not_block_diagnosis(self):
        provider = FakeProvider([
            response(content="Diagnosis remains available."),
        ])

        result = AGENT.run_agent(
            "Diagnose without a configured experience path.",
            provider=provider,
            capture_experience=True,
            verify_environment=False,
        )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["experience_recorded"])
        self.assertIn(
            "ExperienceInitializationError",
            {item.get("type") for item in result["errors"]},
        )

    def test_run_result_has_stable_run_and_session_identity(self):
        run_id = str(uuid.uuid4())
        provider = FakeProvider([
            response(content="No current database evidence was requested."),
        ])

        result = AGENT.run_agent(
            "Explain the operating mode.",
            run_id=run_id,
            session_id="operator-session-1",
            provider=provider,
            verify_environment=False,
        )

        self.assertEqual(result["run_id"], run_id)
        self.assertEqual(result["session_id"], "operator-session-1")

    def test_invalid_run_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "valid UUID"):
            AGENT.run_agent(
                "Diagnose this request.",
                run_id="not-a-uuid",
                provider=FakeProvider([]),
                verify_environment=False,
            )
    def setUp(self):
        CALLS.clear()

    def test_diagnose_mode_removes_and_rejects_proposal_tools(self):
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "call-1",
                        "propose_terminate_backend",
                        {
                            "blocked_pid": 1,
                            "blocker_pid": 2,
                            "blocker_backend_start": (
                                "2026-01-01T00:00:00+00:00"
                            ),
                            "blocker_xact_start": (
                                "2026-01-01T00:01:00+00:00"
                            ),
                            "reason": "test",
                            "confidence": 0.9,
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            response(content="Diagnosis complete."),
        ])

        result = AGENT.run_agent(
            "Diagnosis only; do not execute anything.",
            mode="diagnose",
            provider=provider,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["proposals"], [])
        self.assertEqual(
            result["tool_trace"][0]["status"],
            "policy_rejected",
        )
        self.assertNotIn(
            "propose_terminate_backend",
            provider.tool_sets[0],
        )

    def test_proposal_is_rejected_until_matching_evidence_exists(self):
        query = (
            "SELECT * FROM orders "
            "WHERE customer_id = 1"
        )
        proposal_args = {
            "query": query,
            "table": "orders",
            "column": "customer_id",
            "reason": "selective predicate",
            "confidence": 0.9,
        }
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "early",
                        "propose_create_index",
                        proposal_args,
                    )
                ],
                finish_reason="tool_calls",
            ),
            response(
                calls=[
                    tool_call(
                        "plan",
                        "analyze_query",
                        {"query": query},
                    ),
                    tool_call(
                        "indexes",
                        "get_indexes",
                        {"table_name": "orders"},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            response(
                calls=[
                    tool_call(
                        "proposal",
                        "propose_create_index",
                        proposal_args,
                    )
                ],
                finish_reason="tool_calls",
            ),
            response(
                content=(
                    "Index proposal is evidence-bound "
                    "[ev-0002] [ev-0003]."
                )
            ),
        ])

        result = AGENT.run_agent(
            "Diagnose and propose an optimization.",
            mode="propose",
            provider=provider,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(
            len(result["proposals"][0]["evidence_refs"]),
            2,
        )
        self.assertEqual(
            result["tool_trace"][0]["status"],
            "policy_rejected",
        )

    def test_invalid_built_proposal_is_not_collected(self):
        query = (
            "SELECT * FROM orders "
            "WHERE customer_id = 1"
        )
        proposal_args = {
            "query": query,
            "table": "orders",
            "column": "customer_id",
            "reason": "selective predicate",
            "confidence": 0.9,
        }
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "plan",
                        "analyze_query",
                        {"query": query},
                    ),
                    tool_call(
                        "indexes",
                        "get_indexes",
                        {"table_name": "orders"},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            response(
                calls=[
                    tool_call(
                        "proposal",
                        "propose_create_index",
                        proposal_args,
                    )
                ],
                finish_reason="tool_calls",
            ),
            response(
                content="Evidence gathered [ev-0001] [ev-0002]."
            ),
        ])
        prior_validator = AGENT.validate_proposal_shape
        AGENT.validate_proposal_shape = lambda proposal: {
            "valid": False,
            "errors": ["synthetic builder defect"],
        }

        try:
            result = AGENT.run_agent(
                "Diagnose and propose an optimization.",
                mode="propose",
                provider=provider,
            )
        finally:
            AGENT.validate_proposal_shape = prior_validator

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["tool_trace"][2]["status"], "error")

    def test_duplicate_observation_executes_only_once(self):
        query = "SELECT * FROM orders"
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "first",
                        "analyze_query",
                        {"query": query},
                    ),
                    tool_call(
                        "duplicate",
                        "analyze_query",
                        {"query": " SELECT *  FROM orders; "},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            response(content="Done [ev-0001]."),
        ])

        result = AGENT.run_agent(
            "Analyze this query.",
            provider=provider,
        )

        plan_calls = [
            call
            for call in CALLS
            if call[0] == "get_query_plan"
        ]
        self.assertEqual(len(plan_calls), 1)
        self.assertEqual(
            result["tool_trace"][1]["status"],
            "blocked_duplicate",
        )

    def test_per_turn_budget_stops_before_any_tool_execution(self):
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        f"call-{index}",
                        "get_database_health",
                        {},
                    )
                    for index in range(7)
                ],
                finish_reason="tool_calls",
            )
        ])

        result = AGENT.run_agent(
            "Investigate database health.",
            provider=provider,
            max_tool_calls_per_turn=6,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            result["stop_reason"],
            "per_turn_tool_budget_exceeded",
        )
        self.assertEqual(CALLS, [])

    def test_empty_choices_returns_structured_failure(self):
        provider = FakeProvider([
            SimpleNamespace(choices=[])
        ])
        result = AGENT.run_agent(
            "Investigate.",
            provider=provider,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["stop_reason"],
            "malformed_provider_response",
        )

    def test_same_turn_observations_cannot_authorize_proposal(self):
        query = (
            "SELECT * FROM orders "
            "WHERE customer_id = 1"
        )
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "plan",
                        "analyze_query",
                        {"query": query},
                    ),
                    tool_call(
                        "indexes",
                        "get_indexes",
                        {"table_name": "orders"},
                    ),
                    tool_call(
                        "proposal",
                        "propose_create_index",
                        {
                            "query": query,
                            "table": "orders",
                            "column": "customer_id",
                            "reason": "selective predicate",
                            "confidence": 0.9,
                        },
                    ),
                ],
                finish_reason="tool_calls",
            ),
            response(
                content="Evidence gathered [ev-0001] [ev-0002]."
            ),
        ])

        result = AGENT.run_agent(
            "Propose an optimization.",
            mode="propose",
            provider=provider,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["proposals"], [])
        self.assertEqual(
            result["tool_trace"][2]["status"],
            "policy_rejected",
        )

    def test_truncated_tool_response_executes_nothing(self):
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "health",
                        "get_database_health",
                        {},
                    )
                ],
                finish_reason="length",
            )
        ])

        result = AGENT.run_agent(
            "Investigate database health.",
            provider=provider,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"], "model_length")
        self.assertEqual(CALLS, [])

    def test_auto_mode_requires_positive_proposal_intent(self):
        diagnose_provider = FakeProvider([
            response(content="No tools needed."),
        ])
        AGENT.run_agent(
            "Please inspect why the database is slow.",
            mode="auto",
            provider=diagnose_provider,
        )
        self.assertNotIn(
            "propose_create_index",
            diagnose_provider.tool_sets[0],
        )

        propose_provider = FakeProvider([
            response(content="No tools needed."),
        ])
        AGENT.run_agent(
            "Diagnose and propose a remediation.",
            mode="auto",
            provider=propose_provider,
        )
        self.assertIn(
            "propose_create_index",
            propose_provider.tool_sets[0],
        )

    def test_non_finite_runtime_deadline_is_rejected(self):
        provider = FakeProvider([])
        with self.assertRaises(ValueError):
            AGENT.run_agent(
                "Investigate.",
                provider=provider,
                deadline_seconds=math.nan,
            )

    def test_final_answer_without_evidence_reference_is_retried(self):
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "health",
                        "get_database_health",
                        {},
                    )
                ],
                finish_reason="tool_calls",
            ),
            response(content="The database is healthy."),
            response(content="The database is healthy [ev-0001]."),
        ])

        result = AGENT.run_agent(
            "Investigate database health.",
            provider=provider,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["llm_turns"], 3)

    def test_missing_evidence_reference_fails_closed_at_limit(self):
        provider = FakeProvider([
            response(
                calls=[
                    tool_call(
                        "health",
                        "get_database_health",
                        {},
                    )
                ],
                finish_reason="tool_calls",
            ),
            response(content="The database is healthy."),
        ])

        result = AGENT.run_agent(
            "Investigate database health.",
            provider=provider,
            max_iterations=2,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            result["stop_reason"],
            "evidence_citation_missing",
        )


if __name__ == "__main__":
    unittest.main()
