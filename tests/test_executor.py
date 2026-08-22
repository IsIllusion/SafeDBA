import importlib.util
import io
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from incident_approval import compute_scope_digest  # noqa: E402


class IncidentApprovalUnavailable(RuntimeError):
    """Store rejection double used by the dependency-isolated executor test."""

    pass


AUDIT_RECORDS = []


def load_executor():
    actions = ModuleType("actions")
    actions.validate_action_proposal = lambda proposal: {
        "valid": True,
        "errors": [],
    }

    audit = ModuleType("audit")
    audit.write_audit_log = AUDIT_RECORDS.append

    config = ModuleType("config")
    config.MIN_IMPROVEMENT_PCT = 10.0
    config.MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO = 2.0

    db_tools = ModuleType("db_tools")
    for name in (
        "analyze_table",
        "benchmark_query",
        "compare_query_results",
        "create_index",
        "drop_index",
        "get_column_stats",
        "get_lock_waits",
        "get_query_plan",
        "terminate_blocking_backend",
    ):
        setattr(db_tools, name, lambda *args, **kwargs: None)

    diagnostics = ModuleType("diagnostics")
    diagnostics.analyze_query_plan = lambda plan: {}

    safety = ModuleType("safety")
    safety.assess_risk = lambda action: {
        "CREATE_INDEX": "MEDIUM",
        "REWRITE_QUERY": "LOW",
        "ANALYZE_TABLE": "MEDIUM",
        "TERMINATE_BACKEND": "HIGH",
    }.get(action, "CRITICAL")
    safety.is_operation_allowed = lambda risk: risk != "CRITICAL"
    safety.requires_approval = lambda risk: risk in {
        "MEDIUM",
        "HIGH",
        "CRITICAL",
    }

    stubs = {
        "actions": actions,
        "audit": audit,
        "config": config,
        "db_tools": db_tools,
        "diagnostics": diagnostics,
        "safety": safety,
    }
    previous = {
        name: sys.modules.get(name)
        for name in stubs
    }
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "executor_under_test",
            SRC / "executor.py",
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


EXECUTOR = load_executor()


PROPOSAL = {
    "type": "CREATE_INDEX",
    "query": "SELECT * FROM orders WHERE customer_id = 1",
    "table": "orders",
    "column": "customer_id",
    "index_name": "idx_orders_customer_id",
    "reason": "selective sequential scan",
    "confidence": 0.9,
    "risk": "MEDIUM",
}

IDENTITY = {
    "index_oid": 9001,
    "table_oid": 8001,
    "schema": "public",
    "index_name": "idx_orders_customer_id",
}


