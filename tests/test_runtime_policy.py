import json
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtime_policy import (  # noqa: E402
    ACTION_TYPES, RuntimePolicyError, get_runtime_policy, require_operation,
)
from test_db_tools_safety import DB_TOOLS, EXECUTIONS  # noqa: E402
from test_agent_loop import AGENT, CALLS, FakeProvider, response, tool_call  # noqa: E402
from test_executor import EXECUTOR, AUDIT_RECORDS  # noqa: E402
from test_mcp_adapter import make_facade, make_spec, ToolRegistry  # noqa: E402


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directory = self.contexts.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "controls.json"
        self.settings = SimpleNamespace(
            SAFEDBA_ENV="development", RUNTIME_CONTROLS_PATH=self.path,
            RUNTIME_CONTROLS_REQUIRED=False,
        )
        self.contexts.enter_context(patch.dict(sys.modules, {"config": self.settings}))

    def controls(self, **values):
        self.path.write_text(json.dumps({"version": 1, **values}), encoding="utf-8")

    def test_development_preserves_existing_workflows(self):
        self.assertEqual(get_runtime_policy()["blocked"], {})

    def test_production_disallows_workload_execution_even_with_startup_flags(self):
        self.settings.SAFEDBA_ENV = "production"
        self.settings.ALLOW_RUNTIME_ANALYSIS = True
        self.settings.ALLOW_BENCHMARK = True
        self.settings.ENABLE_MUTATIONS = True
        for op in ("EXPLAIN_ANALYZE", "BENCHMARK", "COMPARE_QUERY_RESULTS", "CREATE_INDEX", "ANALYZE_TABLE", "REWRITE_QUERY", "DROP_INDEX"):
            with self.subTest(operation=op), self.assertRaises(RuntimePolicyError):
                require_operation(op)
        require_operation("OBSERVE")

    def test_production_termination_is_opt_in_and_can_be_disabled_live(self):
        self.settings.SAFEDBA_ENV = "production"
        with self.assertRaises(RuntimePolicyError):
            require_operation("TERMINATE_BACKEND")
        self.settings.ENABLE_MUTATIONS = True
        self.settings.ENABLE_TERMINATE_BACKEND = True
        require_operation("TERMINATE_BACKEND")
        self.controls(disabled_actions=["TERMINATE_BACKEND"])
        with self.assertRaises(RuntimePolicyError):
            require_operation("TERMINATE_BACKEND")

    def test_live_controls_can_restrict_but_not_raise_startup_privileges(self):
        self.settings.ENABLE_MUTATIONS = False
        self.controls(disable_mutations=False, disabled_actions=[])
        with self.assertRaises(RuntimePolicyError):
            require_operation("CREATE_INDEX")

    def test_each_action_flag_and_live_flag_blocks_its_action(self):
        for action in ACTION_TYPES:
            with self.subTest(action=action):
                self.controls(disabled_actions=[action])
                with self.assertRaises(RuntimePolicyError):
                    require_operation(action)
                self.controls()
                setattr(self.settings, f"ENABLE_{action}", False)
                with self.assertRaises(RuntimePolicyError):
                    require_operation(action)
                setattr(self.settings, f"ENABLE_{action}", True)

    def test_invalid_controls_fail_closed_without_leaking_contents(self):
        invalid = [
            '{', '[]', '{"version":true}', '{"version":2}',
            '{"version":1,"disable_agent":"false"}',
            '{"version":1,"disable_agent":true,"disable_agent":false}',
            '{"version":1,"enable_mutations":true}',
            '{"version":1,"disabled_actions":["UNKNOWN"]}',
            '{"version":1,"disabled_actions":["CREATE_INDEX","CREATE_INDEX"]}',
            '{"version":1,"disabled_actions":[{}]}',
            '{"version":1,"disable_agent":NaN}',
            '{"version":1,"secret":"do-not-leak"}',
            ' ' * 16_385,
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:80]):
                self.path.write_text(raw, encoding="utf-8")
                policy = get_runtime_policy()
                self.assertFalse(policy["valid"])
                self.assertEqual(policy["allowed"], [])
                self.assertNotIn("do-not-leak", json.dumps(policy))

    def test_required_control_file_missing_or_unreadable_blocks(self):
        self.settings.RUNTIME_CONTROLS_REQUIRED = True
        self.assertFalse(get_runtime_policy()["valid"])
        self.path.mkdir()
        self.assertFalse(get_runtime_policy()["valid"])

    def test_staging_benchmarks_require_explicit_opt_in(self):
        self.settings.SAFEDBA_ENV = "staging"
        require_operation("EXPLAIN_ANALYZE")
        with self.assertRaises(RuntimePolicyError):
            require_operation("BENCHMARK")
        self.settings.ALLOW_BENCHMARK = True
        require_operation("BENCHMARK")

    def test_unknown_environment_and_operation_fail_closed(self):
        with self.assertRaises(RuntimePolicyError):
            require_operation("UNRECOGNIZED")
        self.settings.SAFEDBA_ENV = "prodution"
        self.assertFalse(get_runtime_policy()["valid"])

    def test_direct_database_calls_cannot_bypass_production_policy(self):
        self.settings.SAFEDBA_ENV = "production"
        functions = [
            lambda: DB_TOOLS.get_query_plan("SELECT 1"),
            lambda: DB_TOOLS._run_explain("SELECT 1", analyze=True),
            lambda: DB_TOOLS.benchmark_query("SELECT 1"),
            lambda: DB_TOOLS.compare_query_results("SELECT 1", "SELECT 1"),
            lambda: DB_TOOLS.create_index("orders", "id", "test_index"),
            lambda: DB_TOOLS.analyze_table("orders"),
            lambda: DB_TOOLS.drop_index("test_index", expected_index_oid=1, expected_table_oid=2),
            lambda: DB_TOOLS.terminate_blocking_backend(1, 2, "a", "b", blocked_backend_start="c", blocked_xact_start="d"),
        ]
        with patch.object(DB_TOOLS.psycopg, "connect") as connect:
            for function in functions:
                with self.assertRaises(RuntimePolicyError):
                    function()
            connect.assert_not_called()

    def test_production_estimated_plan_remains_available(self):
        self.settings.SAFEDBA_ENV = "production"
        EXECUTIONS.clear()
        DB_TOOLS.get_estimated_query_plan("SELECT 1")
        explains = [call[0] for call in EXECUTIONS if str(call[0]).startswith("EXPLAIN")]
        self.assertEqual(len(explains), 1)
        self.assertNotIn("ANALYZE", explains[0])

    def test_malformed_benchmark_budgets_do_not_execute_queries(self):
        with patch.object(DB_TOOLS, "get_query_plan") as plan:
            for warmups, runs in [(-1, 5), (2, 0), (True, 1), (1, False), (1.0, 2), (1, 20), (0, 100_000)]:
                with self.subTest(warmups=warmups, runs=runs), self.assertRaises(ValueError):
                    DB_TOOLS.benchmark_query("SELECT 1", warmups, runs)
            plan.assert_not_called()

    def test_benchmark_rechecks_controls_between_samples(self):
        def sample(query):
            self.controls(disable_benchmarks=True)
            return {"Execution Time": 1.0}
        with patch.object(DB_TOOLS, "get_query_plan", side_effect=sample) as plan:
            with self.assertRaises(RuntimePolicyError):
                DB_TOOLS.benchmark_query("SELECT 1", warmups=0, runs=3)
            self.assertEqual(plan.call_count, 1)

    def test_executor_blocks_before_approval_claim_or_benchmark_and_audits(self):
        self.controls(disable_mutations=True, disabled_actions=["REWRITE_QUERY"])
        AUDIT_RECORDS.clear()
        with patch.object(EXECUTOR, "write_audit_log", side_effect=AUDIT_RECORDS.append), patch.object(EXECUTOR, "_execute_action_proposal_with_intent") as execute:
            for action in ACTION_TYPES:
                result = EXECUTOR.execute_action_proposal({"type": action})
                self.assertEqual(result["status"], "BLOCKED_RUNTIME_POLICY")
                self.assertFalse(result["executed"])
            execute.assert_not_called()
        self.assertEqual(len(AUDIT_RECORDS), len(ACTION_TYPES) * 2)

    def test_agent_does_not_start_security_or_model_when_disabled(self):
        self.controls(disable_agent=True)
        provider = FakeProvider([])
        with patch.object(AGENT, "verify_runtime_security") as verify:
            result = AGENT.run_agent("Investigate database", provider=provider)
            verify.assert_not_called()
        self.assertEqual(result["stop_reason"], "runtime_policy_blocked")
        self.assertEqual(provider.message_batches, [])

    def test_execution_review_does_not_start_another_model_request_after_stop(self):
        self.controls(disable_agent=True)
        with patch.object(AGENT, "get_llm_provider") as provider:
            result = AGENT.review_execution_result({}, {"status": "BLOCKED_RUNTIME_POLICY"})
            provider.assert_not_called()
            self.assertIn("skipped by runtime policy", result)

    def test_agent_hides_and_rejects_runtime_tools_in_production(self):
        self.settings.SAFEDBA_ENV = "production"
        CALLS.clear()
        provider = FakeProvider([response(calls=[tool_call("p1", "get_query_plan", {"query": "SELECT 1"})], finish_reason="tool_calls")])
        AGENT.run_agent("Run my query now", provider=provider, max_iterations=1)
        self.assertNotIn("get_query_plan", provider.tool_sets[0])
        self.assertNotIn("analyze_query", provider.tool_sets[0])
        self.assertIn("get_estimated_query_plan", provider.tool_sets[0])
        self.assertFalse(any(call[0] == "get_query_plan" for call in CALLS))

    def test_agent_switch_changed_during_model_call_stops_tools_and_next_turn(self):
        CALLS.clear()
        parent = self
        class SwitchingProvider(FakeProvider):
            def complete(self, **kwargs):
                parent.controls(disable_agent=True)
                return super().complete(**kwargs)
        provider = SwitchingProvider([response(calls=[tool_call("p1", "get_database_health", {})], finish_reason="tool_calls")])
        result = AGENT.run_agent("Investigate database", provider=provider)
        self.assertEqual(result["stop_reason"], "runtime_policy_blocked")
        self.assertEqual(CALLS, [])
        self.assertEqual(len(provider.message_batches), 1)

    def test_mcp_runtime_tool_cannot_bypass_policy_or_attest_first(self):
        self.settings.SAFEDBA_ENV = "production"
        handler, verifier = Mock(), Mock()
        facade = make_facade(ToolRegistry([make_spec(name="get_query_plan", handler=handler)]), verifier=verifier)
        with self.assertRaises(RuntimePolicyError):
            facade.call_database_tool("get_query_plan")
        handler.assert_not_called()
        verifier.assert_not_called()

    def test_mcp_diagnosis_respects_global_stop_before_invoking_runner(self):
        self.controls(disable_agent=True)
        runner = Mock()
        facade = make_facade(ToolRegistry([make_spec()]), runner=runner)
        with self.assertRaises(RuntimePolicyError):
            facade.diagnose_database("Investigate database")
        runner.assert_not_called()

    def test_termination_rechecks_switch_after_connection_is_opened(self):
        from contextlib import contextmanager

        @contextmanager
        def connected():
            self.controls(disabled_actions=["TERMINATE_BACKEND"])
            yield MagicMock()

        with patch.object(DB_TOOLS, "terminator_connection", connected):
            with self.assertRaises(RuntimePolicyError):
                DB_TOOLS.terminate_blocking_backend(
                    1, 2, "a", "b", blocked_backend_start="c", blocked_xact_start="d",
                )


if __name__ == "__main__":
    unittest.main()
