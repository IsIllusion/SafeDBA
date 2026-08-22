import json
import sys
from pathlib import Path
import unittest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from agent_policy import (  # noqa: E402
    EvidenceLedger,
    canonical_call_key,
    is_diagnosis_only_request,
    is_explicit_proposal_request,
    serialize_tool_output,
    validate_tool_arguments,
)


class AgentPolicyTests(unittest.TestCase):
    def test_detects_diagnosis_only_requests_in_english_and_chinese(self):
        for message in [
            "Diagnosis only; do not execute anything.",
            "Please perform a read-only diagnosis.",
            "仅诊断，不要执行任何修改。",
        ]:
            with self.subTest(message=message):
                self.assertTrue(
                    is_diagnosis_only_request(message)
                )

        self.assertFalse(
            is_diagnosis_only_request(
                "Diagnose and propose the safest remediation."
            )
        )

    def test_canonical_call_key_ignores_query_whitespace(self):
        first = canonical_call_key(
            "analyze_query",
            {"query": "SELECT *\nFROM orders;"},
        )
        second = canonical_call_key(
            "analyze_query",
            {"query": " SELECT * FROM orders "},
        )
        self.assertEqual(first, second)

    def test_tool_argument_schema_rejects_bool_as_number_and_extras(self):
        parameters = {
            "type": "object",
            "properties": {
                "confidence": {"type": "number"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["confidence", "columns"],
        }
        errors = validate_tool_arguments(
            {
                "confidence": True,
                "columns": ["ok", 7],
                "unexpected": "value",
            },
            parameters,
        )
        self.assertEqual(len(errors), 3)

    def test_tool_argument_schema_rejects_nan_and_blank_required_values(self):
        parameters = {
            "type": "object",
            "properties": {
                "confidence": {"type": "number"},
                "column": {"type": "string"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["confidence", "column", "columns"],
        }

        errors = validate_tool_arguments(
            {
                "confidence": float("nan"),
                "column": "   ",
                "columns": ["ok", ""],
            },
            parameters,
        )

        self.assertGreaterEqual(len(errors), 3)

    def test_create_index_requires_bound_plan_and_index_evidence(self):
        ledger = EvidenceLedger()
        arguments = {
            "query": "SELECT * FROM orders WHERE customer_id = 1",
            "table": "orders",
            "column": "customer_id",
            "reason": "selective predicate",
            "confidence": 0.9,
        }

        errors, refs = ledger.proposal_authorization(
            "propose_create_index",
            arguments,
            proposals_allowed=True,
            allowed_action_types={"CREATE_INDEX"},
        )
        self.assertEqual(len(errors), 2)
        self.assertEqual(refs, [])

        plan = ledger.add(
            tool="analyze_query",
            arguments={"query": arguments["query"]},
            result={
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
            },
            status="success",
            duration_ms=1.0,
        )
        indexes = ledger.add(
            tool="get_indexes",
            arguments={"table_name": "orders"},
            result=[],
            status="success",
            duration_ms=1.0,
        )

        errors, refs = ledger.proposal_authorization(
            "propose_create_index",
            arguments,
            proposals_allowed=True,
            allowed_action_types={"CREATE_INDEX"},
        )
        self.assertEqual(errors, [])
        self.assertEqual(refs, [plan.ref, indexes.ref])

    def test_literal_or_cast_name_cannot_authorize_wrong_index_column(self):
        ledger = EvidenceLedger()
        query = "SELECT * FROM orders WHERE status = 'customer_id'"
        ledger.add(
            tool="analyze_query",
            arguments={"query": query},
            result={
                "scan_nodes": [
                    {
                        "scan_type": "Sequential Scan",
                        "table": "orders",
                        "filter": "(status = 'customer_id'::text)",
                        "predicate_columns": ["status"],
                        "rows_examined": 100_000,
                        "selectivity": 0.001,
                    }
                ]
            },
            status="success",
            duration_ms=1.0,
        )
        ledger.add(
            tool="get_indexes",
            arguments={"table_name": "orders"},
            result=[],
            status="success",
            duration_ms=1.0,
        )

        errors, refs = ledger.proposal_authorization(
            "propose_create_index",
            {
                "query": query,
                "table": "orders",
                "column": "customer_id",
                "reason": "wrong column",
                "confidence": 0.9,
            },
            proposals_allowed=True,
            allowed_action_types={"CREATE_INDEX"},
        )

        self.assertTrue(errors)
        self.assertNotIn("ev-0001", refs)

    def test_terminate_requires_same_current_pid_relationship(self):
        ledger = EvidenceLedger()
        evidence = ledger.add(
            tool="get_lock_waits",
            arguments={},
            result=[
                {
                    "blocked_pid": 101,
                    "blocker_pid": 202,
                    "blocked_wait_event_type": "Lock",
                    "blocker_state": "idle in transaction",
                    "blocker_backend_start": "backend-start",
                    "blocker_xact_start": "xact-start",
                }
            ],
            status="success",
            duration_ms=1.0,
        )

        errors, refs = ledger.proposal_authorization(
            "propose_terminate_backend",
            {
                "blocked_pid": 101,
                "blocker_pid": 202,
                "blocker_backend_start": "backend-start",
                "blocker_xact_start": "xact-start",
                "reason": "idle blocker",
                "confidence": 0.9,
            },
            proposals_allowed=True,
            allowed_action_types={"TERMINATE_BACKEND"},
        )
        self.assertEqual(errors, [])
        self.assertEqual(refs, [evidence.ref])

    def test_latest_lock_snapshot_invalidates_old_relationship(self):
        ledger = EvidenceLedger()
        ledger.add(
            tool="get_lock_waits",
            arguments={},
            result=[
                {
                    "blocked_pid": 101,
                    "blocker_pid": 202,
                    "blocked_wait_event_type": "Lock",
                    "blocker_state": "idle in transaction",
                    "blocker_backend_start": "backend-start",
                    "blocker_xact_start": "xact-start",
                }
            ],
            status="success",
            duration_ms=1.0,
        )
        latest = ledger.add(
            tool="get_lock_waits",
            arguments={},
            result=[],
            status="success",
            duration_ms=1.0,
        )

        errors, refs = ledger.proposal_authorization(
            "propose_terminate_backend",
            {
                "blocked_pid": 101,
                "blocker_pid": 202,
                "blocker_backend_start": "backend-start",
                "blocker_xact_start": "xact-start",
                "reason": "idle blocker",
                "confidence": 0.9,
            },
            proposals_allowed=True,
            allowed_action_types={"TERMINATE_BACKEND"},
        )

        self.assertTrue(errors)
        self.assertEqual(refs, [])
        self.assertTrue(
            latest.recorded_monotonic > 0
        )

    def test_failed_lock_refresh_cannot_fall_back_to_old_success(self):
        ledger = EvidenceLedger()
        ledger.add(
            tool="get_lock_waits",
            arguments={},
            result=[
                {
                    "blocked_pid": 101,
                    "blocker_pid": 202,
                    "blocked_wait_event_type": "Lock",
                    "blocker_state": "idle in transaction",
                    "blocker_backend_start": "backend-start",
                    "blocker_xact_start": "xact-start",
                }
            ],
            status="success",
            duration_ms=1.0,
        )
        ledger.add(
            tool="get_lock_waits",
            arguments={},
            result={"error": "refresh failed"},
            status="error",
            duration_ms=1.0,
        )

        errors, refs = ledger.proposal_authorization(
            "propose_terminate_backend",
            {
                "blocked_pid": 101,
                "blocker_pid": 202,
                "blocker_backend_start": "backend-start",
                "blocker_xact_start": "xact-start",
                "reason": "idle blocker",
                "confidence": 0.9,
            },
            proposals_allowed=True,
            allowed_action_types={"TERMINATE_BACKEND"},
        )

        self.assertTrue(errors)
        self.assertEqual(refs, [])

    def test_same_turn_lock_refresh_vetoes_old_snapshot(self):
        ledger = EvidenceLedger()
        ledger.add(
            tool="get_lock_waits",
            arguments={},
            result=[
                {
                    "blocked_pid": 101,
                    "blocker_pid": 202,
                    "blocked_wait_event_type": "Lock",
                    "blocker_state": "idle in transaction",
                    "blocker_backend_start": "backend-start",
                    "blocker_xact_start": "xact-start",
                }
            ],
            status="success",
            duration_ms=1.0,
        )
        cutoff = len(ledger.records)
        ledger.add(
            tool="get_lock_waits",
            arguments={},
            result=[],
            status="success",
            duration_ms=1.0,
        )

        errors, refs = ledger.proposal_authorization(
            "propose_terminate_backend",
            {
                "blocked_pid": 101,
                "blocker_pid": 202,
                "blocker_backend_start": "backend-start",
                "blocker_xact_start": "xact-start",
                "reason": "idle blocker",
                "confidence": 0.9,
            },
            proposals_allowed=True,
            allowed_action_types={"TERMINATE_BACKEND"},
            evidence_cutoff=cutoff,
        )

        self.assertTrue(errors)
        self.assertEqual(refs, [])

    def test_policy_rejects_blank_identifier_and_non_finite_confidence(self):
        ledger = EvidenceLedger()
        errors, refs = ledger.proposal_authorization(
            "propose_create_index",
            {
                "query": "SELECT * FROM orders",
                "table": "orders",
                "column": "   ",
                "reason": "test",
                "confidence": float("nan"),
            },
            proposals_allowed=True,
            allowed_action_types={"CREATE_INDEX"},
        )

        self.assertGreaterEqual(len(errors), 2)
        self.assertEqual(refs, [])

    def test_rewrite_requires_type_evidence_and_exact_transform(self):
        ledger = EvidenceLedger()
        original = (
            "SELECT * FROM orders "
            "WHERE DATE(created_at) = DATE '2026-05-18'"
        )
        rewritten = (
            "SELECT * FROM orders "
            "WHERE created_at >= DATE '2026-05-18' "
            "AND created_at < DATE '2026-05-18' "
            "+ INTERVAL '1 day'"
        )
        plan = ledger.add(
            tool="analyze_query",
            arguments={"query": original},
            result={
                "non_sargable_findings": [
                    {
                        "table": "orders",
                        "column": "created_at",
                        "function": "DATE",
                    }
                ]
            },
            status="success",
            duration_ms=1.0,
        )
        arguments = {
            "original_query": original,
            "rewritten_query": rewritten,
            "reason": "restore index use",
            "confidence": 0.9,
        }

        errors, _ = ledger.proposal_authorization(
            "propose_query_rewrite",
            arguments,
            proposals_allowed=True,
            allowed_action_types={"REWRITE_QUERY"},
        )
        self.assertTrue(errors)

        info = ledger.add(
            tool="get_column_info",
            arguments={
                "table_name": "orders",
                "column_name": "created_at",
            },
            result={
                "data_type": "timestamp without time zone",
            },
            status="success",
            duration_ms=1.0,
        )
        errors, refs = ledger.proposal_authorization(
            "propose_query_rewrite",
            arguments,
            proposals_allowed=True,
            allowed_action_types={"REWRITE_QUERY"},
        )
        self.assertEqual(errors, [])
        self.assertEqual(refs, [plan.ref, info.ref])

    def test_analyze_rejects_empty_columns_and_unrelated_table(self):
        ledger = EvidenceLedger()
        ledger.add(
            tool="analyze_query",
            arguments={"query": "SELECT 1"},
            result={
                "cardinality_findings": [
                    {"type": "SEVERE_CARDINALITY_ERROR"}
                ],
                "scan_nodes": [],
            },
            status="success",
            duration_ms=1.0,
        )
        errors, refs = ledger.proposal_authorization(
            "propose_analyze_table",
            {
                "query": "SELECT 1",
                "table": "customers",
                "columns": [],
                "reason": "refresh",
                "confidence": 0.9,
            },
            proposals_allowed=True,
            allowed_action_types={"ANALYZE_TABLE"},
        )
        self.assertTrue(errors)
        self.assertEqual(refs, [])

    def test_analyze_cannot_bind_column_found_only_in_literal(self):
        ledger = EvidenceLedger()
        query = "SELECT * FROM orders WHERE status = 'customer_id'"
        ledger.add(
            tool="analyze_query",
            arguments={"query": query},
            result={
                "cardinality_findings": [
                    {"type": "SEVERE_CARDINALITY_ERROR"}
                ],
                "scan_nodes": [
                    {
                        "table": "orders",
                        "filter": "status = 'customer_id'::text",
                        "predicate_columns": ["status"],
                    }
                ],
            },
            status="success",
            duration_ms=1.0,
        )
        ledger.add(
            tool="get_column_stats",
            arguments={
                "table_name": "orders",
                "column_name": "customer_id",
            },
            result={
                "n_mod_since_analyze": 10_000,
                "n_live_tup": 20_000,
            },
            status="success",
            duration_ms=1.0,
        )

        errors, refs = ledger.proposal_authorization(
            "propose_analyze_table",
            {
                "query": query,
                "table": "orders",
                "columns": ["customer_id"],
                "reason": "wrong column",
                "confidence": 0.9,
            },
            proposals_allowed=True,
            allowed_action_types={"ANALYZE_TABLE"},
        )

        self.assertTrue(errors)
        self.assertNotIn("ev-0001", refs)

    def test_explicit_proposal_intent_requires_positive_language(self):
        self.assertFalse(
            is_explicit_proposal_request(
                "Please inspect why the database is slow."
            )
        )
        self.assertTrue(
            is_explicit_proposal_request(
                "Diagnose and propose a remediation."
            )
        )

    def test_diagnosis_mode_blocks_proposals(self):
        ledger = EvidenceLedger()
        errors, refs = ledger.proposal_authorization(
            "propose_terminate_backend",
            {
                "blocked_pid": 1,
                "blocker_pid": 2,
            },
            proposals_allowed=False,
            allowed_action_types={"TERMINATE_BACKEND"},
        )
        self.assertTrue(errors)
        self.assertEqual(refs, [])

    def test_large_tool_output_is_valid_bounded_json(self):
        serialized = serialize_tool_output(
            {
                "payload": (
                    '"\\\n\t' * 4_000
                    + "x" * 10_000
                )
            },
            max_chars=1_000,
        )
        decoded = json.loads(serialized)
        self.assertTrue(decoded["truncated"])
        self.assertLessEqual(len(serialized), 1_000)

    def test_non_finite_tool_result_is_serialized_as_strict_json(self):
        serialized = serialize_tool_output(
            {"metric": float("nan")},
            max_chars=1_000,
        )

        decoded = json.loads(
            serialized,
            parse_constant=lambda value: self.fail(
                f"non-standard JSON constant: {value}"
            ),
        )
        self.assertEqual(
            decoded["metric"]["non_finite_float"],
            "nan",
        )


if __name__ == "__main__":
    unittest.main()
