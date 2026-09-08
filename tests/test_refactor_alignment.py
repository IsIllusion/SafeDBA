"""Behavioral contracts against d42113b, before the modular refactor.

The frozen implementations are test-only oracles, not alternative runtimes.
Only wall-clock duration is normalized in diagnostic result comparisons.
"""

import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_langgraph_alignment as alignment
from test_agent_loop import AGENT, CALLS, response
from test_langchain_bridge import ScriptedChatModel, call_message
from langchain_core.messages import AIMessage

from agent_prompts import AGENT_INSTRUCTIONS, EXECUTION_REVIEW_INSTRUCTIONS
from agent_tools import _ROUTES
from evaluation_policy import evaluate_case_result, validate_proposal_shape
from identifiers import build_index_name, is_positive_int, is_valid_uuid
from serialization import canonical_json, json_digest
from state_database import open_state_database

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "415486aa-74fc-4791-a140-c364f8d21cc0"
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
baseline = dict(vars(AGENT))
for name in ("refactor_agent_helpers.py", "refactor_agent_loop.py"):
    source = FIXTURES / name
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), baseline)
baseline["TOOL_REGISTRY"] = baseline["_build_tool_registry"]()
before = baseline["run_agent"]
old_grading = {}
source = FIXTURES / "refactor_evaluation_policy.py"
exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), old_grading)