class ExecutorCreateIndexTests(unittest.TestCase):
    def setUp(self):
        AUDIT_RECORDS.clear()
        EXECUTOR.write_audit_log = AUDIT_RECORDS.append
        EXECUTOR.validate_action_proposal = lambda proposal: {
            "valid": True,
            "errors": [],
        }
        EXECUTOR.create_index = lambda **kwargs: dict(IDENTITY)
        self.drop_calls = []
        EXECUTOR.drop_index = (
            lambda index_name, **kwargs: self.drop_calls.append(
                (index_name, kwargs)
            )
        )

    def _run(self):
        with patch("builtins.input", return_value="y"):
            with redirect_stdout(io.StringIO()):
                return EXECUTOR.execute_action_proposal(
                    dict(PROPOSAL)
                )

    def test_verification_error_rolls_back_oid_bound_index(self):
        calls = iter([
            {"median_ms": 100.0},
            RuntimeError("verification failed"),
        ])

        def benchmark(query):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        EXECUTOR.benchmark_query = benchmark
        EXECUTOR.capture_query_state = lambda **kwargs: {
            "scan": {"scan_type": "Sequential Scan"},
            "scan_nodes": [],
        }

        result = self._run()

        self.assertEqual(
            result["status"],
            "ROLLED_BACK_AFTER_VERIFICATION_ERROR",
        )
        self.assertEqual(
            self.drop_calls,
            [
                (
                    "idx_orders_customer_id",
                    {
                        "expected_index_oid": 9001,
                        "expected_table_oid": 8001,
                    },
                )
            ],
        )

    def test_rollback_failure_is_never_reported_as_success(self):
        calls = iter([
            {"median_ms": 100.0},
            RuntimeError("verification failed"),
        ])

        def benchmark(query):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        EXECUTOR.benchmark_query = benchmark
        EXECUTOR.capture_query_state = lambda **kwargs: {
            "scan_nodes": [],
        }
        EXECUTOR.drop_index = lambda *args, **kwargs: (
            (_ for _ in ()).throw(
                RuntimeError("OID mismatch")
            )
        )

        result = self._run()

        self.assertEqual(result["status"], "ROLLBACK_FAILED")
        self.assertEqual(result["decision"], "REVIEW")

    def test_index_is_kept_only_when_used_and_faster(self):
        benchmark_results = iter([
            {"median_ms": 100.0},
            {"median_ms": 50.0},
        ])
        EXECUTOR.benchmark_query = lambda query: next(
            benchmark_results
        )
        states = iter([
            {
                "scan": {"scan_type": "Sequential Scan"},
                "scan_nodes": [
                    {"scan_type": "Sequential Scan"}
                ],
            },
            {
                "scan": {"scan_type": "Index Scan"},
                "scan_nodes": [
                    {
                        "scan_type": "Index Scan",
                        "index_name": "idx_orders_customer_id",
                    }
                ],
            },
        ])
        EXECUTOR.capture_query_state = lambda **kwargs: next(states)

        result = self._run()

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["decision"], "KEEP")
        self.assertEqual(result["created_index_identity"], IDENTITY)
        self.assertEqual(self.drop_calls, [])
        self.assertEqual(AUDIT_RECORDS[0]["status"], "INTENT_RECEIVED")
        self.assertEqual(AUDIT_RECORDS[-1]["status"], "SUCCESS")
        self.assertEqual(
            {
                AUDIT_RECORDS[0]["operation_id"],
                AUDIT_RECORDS[-1]["operation_id"],
                result["operation_id"],
            },
            {result["operation_id"]},
        )

    def test_unused_index_is_rolled_back_even_if_latency_improves(self):
        benchmark_results = iter([
            {"median_ms": 100.0},
            {"median_ms": 50.0},
        ])
        EXECUTOR.benchmark_query = lambda query: next(
            benchmark_results
        )
        EXECUTOR.capture_query_state = lambda **kwargs: {
            "scan": {"scan_type": "Sequential Scan"},
            "scan_nodes": [
                {"scan_type": "Sequential Scan"}
            ],
        }

        result = self._run()

        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertEqual(
            result["rollback_reason"],
            "created_index_not_used",
        )
        self.assertEqual(len(self.drop_calls), 1)

    def test_unavailable_audit_sink_blocks_before_database_action(self):
        create_calls = []
        EXECUTOR.create_index = lambda **kwargs: create_calls.append(
            kwargs
        )
        EXECUTOR.write_audit_log = lambda record: (
            (_ for _ in ()).throw(OSError("disk unavailable"))
        )

        result = self._run()

        self.assertEqual(
            result["status"],
            "BLOCKED_AUDIT_UNAVAILABLE",
        )
        self.assertEqual(create_calls, [])

    def test_unexpected_failure_is_correlated_and_requires_review(self):
        EXECUTOR.benchmark_query = lambda query: (
            (_ for _ in ()).throw(RuntimeError("unexpected"))
        )
        create_calls = []
        EXECUTOR.create_index = lambda **kwargs: create_calls.append(kwargs)

        result = self._run()

        self.assertEqual(result["status"], "FAILED_UNHANDLED")
        self.assertEqual(result["decision"], "REVIEW")
        self.assertEqual(create_calls, [])
        self.assertEqual(len(AUDIT_RECORDS), 2)
        self.assertEqual(
            AUDIT_RECORDS[0]["operation_id"],
            AUDIT_RECORDS[1]["operation_id"],
        )


