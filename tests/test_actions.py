import importlib.util
import math
from pathlib import Path
from types import ModuleType
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from query_guard import ensure_read_only_query  # noqa: E402


def load_actions():
    db_tools = ModuleType("db_tools")
    db_tools.ensure_read_only_query = ensure_read_only_query
    db_tools.get_indexes = lambda table: []
    db_tools.get_lock_waits = lambda: []
    db_tools.get_table_columns = lambda table: [
        "id",
        "customer_id",
        "created_at",
    ]

    diagnostics = ModuleType("diagnostics")
    diagnostics.column_has_index = (
        lambda column, indexes: False
    )

    safety = ModuleType("safety")
    risks = {
        "CREATE_INDEX": "MEDIUM",
        "REWRITE_QUERY": "LOW",
        "ANALYZE_TABLE": "MEDIUM",
        "TERMINATE_BACKEND": "HIGH",
    }
    safety.assess_risk = lambda action: risks.get(
        action,
        "CRITICAL",
    )

    stubs = {
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
            "actions_under_test",
            SRC / "actions.py",
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


ACTIONS = load_actions()


class ActionsTests(unittest.TestCase):
    def test_confidence_rejects_bool_nan_and_infinity(self):
        for value in [True, False, math.nan, math.inf]:
            errors = []
            ACTIONS.validate_confidence(
                {"confidence": value},
                errors,
            )
            with self.subTest(value=value):
                self.assertTrue(errors)

    def test_long_unicode_index_name_is_stable_and_within_pg_limit(self):
        first = ACTIONS.build_index_name(
            "订单" * 30,
            "客户编号" * 20,
        )
        second = ACTIONS.build_index_name(
            "订单" * 30,
            "客户编号" * 20,
        )
        self.assertEqual(first, second)
        self.assertLessEqual(
            len(first.encode("utf-8")),
            63,
        )

    def test_create_index_rejects_unsafe_query(self):
        proposal = ACTIONS.build_create_index_proposal(
            query="SELECT pg_sleep(10)",
            table="orders",
            column="customer_id",
            reason="test",
            confidence=0.9,
        )
        result = ACTIONS.validate_action_proposal(
            proposal
        )
        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "pg_sleep" in error
                for error in result["errors"]
            )
        )

    def test_proposal_shape_rejects_blank_identifier_and_nan(self):
        proposal = ACTIONS.build_create_index_proposal(
            query="SELECT * FROM orders",
            table="orders",
            column="   ",
            reason="test",
            confidence=math.nan,
        )

        result = ACTIONS.validate_proposal_shape(proposal)

        self.assertFalse(result["valid"])
        self.assertGreaterEqual(len(result["errors"]), 2)

    def test_analyze_rejects_non_string_columns_without_crashing(self):
        proposal = ACTIONS.build_analyze_table_proposal(
            query="SELECT * FROM orders",
            table="orders",
            columns=["created_at", 7],
            reason="test",
            confidence=0.9,
        )
        result = ACTIONS.validate_action_proposal(
            proposal
        )
        self.assertFalse(result["valid"])

    def test_query_rewrite_rejects_non_string_input_without_crashing(self):
        proposal = {
            "type": "REWRITE_QUERY",
            "original_query": 123,
            "rewritten_query": "SELECT 1",
            "reason": "test",
            "confidence": 0.9,
            "risk": "LOW",
        }
        result = ACTIONS.validate_action_proposal(
            proposal
        )
        self.assertFalse(result["valid"])

    def test_terminate_proposal_is_bound_to_backend_and_transaction_start(self):
        relationship = {
            "blocked_pid": 101,
            "blocker_pid": 202,
            "blocked_wait_event_type": "Lock",
            "blocker_state": "idle in transaction",
            "blocker_backend_start": "backend-start",
            "blocker_xact_start": "xact-start",
        }
        ACTIONS.get_lock_waits = lambda: [relationship]
        proposal = ACTIONS.build_terminate_backend_proposal(
            blocked_pid=101,
            blocker_pid=202,
            blocker_backend_start="backend-start",
            blocker_xact_start="xact-start",
            reason="current idle blocker",
            confidence=0.9,
        )

        self.assertTrue(
            ACTIONS.validate_action_proposal(proposal)["valid"]
        )

        proposal["blocker_xact_start"] = "new-transaction"
        self.assertFalse(
            ACTIONS.validate_action_proposal(proposal)["valid"]
        )


if __name__ == "__main__":
    unittest.main()
