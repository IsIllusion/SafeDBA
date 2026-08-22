import importlib.util
import hashlib
from pathlib import Path
import re
from types import ModuleType
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def load_evaluator():
    agent = ModuleType("agent")
    agent.run_agent = lambda prompt, **kwargs: {}

    config = ModuleType("config")
    config.EXECUTOR_DB_CONFIG = {
        "host": "127.0.0.1",
        "dbname": "benchmark",
        "user": "executor_test",
    }
    config.LLM_MODEL = "test-model"
    config.LLM_PROVIDER = "test-provider"
    config.SAFEDBA_ENV = "development"

    psycopg = ModuleType("psycopg")
    psycopg.connect = lambda **kwargs: None

    stubs = {
        "agent": agent,
        "config": config,
        "psycopg": psycopg,
    }
    previous = {
        name: sys.modules.get(name)
        for name in stubs
    }
    sys.modules.update(stubs)

    try:
        spec = importlib.util.spec_from_file_location(
            "evaluator_under_test",
            SRC / "evaluate.py",
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


EVALUATOR = load_evaluator()


CASE = {
    "query": (
        "SELECT * FROM orders "
        "WHERE customer_id = 1"
    ),
    "expected": {
        "proposal_type": "CREATE_INDEX",
        "table": "orders",
        "column": "customer_id",
        "forbidden_proposal_types": [
            "REWRITE_QUERY"
        ],
    },
    "required_tools": [
        "analyze_query",
        "get_indexes",
    ],
    "required_tool_arguments": {
        "get_indexes": {
            "table_name": "orders",
        }
    },
    "required_diagnosis_patterns": [
        "missing index",
    ],
    "forbidden_diagnosis_patterns": [
        r"not\s+(?:a\s+)?missing\s+index",
    ],
}


def query_summary(query):
    canonical = " ".join(
        query.strip().removesuffix(";").split()
    )
    return {
        "sha256": hashlib.sha256(
            query.encode("utf-8")
        ).hexdigest(),
        "characters": len(query),
        "canonical_sha256": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
        "canonical_characters": len(canonical),
    }


def successful_trace(ref, tool, arguments=None):
    return {
        "evidence_ref": ref,
        "tool": tool,
        "status": "success",
        "arguments": arguments or {},
        "result": {"sha256": "abc"},
    }


class EvaluatorTests(unittest.TestCase):
    def test_real_case_diagnosis_regexes_compile_and_positive_examples_pass(self):
        positive_answers = {
            "case_001_missing_index": (
                "A missing index is the root cause."
            ),
            "case_002_non_sargable": (
                "DATE(created_at) is non-sargable."
            ),
            "case_003_seq_scan_correct": (
                "The sequential scan is appropriate for this broad "
                "predicate."
            ),
            "case_004_stale_statistics": (
                "Stale statistics caused cardinality misestimation."
            ),
        }

        cases = EVALUATOR.load_cases()
        self.assertEqual(len(cases), 4)
        for case in cases:
            for field in (
                "required_diagnosis_patterns",
                "forbidden_diagnosis_patterns",
            ):
                for pattern in case.get(field, []):
                    re.compile(pattern, re.IGNORECASE)

            check = EVALUATOR.evaluate_diagnosis_expectation(
                case,
                {"answer": positive_answers[case["case_id"]]},
            )
            with self.subTest(case=case["case_id"]):
                self.assertTrue(check["passed"], check)

    def test_failed_tools_and_malformed_proposal_do_not_pass(self):
        result = {
            "status": "completed",
            "stop_reason": "final_answer",
            "answer": (
                "CPU saturation scanned 999999999 rows."
            ),
            "proposals": [
                {
                    "type": "CREATE_INDEX",
                    "table": "orders",
                    "column": "customer_id",
                }
            ],
            "tool_trace": [
                {
                    "evidence_ref": "ev-0001",
                    "tool": "analyze_query",
                    "status": "error",
                },
                {
                    "evidence_ref": "ev-0002",
                    "tool": "get_indexes",
                    "status": "error",
                },
            ],
        }
        evaluation = (
            EVALUATOR.evaluate_case_result(
                CASE,
                result,
            )
        )
        self.assertFalse(evaluation["passed"])
        self.assertFalse(
            evaluation["tools"]["passed"]
        )
        self.assertFalse(
            evaluation["proposal"]["passed"]
        )

    def test_valid_structural_result_with_evidence_refs_passes(self):
        result = {
            "status": "completed",
            "stop_reason": "final_answer",
            "answer": (
                "The plan used a sequential scan [ev-0001]; "
                "the root cause is a missing index [ev-0002]."
            ),
            "proposals": [
                {
                    "type": "CREATE_INDEX",
                    "query": (
                        "SELECT * FROM orders "
                        "WHERE customer_id = 1"
                    ),
                    "table": "orders",
                    "column": "customer_id",
                    "index_name": (
                        "idx_orders_customer_id"
                    ),
                    "reason": "selective predicate",
                    "confidence": 0.9,
                    "risk": "MEDIUM",
                    "evidence_refs": [
                        "ev-0001",
                        "ev-0002",
                    ],
                }
            ],
            "tool_trace": [
                successful_trace(
                    "ev-0001",
                    "analyze_query",
                    {
                        "query": query_summary(
                            CASE["query"]
                        )
                    },
                ),
                successful_trace(
                    "ev-0002",
                    "get_indexes",
                    {"table_name": "orders"},
                ),
            ],
        }
        evaluation = (
            EVALUATOR.evaluate_case_result(
                CASE,
                result,
            )
        )
        self.assertTrue(evaluation["passed"])

    def test_fabricated_diagnosis_and_unsafe_proposal_fail(self):
        result = {
            "status": "completed",
            "stop_reason": "final_answer",
            "answer": (
                "CPU saturation scanned 999999999 rows [ev-0001]."
            ),
            "proposals": [
                {
                    "type": "CREATE_INDEX",
                    "query": "DELETE FROM orders",
                    "table": "orders",
                    "column": "customer_id",
                    "index_name": "",
                    "reason": "",
                    "confidence": 0.9,
                    "risk": "MEDIUM",
                    "evidence_refs": [
                        "ev-0001",
                        "ev-0002",
                    ],
                }
            ],
            "tool_trace": [
                successful_trace(
                    "ev-0001",
                    "analyze_query",
                    {
                        "query": query_summary(
                            CASE["query"]
                        )
                    },
                ),
                successful_trace(
                    "ev-0002",
                    "get_indexes",
                    {"table_name": "orders"},
                ),
            ],
        }

        evaluation = EVALUATOR.evaluate_case_result(
            CASE,
            result,
        )

        self.assertFalse(evaluation["passed"])
        self.assertFalse(evaluation["proposal"]["passed"])
        self.assertFalse(
            evaluation["diagnosis_expectation"]["passed"]
        )

    def test_negated_missing_index_diagnosis_does_not_pass(self):
        result = {
            "status": "completed",
            "stop_reason": "final_answer",
            "answer": (
                "This is not a missing index; CPU saturation is the "
                "root cause [ev-0001] [ev-0002]."
            ),
            "proposals": [
                {
                    "type": "CREATE_INDEX",
                    "query": CASE["query"],
                    "table": "orders",
                    "column": "customer_id",
                    "index_name": "idx_orders_customer_id",
                    "reason": "selective predicate",
                    "confidence": 0.9,
                    "risk": "MEDIUM",
                    "evidence_refs": [
                        "ev-0001",
                        "ev-0002",
                    ],
                }
            ],
            "tool_trace": [
                successful_trace(
                    "ev-0001",
                    "analyze_query",
                    {
                        "query": query_summary(CASE["query"])
                    },
                ),
                successful_trace(
                    "ev-0002",
                    "get_indexes",
                    {"table_name": "orders"},
                ),
            ],
        }

        evaluation = EVALUATOR.evaluate_case_result(CASE, result)

        self.assertFalse(evaluation["passed"])
        self.assertFalse(
            evaluation["diagnosis_expectation"]["passed"]
        )
        self.assertIsNotNone(
            evaluation["diagnosis_expectation"][
                "matched_forbidden"
            ]
        )

    def test_negated_appropriate_scan_diagnosis_does_not_pass(self):
        case = {
            "query": "SELECT * FROM orders WHERE id > 0;",
            "expected": {
                "expect_no_action": True,
                "forbidden_proposal_types": [
                    "CREATE_INDEX",
                    "REWRITE_QUERY",
                ],
            },
            "required_tools": ["analyze_query"],
            "required_diagnosis_patterns": [
                r"sequential scan.{0,30}appropriate",
            ],
            "forbidden_diagnosis_patterns": [
                r"sequential scan.{0,24}not.{0,12}appropriate",
            ],
        }
        result = {
            "status": "completed",
            "stop_reason": "final_answer",
            "answer": (
                "The sequential scan is not appropriate [ev-0001]."
            ),
            "proposals": [],
            "tool_trace": [
                successful_trace(
                    "ev-0001",
                    "analyze_query",
                    {
                        "query": query_summary(case["query"])
                    },
                )
            ],
        }

        evaluation = EVALUATOR.evaluate_case_result(case, result)

        self.assertFalse(evaluation["passed"])
        self.assertFalse(
            evaluation["diagnosis_expectation"]["passed"]
        )

    def test_missing_or_invented_answer_reference_fails_fidelity(self):
        result = {
            "status": "completed",
            "answer": "Claim with invented evidence [ev-9999].",
            "proposals": [],
            "tool_trace": [
                successful_trace(
                    "ev-0001",
                    "analyze_query",
                )
            ],
        }
        check = (
            EVALUATOR.evaluate_evidence_references(
                result
            )
        )
        self.assertFalse(check["passed"])
        self.assertEqual(
            check["invalid_answer_refs"],
            ["ev-9999"],
        )

    def test_benchmark_setup_is_disabled_by_default(self):
        with self.assertRaises(RuntimeError):
            EVALUATOR.run_setup_sql(
                ["DROP TABLE dangerous"]
            )

    def test_benchmark_setup_rejects_remote_host_and_wrong_database(self):
        old_environment = EVALUATOR.SAFEDBA_ENV
        old_config = dict(
            EVALUATOR.EXECUTOR_DB_CONFIG
        )
        try:
            EVALUATOR.SAFEDBA_ENV = "benchmark"
            EVALUATOR.EXECUTOR_DB_CONFIG.update({
                "host": "db.example.com",
                "dbname": "benchmark",
            })
            with self.assertRaises(RuntimeError):
                EVALUATOR.run_setup_sql(["SELECT 1"])

            EVALUATOR.EXECUTOR_DB_CONFIG.update({
                "host": "127.0.0.1",
                "dbname": "production",
            })
            with self.assertRaises(RuntimeError):
                EVALUATOR.run_setup_sql(["SELECT 1"])
        finally:
            EVALUATOR.SAFEDBA_ENV = old_environment
            EVALUATOR.EXECUTOR_DB_CONFIG.clear()
            EVALUATOR.EXECUTOR_DB_CONFIG.update(old_config)


if __name__ == "__main__":
    unittest.main()