class ExecutorIncidentApprovalTests(unittest.TestCase):
    def setUp(self):
        AUDIT_RECORDS.clear()
        EXECUTOR.write_audit_log = AUDIT_RECORDS.append
        self.terminate_calls = []
        self.proposal = {
            "type": "TERMINATE_BACKEND",
            "database_name": "benchmark",
            "blocked_pid": 101,
            "blocker_pid": 202,
            "blocker_backend_start": (
                "2026-08-14T00:00:00+00:00"
            ),
            "blocker_xact_start": (
                "2026-08-14T00:01:00+00:00"
            ),
            "reason": "Exact current lock blocker.",
            "confidence": 0.95,
            "risk": "HIGH",
        }
        EXECUTOR.validate_action_proposal = lambda value: {
            "valid": True,
            "errors": [],
            "current_blocking_evidence": {
                "database_name": "benchmark",
                "blocked_pid": 101,
                "blocked_backend_start": (
                    "2026-08-14T00:02:00+00:00"
                ),
                "blocked_xact_start": (
                    "2026-08-14T00:03:00+00:00"
                ),
                "blocker_pid": 202,
                "blocker_backend_start": self.proposal[
                    "blocker_backend_start"
                ],
                "blocker_xact_start": self.proposal[
                    "blocker_xact_start"
                ],
                "blocker_state": "idle in transaction",
            },
        }
        EXECUTOR.terminate_blocking_backend = (
            lambda **kwargs: self._terminate(kwargs)
        )
        EXECUTOR.get_lock_waits = lambda: []

    def _terminate(self, kwargs):
        self.terminate_calls.append(kwargs)
        return {
            "final_validation_passed": True,
            "terminated": True,
        }

    def approval_context(self):
        incident_id = str(uuid.uuid4())
        action_id = str(uuid.uuid4())
        approved_actions = [{
            "action_id": action_id,
            "ordinal": 1,
            "target": {
                "database_name": "benchmark",
                "blocker_pid": 202,
                "blocker_backend_start": self.proposal[
                    "blocker_backend_start"
                ],
                "blocker_xact_start": self.proposal[
                    "blocker_xact_start"
                ],
            },
            "allowed_blocked_pids": [101],
            "approved_waiters": [{
                "blocked_pid": 101,
                "blocked_backend_start": (
                    "2026-08-14T00:02:00+00:00"
                ),
                "blocked_xact_start": (
                    "2026-08-14T00:03:00+00:00"
                ),
            }],
        }]
        current = datetime.now(timezone.utc)
        context = {
            "kind": "LOCK_INCIDENT_APPROVAL",
            "approval_id": str(uuid.uuid4()),
            "incident_id": incident_id,
            "plan_revision": 1,
            "approved_at": current.isoformat(),
            "expires_at": (
                current + timedelta(minutes=2)
            ).isoformat(),
            "max_risk": "HIGH",
            "max_actions": 1,
            "approved_actions": approved_actions,
            "current_action_id": action_id,
        }
        context["scope_digest"] = compute_scope_digest(
            incident_id=incident_id,
            plan_revision=1,
            approved_actions=approved_actions,
            max_actions=1,
            max_risk="HIGH",
        )
        return context, action_id

    @staticmethod
    def execution_context(context, action_id):
        return {
            "kind": "LOCK_INCIDENT_EXECUTION",
            "store_path": "C:/configured/incidents.sqlite3",
            "worker_id": "worker-test",
            "incident_id": context["incident_id"],
            "action_id": action_id,
            "plan_revision": context["plan_revision"],
        }

    def test_exact_incident_approval_skips_second_interactive_prompt(self):
        context, action_id = self.approval_context()
        execution_context = self.execution_context(
            context,
            action_id,
        )
        claim_calls = []

        class FakeStore:
            def claim_action_execution(self, **kwargs):
                claim_calls.append(kwargs)
                return {
                    "kind": "PERSISTED_ACTION_EXECUTION_CLAIM",
                    "incident_id": context["incident_id"],
                    "approval_id": context["approval_id"],
                    "action_id": action_id,
                    "operation_id": action_id,
                    "plan_revision": 1,
                    "scope_digest": context["scope_digest"],
                    "evidence_digest": "evidence-digest",
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                }

        with patch(
            "builtins.input",
            side_effect=AssertionError("unexpected second approval"),
        ):
            with patch.object(
                EXECUTOR,
                "incident_store_factory",
                return_value=FakeStore(),
            ):
                with redirect_stdout(io.StringIO()):
                    result = EXECUTOR.execute_action_proposal(
                        dict(self.proposal),
                        operation_id=action_id,
                        approval_context=context,
                        execution_context=execution_context,
                    )

        self.assertEqual(
            result["status"],
            "BACKEND_TERMINATION_CONFIRMED",
        )
        self.assertEqual(
            result["approval_source"]["mode"],
            "PERSISTED_INCIDENT_CLAIM",
        )
        self.assertEqual(len(self.terminate_calls), 1)
        self.assertEqual(len(claim_calls), 1)
        self.assertEqual(
            claim_calls[0]["current_evidence"][
                "blocked_backend_start"
            ],
            "2026-08-14T00:02:00+00:00",
        )
        self.assertEqual(
            self.terminate_calls[0]["blocked_backend_start"],
            "2026-08-14T00:02:00+00:00",
        )
        self.assertEqual(
            self.terminate_calls[0]["blocked_xact_start"],
            "2026-08-14T00:03:00+00:00",
        )
        self.assertEqual(result["audit"]["status"], "WRITTEN")

    def test_tampered_incident_approval_is_blocked_before_termination(self):
        context, action_id = self.approval_context()
        with self.subTest("self-contained approval is not authority"):
            with redirect_stdout(io.StringIO()):
                result = EXECUTOR.execute_action_proposal(
                    dict(self.proposal),
                    operation_id=action_id,
                    approval_context=context,
                )
            self.assertEqual(
                result["status"],
                "BLOCKED_INCIDENT_APPROVAL",
            )

        class RejectingStore:
            def claim_action_execution(self, **kwargs):
                raise IncidentApprovalUnavailable(
                    "forged or stale approval"
                )

        with self.subTest("store rejection is fail closed"):
            with patch.object(
                EXECUTOR,
                "incident_store_factory",
                return_value=RejectingStore(),
            ):
                with redirect_stdout(io.StringIO()):
                    result = EXECUTOR.execute_action_proposal(
                        dict(self.proposal),
                        operation_id=action_id,
                        approval_context=context,
                        execution_context=self.execution_context(
                            context,
                            action_id,
                        ),
                    )
            self.assertEqual(
                result["status"],
                "BLOCKED_INCIDENT_APPROVAL",
            )
            self.assertEqual(
                result["approval_error_type"],
                "IncidentApprovalUnavailable",
            )
        self.assertEqual(self.terminate_calls, [])

    def test_single_action_path_still_requires_interactive_approval(self):
        with patch("builtins.input", return_value="y") as prompt:
            with patch.object(
                EXECUTOR,
                "incident_store_factory",
                side_effect=AssertionError(
                    "single-action approval must not claim an incident"
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    result = EXECUTOR.execute_action_proposal(
                        dict(self.proposal)
                    )

        self.assertEqual(
            result["status"],
            "BACKEND_TERMINATION_CONFIRMED",
        )
        self.assertEqual(
            result["approval_source"]["mode"],
            "INTERACTIVE_SINGLE_ACTION",
        )
        prompt.assert_called_once()
        self.assertEqual(len(self.terminate_calls), 1)


if __name__ == "__main__":
    unittest.main()
