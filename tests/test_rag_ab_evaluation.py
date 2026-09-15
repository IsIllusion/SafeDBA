"""Offline checks of the paired evaluator; no model/DB access or paid calls."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rag_ab_evaluate import (
    ABBudgetExceeded,
    ABProvider,
    MAX_INPUT_CHARS,
    grade_case,
    load_fixture,
    run_case,
    summarize,
)
from knowledge_base import FileKnowledgeBase, parse_timestamp
from openai.types.chat import ChatCompletion


def message(content=None, calls=None):
    return ChatCompletion.model_validate(
        {
            "id": "offline-only",
            "object": "chat.completion",
            "created": 1,
            "model": "offline-test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if calls else "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": calls,
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


def call(name, arguments, number=1):
    return {
        "id": "test-call-" + str(number),
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class QueueProvider:
    model = "offline-script-not-an-intelligence-evaluation"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.responses)

    @staticmethod
    def assistant_message_to_dict(value):
        return value.model_dump(exclude_none=True)


class RAGABEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.fixture, self.bundle, self.scope = load_fixture(
            ROOT / "benchmarks/retrieval/agent_ab_cases.json"
        )
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bundle.json"
        self.path.write_text(
            json.dumps(self.bundle, ensure_ascii=False), encoding="utf-8"
        )
        self.retriever = FileKnowledgeBase(
            self.path, clock=lambda: parse_timestamp(self.fixture["as_of"])
        )

    def test_fixture_labels_are_not_in_model_questions(self):
        self.assertEqual(len(self.fixture["cases"]), 12)
        for identifier in (
            "OPS-JADE-47",
            "POOL-284",
            "MAP-926",
            "LAG-619",
            "VAC-583",
            "DISK-482",
        ):
            self.assertNotIn(
                identifier, " ".join(case["question"] for case in self.fixture["cases"])
            )

    def test_real_graph_paired_flow_and_source_grounding(self):
        case = self.fixture["cases"][0]
        hit = next(
            item
            for item in self.retriever.search("mira_settlement", scope=self.scope)
            if item["document_id"] == "mira-lock"
        )
        off = QueueProvider(
            [
                message(calls=[call("get_lock_waits", {})]),
                message("7101 阻塞 7102 [ev-0001]。内部队列和分类未知。"),
            ]
        )
        on = QueueProvider(
            [
                message(
                    calls=[
                        call("get_lock_waits", {}),
                        call("search_knowledge", {"query": "mira_settlement"}, 2),
                    ]
                ),
                message(
                    f"7101 阻塞 7102 [ev-0001]。OPS-JADE-47，LOCK-731 [{hit['ref']}]。"
                ),
            ]
        )
        with patch.dict(
            sys.modules, {"db_tools": None, "psycopg": None, "actions": None}
        ):
            a, a_sources = run_case(
                self.fixture, self.path, case=case, enabled=False, provider=off
            )
            b, b_sources = run_case(
                self.fixture, self.path, case=case, enabled=True, provider=on
            )
        ga = grade_case(case, a, a_sources, enabled=False)
        gb = grade_case(case, b, b_sources, enabled=True)
        self.assertEqual(a["status"], "completed")
        self.assertTrue(ga["abstention_detected"])
        self.assertFalse(ga["passed"])
        self.assertTrue(gb["passed"], gb)
        self.assertTrue(gb["grounded_passed"], gb)
        off_tools = {
            tool["function"]["name"]: tool for tool in off.requests[0]["tools"]
        }
        on_tools = {tool["function"]["name"]: tool for tool in on.requests[0]["tools"]}
        self.assertEqual(set(on_tools) - set(off_tools), {"search_knowledge"})
        self.assertEqual(
            off_tools,
            {
                key: value
                for key, value in on_tools.items()
                if key != "search_knowledge"
            },
        )
        self.assertEqual(off.requests[0]["messages"][1], on.requests[0]["messages"][1])
        self.assertFalse(b["memory"]["enabled"])
        self.assertFalse(b["experience_recorded"])
        self.assertNotIn("knowledge", a)
        self.assertNotIn(
            "evidence_ref",
            next(row for row in b["tool_trace"] if row["tool"] == "search_knowledge"),
        )

    def test_filtered_documents_never_reach_agent_retriever(self):
        for query in ("nova_archive", "phoenix_export"):
            hits = self.retriever.search(query, scope=self.scope)
            self.assertEqual(hits, [])

    def test_grader_rejects_fake_refs_missing_facts_and_wrong_source(self):
        case = self.fixture["cases"][4]
        source = {"ref": "kb-test", "document_id": "mira-status"}
        result = {
            "status": "completed",
            "stop_reason": "final_answer",
            "answer": "人工撤销，不计入 MAP-926 [kb-test]",
            "tool_trace": [
                {
                    "tool": "search_knowledge",
                    "status": "success",
                    "knowledge_refs": ["kb-test"],
                }
            ],
            "proposals": [],
        }
        self.assertTrue(
            grade_case(case, result, [source], enabled=True)["grounded_passed"]
        )
        for answer in (
            "人工撤销，不计入 MAP-926 [kb-fake]",
            "人工撤销，不计入 [kb-test]",
            "人工撤销，不计入 MAP-926 [kb-test] [ev-9999]",
        ):
            self.assertFalse(
                grade_case(case, {**result, "answer": answer}, [source], enabled=True)[
                    "passed"
                ]
            )
        wrong = grade_case(
            case, result, [{**source, "document_id": "unrelated"}], enabled=True
        )
        self.assertTrue(wrong["passed"])
        self.assertFalse(wrong["grounded_passed"])

    def test_reference_document_cannot_enable_runtime_execution(self):
        case = self.fixture["cases"][-1]
        provider = QueueProvider(
            [
                message(
                    calls=[
                        call(
                            "get_query_plan",
                            {"query": "DELETE FROM public.mira_orders"},
                        )
                    ]
                ),
                message("无法执行；仅做诊断。"),
            ]
        )
        result, sources = run_case(
            self.fixture, self.path, case=case, enabled=True, provider=provider
        )
        self.assertEqual(result["evaluation_fixture"]["observation_calls"], [])
        self.assertFalse(
            grade_case(case, result, sources, enabled=True)["checks"][
                "no_proposal_or_runtime_attempt"
            ]
        )

    def test_unknown_sql_cannot_receive_canned_plan(self):
        case = self.fixture["cases"][7]
        provider = QueueProvider(
            [
                message(
                    calls=[
                        call(
                            "get_estimated_query_plan",
                            {"query": "SELECT * FROM unrelated"},
                        )
                    ]
                ),
                message("无法确认。"),
            ]
        )
        result, _ = run_case(
            self.fixture, self.path, case=case, enabled=False, provider=provider
        )
        self.assertEqual(result["evaluation_fixture"]["observation_calls"], [])
        self.assertEqual(result["tool_trace"][0]["status"], "error")

    def test_budget_counts_failed_attempt_and_never_retries(self):
        raw = Mock(model="offline")
        raw.complete.side_effect = RuntimeError("do-not-log-secret")
        provider = ABProvider(raw, max_requests=2)
        with self.assertRaises(RuntimeError):
            provider.complete(messages=[])
        with self.assertRaises(ABBudgetExceeded):
            provider.complete(messages=[])
        self.assertEqual(raw.complete.call_count, 1)
        self.assertNotIn("do-not-log-secret", str(provider.calls))

    def test_budget_blocks_oversized_input_before_provider(self):
        raw = Mock(model="offline")
        provider = ABProvider(raw, max_requests=1)
        with self.assertRaises(ABBudgetExceeded):
            provider.complete(messages=["x" * MAX_INPUT_CHARS])
        raw.complete.assert_not_called()

    def test_budget_total_input_and_request_ceiling(self):
        raw = QueueProvider([message("test"), message("test")])
        provider = ABProvider(raw, max_requests=1)
        provider.complete(messages=[])
        with self.assertRaises(ABBudgetExceeded):
            provider.complete(messages=[])
        limited = ABProvider(raw, max_requests=2, max_total_input_chars=1)
        with self.assertRaises(ABBudgetExceeded):
            limited.complete(messages=[])

    def test_launcher_only_forwards_primary_model_settings(self):
        spec = importlib.util.spec_from_file_location(
            "rag_ab_launcher_test", ROOT / "scripts/run_rag_ab.py"
        )
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        dotenv = {
            "DEEPSEEK_API_KEY": "synthetic-key",
            "SAFEDBA_LLM_MODEL": "test-model",
            "SAFEDBA_DB_HOST": "private-db",
            "SAFEDBA_TERMINATOR_DB_PASSWORD": "private-password",
            "LANGSMITH_API_KEY": "trace-key",
            "SAFEDBA_LLM_FALLBACK_API_KEY": "fallback-key",
        }
        with patch.dict(
            os.environ,
            {"PGPASSWORD": "ambient", "HTTPS_PROXY": "private-proxy"},
            clear=True,
        ), patch("dotenv.dotenv_values", return_value=dotenv):
            env = launcher.isolated_environment()
        self.assertEqual(env["DEEPSEEK_API_KEY"], "synthetic-key")
        self.assertEqual(env["SAFEDBA_SKIP_DOTENV"], "1")
        for key in (
            "PGPASSWORD",
            "HTTPS_PROXY",
            "SAFEDBA_DB_HOST",
            "SAFEDBA_TERMINATOR_DB_PASSWORD",
            "LANGSMITH_API_KEY",
            "SAFEDBA_LLM_FALLBACK_API_KEY",
        ):
            self.assertNotIn(key, env)

    def test_summary_counts_improvement_regression_and_incomplete_pairs(self):
        def row(case_id, arm, passed):
            return {
                "id": case_id,
                "repeat": 1,
                "arm": arm,
                "group": "control",
                "source_ids": [],
                "request_count": 2,
                "result": {"usage": {"elapsed_ms": 10, "total_tokens": 20}},
                "grade": {
                    "passed": passed,
                    "grounded_passed": passed,
                    "fact_hits": 1,
                    "fact_count": 2,
                    "checks": {"completed": True},
                },
            }

        summary = summarize(
            [
                row("a", "off", False),
                row("a", "on", True),
                row("b", "off", True),
                row("b", "on", False),
                row("c", "off", True),
            ]
        )
        self.assertEqual(
            summary["pairs"], {"improved": 1, "regressed": 1, "incomplete": 1}
        )
        self.assertEqual(summary["off"]["runs"], 3)


if __name__ == "__main__":
    unittest.main()
