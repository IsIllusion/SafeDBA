import importlib.util
from pathlib import Path
from types import ModuleType
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def load_diagnostics():
    stub = ModuleType("db_tools")
    stub.get_indexes = lambda table: [
        {
            "index_name": "idx_orders_created_at",
            "index_definition": (
                "CREATE INDEX idx_orders_created_at "
                "ON public.orders USING btree (created_at)"
            ),
        }
    ]
    prior = sys.modules.get("db_tools")
    sys.modules["db_tools"] = stub

    try:
        spec = importlib.util.spec_from_file_location(
            "diagnostics_under_test",
            SRC / "diagnostics.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if prior is None:
            sys.modules.pop("db_tools", None)
        else:
            sys.modules["db_tools"] = prior

    return module


DIAGNOSTICS = load_diagnostics()


def plan(
    *,
    estimated=10,
    actual=10,
    parallel=False,
    filter_text=None,
):
    node = {
        "Node Type": "Seq Scan",
        "Relation Name": "orders",
        "Plan Rows": estimated,
        "Actual Rows": actual,
        "Actual Loops": 1,
        "Parallel Aware": parallel,
    }
    if filter_text:
        node["Filter"] = filter_text

    return {
        "Plan": node,
        "Planning Time": 0.1,
        "Execution Time": 1.0,
    }


class DiagnosticsTests(unittest.TestCase):
    def test_predicate_columns_exclude_literals_casts_and_function_names(self):
        columns = DIAGNOSTICS.extract_predicate_columns(
            "(status = 'customer_id'::text AND "
            "date(created_at) = '2026-05-18'::date AND "
            "status <> E'fake\\'column'::text)"
        )

        self.assertEqual(columns, ["status", "created_at"])

    def test_serial_seq_scan_is_normalized_for_non_sargable_detector(self):
        analysis = DIAGNOSTICS.analyze_query_plan(
            plan(
                filter_text=(
                    "(date(created_at) = '2026-05-18'::date)"
                )
            )
        )
        self.assertEqual(
            analysis["scan_nodes"][0]["scan_type"],
            "Sequential Scan",
        )
        self.assertEqual(
            analysis["scan_nodes"][0]["predicate_columns"],
            ["created_at"],
        )
        findings = (
            DIAGNOSTICS.detect_non_sargable_predicates(
                analysis
            )
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0]["column"],
            "created_at",
        )

    def test_parallel_seq_scan_has_explicit_name(self):
        analysis = DIAGNOSTICS.analyze_query_plan(
            plan(parallel=True)
        )
        self.assertEqual(
            analysis["scan_nodes"][0]["scan_type"],
            "Parallel Sequential Scan",
        )

    def test_zero_estimate_is_reported_as_unbounded_underestimate(self):
        analysis = DIAGNOSTICS.analyze_query_plan(
            plan(estimated=0, actual=25)
        )
        findings = (
            DIAGNOSTICS.detect_cardinality_anomalies(
                analysis
            )
        )
        self.assertEqual(len(findings), 1)
        self.assertTrue(
            findings[0]["unbounded_error"]
        )
        self.assertEqual(
            findings[0]["direction"],
            "UNDER_ESTIMATE",
        )

    def test_zero_actual_is_reported_as_unbounded_overestimate(self):
        analysis = DIAGNOSTICS.analyze_query_plan(
            plan(estimated=25, actual=0)
        )
        findings = (
            DIAGNOSTICS.detect_cardinality_anomalies(
                analysis
            )
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0]["direction"],
            "OVER_ESTIMATE",
        )


if __name__ == "__main__":
    unittest.main()