class RefactorAlignmentTests(unittest.TestCase):
    maxDiff = 5000
    replay = alignment.LangGraphAlignmentTests.replay

    def test_public_signature_and_prompt_tool_contracts(self):
        self.assertEqual(inspect.signature(before), inspect.signature(AGENT.run_agent))
        self.assertEqual(
            inspect.signature(baseline["review_execution_result"]),
            inspect.signature(AGENT.review_execution_result),
        )
        contract = json.loads((FIXTURES / "refactor_contract.json").read_text())
        values = {
            "agent_instructions": AGENT_INSTRUCTIONS,
            "review_instructions": EXECUTION_REVIEW_INSTRUCTIONS,
            "tools": json.dumps(AGENT.TOOLS, sort_keys=True, separators=(",", ":")),
            "capabilities": json.dumps(
                {
                    name: [category, risk.value, freshness]
                    for name, (
                        category,
                        risk,
                        freshness,
                    ) in AGENT._TOOL_CAPABILITIES.items()
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for name, value in values.items():
            with self.subTest(contract=name):
                self.assertEqual(
                    hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    contract[name + "_sha256"],
                )
        self.assertEqual(
            baseline["TOOL_REGISTRY"].to_chat_completions_tools(),
            AGENT.TOOL_REGISTRY.to_chat_completions_tools(),
        )

    def test_every_builtin_dispatch_keeps_argument_shape_and_order(self):
        values = {
            "query": "SELECT 1",
            "original_query": "SELECT 1",
            "rewritten_query": "SELECT 2",
            "table": "orders",
            "table_name": "orders",
            "column": "customer_id",
            "column_name": "customer_id",
            "columns": ["customer_id"],
            "reason": "Observed evidence",
            "confidence": 0.9,
            "blocked_pid": 1,
            "blocker_pid": 2,
            "blocker_backend_start": "2026-09-08T00:00:00Z",
            "blocker_xact_start": "2026-09-08T00:01:00Z",
        }
        bindings = {route[0] for route in _ROUTES.values()} | {
            "get_query_plan",
            "analyze_query_plan",
            "detect_non_sargable_predicates",
            "detect_cardinality_anomalies",
        }
        for wire in AGENT.TOOLS:
            name = wire["function"]["name"]
            arguments = {
                key: values[key] for key in wire["function"]["parameters"]["properties"]
            }
            outcomes = []
            for dispatcher in (
                baseline["_dispatch_builtin_tool"],
                AGENT._dispatch_builtin_tool,
            ):
                calls = []

                def recorder(binding):
                    def invoke(*args, **kwargs):
                        calls.append((binding, deepcopy(args), deepcopy(kwargs)))
                        return {"observed": binding}

                    return invoke

                replacements = {binding: recorder(binding) for binding in bindings}
                with patch.multiple(AGENT, **replacements), patch.dict(
                    baseline, replacements
                ):
                    result = dispatcher(name, deepcopy(arguments))
                outcomes.append((result, calls))
            with self.subTest(tool=name):
                self.assertEqual(*outcomes)
        for dispatcher in (
            baseline["_dispatch_builtin_tool"],
            AGENT._dispatch_builtin_tool,
        ):
            with self.assertRaisesRegex(ValueError, "Unknown tool: unknown"):
                dispatcher("unknown", {})

    def test_native_langchain_messages_remain_identical(self):
        scripts = [
            [call_message(), AIMessage(content="Evidence [ev-0001].")],
            [call_message(finish_reason="length")],
            [call_message(args={"unexpected": True}), AIMessage(content="Rejected.")],
        ]
        for script in scripts:
            for i, message in enumerate(script):
                message.id = f"script-{i}"
            results = []
            for runner in (before, AGENT.run_agent):
                CALLS.clear()
                model = ScriptedChatModel(responses=deepcopy(script))
                result = runner(
                    "Inspect",
                    chat_model=model,
                    run_id=RUN_ID,
                    use_memory=False,
                    capture_experience=False,
                )
                requests = [
                    {
                        **request,
                        "messages": [
                            message.model_dump() for message in request["messages"]
                        ],
                    }
                    for request in model.requests
                ]
                results.append(
                    alignment.normalized((result, deepcopy(CALLS), requests))
                )
            self.assertEqual(*results)

    def test_memory_and_experience_persistence_remain_identical(self):
        from agent_memory import SQLiteAgentMemory
        from experience_store import SQLiteExperienceStore

        results = []
        for runner in (before, AGENT.run_agent):
            with tempfile.TemporaryDirectory() as directory:
                memory = SQLiteAgentMemory(
                    Path(directory) / "memory.sqlite3", clock=lambda: NOW
                )
                experience = SQLiteExperienceStore(
                    Path(directory) / "experience.sqlite3", clock=lambda: NOW
                )
                result = runner(
                    "Inspect database health",
                    provider=alignment.ScriptedProvider(
                        [
                            alignment.observation(),
                            response(content="Evidence [ev-0001]."),
                        ]
                    ),
                    memory_store=memory,
                    experience_store=experience,
                    thread_id="tenant-1",
                    session_id="session-1",
                    run_id=RUN_ID,
                )
                turns = memory.get_recent_session(
                    thread_id="tenant-1", session_id="session-1"
                )
                turns = [
                    {k: v for k, v in turn.items() if k != "memory_id"}
                    for turn in turns
                ]
                results.append(
                    alignment.normalized(
                        {
                            "result": result,
                            "run": memory.get_run(RUN_ID),
                            "turns": turns,
                            "experience": experience.get_run(RUN_ID),
                        }
                    )
                )
        self.assertTrue(results[0]["result"]["memory"]["persisted"])
        self.assertTrue(results[0]["result"]["experience_recorded"])
        self.assertEqual(*results)

    def test_persistence_failures_remain_nonfatal_and_reported(self):
        class FailingMemory:
            def get_recent_session(self, **kwargs):
                raise OSError("memory unavailable")

        class FailingExperience:
            def record_run_summary(self, **kwargs):
                raise OSError("experience unavailable")

        results = []
        for runner in (before, AGENT.run_agent):
            result = runner(
                "Inspect",
                provider=alignment.ScriptedProvider([response(content="Done.")]),
                run_id=RUN_ID,
                session_id="session-1",
                memory_store=FailingMemory(),
                experience_store=FailingExperience(),
            )
            results.append(alignment.normalized(result))
        self.assertEqual(*results)
        self.assertEqual(results[0]["status"], "completed")
        self.assertIn(
            "ExperienceCaptureError", [item["type"] for item in results[0]["errors"]]
        )

    def test_deadline_after_model_prevents_tool_execution(self):
        outputs = []
        for runner in (before, AGENT.run_agent):
            now = [0.0]

            class SlowModel(alignment.ScriptedProvider):
                def complete(self, **kwargs):
                    result = super().complete(**kwargs)
                    now[0] = 100.0
                    return result

            clock = SimpleNamespace(monotonic=lambda: now[0])
            CALLS.clear()
            with patch.object(AGENT, "time", clock), patch.dict(
                baseline, {"time": clock}
            ):
                result = runner(
                    "Inspect",
                    provider=SlowModel([alignment.observation()]),
                    run_id=RUN_ID,
                    use_memory=False,
                    capture_experience=False,
                )
            self.assertEqual(result["stop_reason"], "deadline_exceeded")
            self.assertEqual(CALLS, [])
            outputs.append(alignment.normalized(result))
        self.assertEqual(*outputs)

    def test_execution_review_preserves_messages_and_has_no_tools(self):
        class ReviewProvider(alignment.ScriptedProvider):
            def complete(self, *, messages, tools=(), tool_choice=None):
                self.asserted_no_tools = not tools and tool_choice is None
                return super().complete(
                    messages=messages, tools=tools, tool_choice=tool_choice
                )

        outputs = []
        for review in (
            baseline["review_execution_result"],
            AGENT.review_execution_result,
        ):
            provider = ReviewProvider([response(content="No further action.")])
            with patch.object(AGENT, "get_llm_provider", lambda: provider), patch.dict(
                baseline, {"get_llm_provider": lambda: provider}
            ):
                answer = review({"type": "ANALYZE_TABLE"}, {"status": "VERIFIED"})
            outputs.append((answer, provider.message_batches, provider.tool_sets))
            self.assertTrue(provider.asserted_no_tools)
        self.assertEqual(*outputs)


def replay_test(script, options):
    def test(self):
        self.assertEqual(
            self.replay(before, script, options),
            self.replay(AGENT.run_agent, script, options),
        )

    return test


for name, (script, options) in alignment.cases().items():
    setattr(RefactorAlignmentTests, "test_replay_" + name, replay_test(script, options))


class SharedPrimitiveContracts(unittest.TestCase):
    def test_canonical_json_and_digest_match_existing_encoding(self):
        for value in (
            {"中文": [None, False, 1, -1, 0.125], "a": "é"},
            {},
            [],
            "text",
            -0.0,
        ):
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            self.assertEqual(canonical_json(value), encoded)
            self.assertEqual(
                json_digest(value), hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            )
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ValueError):
                canonical_json({"value": value})
        with self.assertRaises(TypeError):
            canonical_json({"value": object()})

    def test_identifier_edge_cases(self):
        for value in (1, 10**50):
            self.assertTrue(is_positive_int(value))
        for value in (True, False, 0, -1, 1.0, "1", None):
            self.assertFalse(is_positive_int(value))
        for value in (RUN_ID, RUN_ID.replace("-", ""), "{" + RUN_ID + "}"):
            self.assertTrue(is_valid_uuid(value))
        for value in (None, 1, True, "invalid", " " + RUN_ID):
            self.assertFalse(is_valid_uuid(value))

    def test_index_names_match_frozen_evaluator_at_utf8_boundaries(self):
        for table, column in (
            ("orders", "customer_id"),
            ("a" * 57, "b"),
            ("a" * 58, "b"),
            ("表" * 30, "字段" * 20),
            ("😀" * 20, "x"),
        ):
            index = build_index_name(table, column)
            self.assertLessEqual(len(index.encode("utf-8")), 63)
            proposal = {
                "type": "CREATE_INDEX",
                "table": table,
                "column": column,
                "index_name": index,
                "query": "SELECT 1",
                "reason": "Evidence",
                "confidence": 0.9,
                "risk": "MEDIUM",
                "evidence_refs": ["ev-0001"],
            }
            for candidate in (proposal, {**proposal, "index_name": "wrong_name"}):
                self.assertEqual(
                    old_grading["validate_proposal_shape"](candidate),
                    validate_proposal_shape(candidate),
                )

    def test_state_connection_profile_is_unchanged(self):
        connection = open_state_database(":memory:")
        self.addCleanup(connection.close)
        self.assertIsNone(connection.isolation_level)
        self.assertIs(connection.row_factory, sqlite3.Row)
        for pragma, expected in (
            ("foreign_keys", 1),
            ("busy_timeout", 5000),
            ("synchronous", 2),
        ):
            self.assertEqual(
                connection.execute(f"PRAGMA {pragma}").fetchone()[0], expected
            )

    def test_evaluation_results_match_frozen_rules(self):
        for path in sorted((ROOT / "benchmarks/cases").glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8-sig"))
            for script, options in alignment.cases().values():
                provider = alignment.ScriptedProvider(deepcopy(script))
                result = AGENT.run_agent(
                    "Inspect",
                    provider=provider,
                    run_id=RUN_ID,
                    use_memory=False,
                    capture_experience=False,
                    **options,
                )
                self.assertEqual(
                    old_grading["evaluate_case_result"](case, result),
                    evaluate_case_result(case, result),
                )

    def test_grading_and_core_do_not_import_database_or_application_wiring(self):
        for name in (
            "evaluation_policy",
            "agent_context",
            "agent_runtime",
            "agent_tool_execution",
            "agent_tools",
            "agent_tool_catalog",
            "serialization",
            "identifiers",
        ):
            tree = ast.parse(
                (ROOT / "src" / (name + ".py")).read_text(encoding="utf-8-sig")
            )
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add((node.module or "").split(".")[0])
            self.assertFalse(
                imports & {"config", "db_tools", "psycopg", "agent", "llm_provider"},
                name,
            )


if __name__ == "__main__":
    unittest.main()
