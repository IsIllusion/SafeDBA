import importlib.util
import os
import json
import tempfile
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from live_evaluate import BudgetedProvider, CASES, EvaluationBudgetExceeded, MAX_REQUEST_CHARS, grade_case


class LiveEvaluationTests(unittest.TestCase):
    def test_live_setup_failure_cannot_inherit_passing_integration_status(self):
        spec = importlib.util.spec_from_file_location("failed_live_runner_test", ROOT / "scripts/run_postgres_integration.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "tests/fixtures/postgres_integration.sql"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("-- synthetic runner unit test", encoding="utf-8")
            def fake_command(args, **kwargs):
                if "--report" in args:
                    report_path = Path(args[args.index("--report") + 1])
                    report_path.write_text(json.dumps({"tests_run": 1, "passed": True}), encoding="utf-8")
                return SimpleNamespace(returncode=3 if "status" in args else 0, stdout="test", stderr="")
            with patch.object(runner, "ROOT", root), patch.object(runner, "binary", side_effect=lambda directory, name: name), patch.object(runner, "source_fingerprint", return_value="test-fingerprint"), patch.object(runner, "command", side_effect=fake_command), patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0)), patch.object(runner.psycopg, "connect"), patch.object(runner, "run_live_evaluation", side_effect=TimeoutError("injected")), patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--live-model-eval"]), 1)
            report = json.loads(next(root.glob("logs/integration-runs/*/report.json")).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["error_type"], "TimeoutError")
            self.assertIsNone(report["live_llm_used"])
            self.assertTrue(report["server_stopped"])
            self.assertTrue(report["cluster_removed"])

    def test_request_budget_counts_attempts_and_stops_after_failure(self):
        underlying = Mock(model="test-only")
        underlying.complete.side_effect = RuntimeError("provider error")
        provider = BudgetedProvider(underlying)
        with self.assertRaises(RuntimeError):
            provider.complete(messages=[])
        with self.assertRaises(EvaluationBudgetExceeded):
            provider.complete(messages=[])
        self.assertEqual(len(provider.calls), 1)
        underlying.complete.assert_called_once()
        self.assertNotIn("provider error", str(provider.calls))

    def test_request_and_input_ceilings_apply_before_network(self):
        underlying = Mock(model="test-only")
        underlying.complete.return_value = SimpleNamespace(model="test", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12))
        provider = BudgetedProvider(underlying, max_requests=1)
        with self.assertRaises(EvaluationBudgetExceeded):
            provider.complete(messages=["x" * MAX_REQUEST_CHARS])
        underlying.complete.assert_not_called()
        provider.complete(messages=[])
        with self.assertRaises(EvaluationBudgetExceeded):
            provider.complete(messages=[])
        self.assertEqual(provider.calls[0]["total_tokens"], 12)
        underlying.complete.assert_called_once()

    def test_deterministic_grader_rejects_missing_facts_and_fabricated_refs(self):
        result = {"status": "completed", "stop_reason": "final_answer", "answer": "Blocker 123, waiter 456, idle in transaction [ev-0001]", "proposals": [], "tool_trace": [{"tool": "get_lock_waits", "status": "success", "evidence_ref": "ev-0001"}]}
        self.assertTrue(grade_case(CASES[2], result, blocker_pid=123, waiter_pid=456)["passed"])
        for answer in ("123 456 idle in transaction [ev-9999]", "123 999 idle in transaction [ev-0001]", "123 456 active [ev-0001]"):
            self.assertFalse(grade_case(CASES[2], {**result, "answer": answer}, blocker_pid=123, waiter_pid=456)["passed"])

    def test_model_environment_whitelists_model_settings_not_business_database(self):
        spec = importlib.util.spec_from_file_location("live_runner_test", ROOT / "scripts/run_postgres_integration.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fake_dotenv = SimpleNamespace(dotenv_values=lambda *a, **k: {"SAFEDBA_LLM_PROVIDER": "deepseek", "SAFEDBA_LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "test-key", "SAFEDBA_DB_HOST": "business", "SAFEDBA_TERMINATOR_DB_PASSWORD": "business-secret"})
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"dotenv": fake_dotenv}):
            env = module.isolated_environment(54321, Path("output"), "test-instance")
            value = module.live_model_environment(env)
        self.assertEqual(value["SAFEDBA_DB_HOST"], "127.0.0.1")
        self.assertEqual(value["SAFEDBA_LLM_MODEL"], "test-model")
        self.assertEqual(value["SAFEDBA_PROCESS_ROLE"], "agent")
        self.assertEqual(value["SAFEDBA_ENABLE_MUTATIONS"], "false")
        self.assertNotIn("SAFEDBA_TERMINATOR_DB_PASSWORD", value)
        self.assertNotIn("SAFEDBA_EXECUTOR_DB_PASSWORD", value)
        self.assertNotIn("business", str(value))
        self.assertFalse(any(key.startswith("SAFEDBA_LLM_FALLBACK") for key in value))


if __name__ == "__main__":
    unittest.main()
