import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


IMPORT_CONFIG = (
    "import sys, types; "
    "m=types.ModuleType('dotenv'); "
    "m.load_dotenv=lambda *a, **k: None; "
    "sys.modules['dotenv']=m; "
    f"sys.path.insert(0, {str(SRC)!r}); "
    "import config; print('ok')"
)


class ConfigSafetyTests(unittest.TestCase):
    def test_knowledge_requires_explicit_valid_deployment_scope_and_version(self):
        for changes in ({"SAFEDBA_KNOWLEDGE_SCOPE": ""},
                        {"SAFEDBA_KNOWLEDGE_POSTGRES_MAJOR": "0"},
                        {"SAFEDBA_KNOWLEDGE_SCOPE": "*"}):
            result = self.run_import({"SAFEDBA_KNOWLEDGE_ENABLED": "true",
                                      "SAFEDBA_KNOWLEDGE_SCOPE": "team-a",
                                      "SAFEDBA_KNOWLEDGE_POSTGRES_MAJOR": "18", **changes})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid knowledge", result.stderr)

    def test_knowledge_configuration_does_not_read_bundle_at_import(self):
        result = self.run_import({"SAFEDBA_KNOWLEDGE_ENABLED": "true",
                                  "SAFEDBA_KNOWLEDGE_SCOPE": "team-a",
                                  "SAFEDBA_KNOWLEDGE_POSTGRES_MAJOR": "18",
                                  "SAFEDBA_KNOWLEDGE_PATH": "missing-review-bundle.json"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_skip_dotenv_does_not_read_the_local_file(self):
        environment = os.environ.copy()
        environment.update({
            "SAFEDBA_SKIP_DOTENV": "1", "SAFEDBA_ENV": "development",
            "SAFEDBA_DB_USER": "observer_test", "SAFEDBA_EXECUTOR_DB_USER": "executor_test",
            "SAFEDBA_TERMINATOR_DB_USER": "terminator_test",
        })
        code = IMPORT_CONFIG.replace("lambda *a, **k: None", "lambda *a, **k: print('UNEXPECTED_DOTENV_LOAD')")
        result = subprocess.run([sys.executable, "-B", "-c", code], env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNEXPECTED_DOTENV_LOAD", result.stdout)

    def test_unknown_environment_and_non_boolean_runtime_controls_are_rejected(self):
        for overrides, message in [
            ({"SAFEDBA_ENV": "prodution"}, "SAFEDBA_ENV must be"),
            ({"SAFEDBA_ENABLE_MUTATIONS": "sometimes"}, "Invalid boolean value"),
            ({"SAFEDBA_RUNTIME_CONTROLS_REQUIRED": "sometimes"}, "Invalid boolean value"),
        ]:
            with self.subTest(overrides=overrides):
                result = self.run_import(overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def run_import(self, overrides):
        environment = os.environ.copy()
        environment.update({
            "SAFEDBA_DB_USER": "observer_test",
            "SAFEDBA_EXECUTOR_DB_USER": "executor_test",
            "SAFEDBA_TERMINATOR_DB_USER": "terminator_test",
        })
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", IMPORT_CONFIG],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_non_finite_float_cannot_disable_cost_or_deadline_gate(self):
        for name, value in [
            ("SAFEDBA_DB_MAX_EXPLAIN_TOTAL_COST", "nan"),
            ("SAFEDBA_AGENT_DEADLINE_SECONDS", "inf"),
        ]:
            with self.subTest(name=name, value=value):
                result = self.run_import({name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "must be a finite number",
                    result.stderr,
                )

    def test_excessive_safety_budget_is_rejected(self):
        result = self.run_import({
            "SAFEDBA_DB_STATEMENT_TIMEOUT_MS": "999999999",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "exceed safety bounds",
            result.stderr,
        )

    def test_memory_retention_bounds_are_validated(self):
        result = self.run_import({
            "SAFEDBA_AGENT_MEMORY_MAX_SESSION_TURNS": "100000",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "exceed safety bounds",
            result.stderr,
        )

    def test_runtime_database_identities_must_be_distinct(self):
        result = self.run_import({
            "SAFEDBA_DB_USER": "same_user",
            "SAFEDBA_EXECUTOR_DB_USER": "same_user",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "must be three distinct identities",
            result.stderr,
        )

    def test_telemetry_configuration_is_bounded(self):
        for overrides, message in [
            (
                {"SAFEDBA_OTEL_ENDPOINT": "file:///tmp/traces"},
                "must be an HTTP(S) endpoint",
            ),
            (
                {"SAFEDBA_OTEL_EXPORT_TIMEOUT_SECONDS": "31"},
                "exceed safety bounds",
            ),
        ]:
            with self.subTest(overrides=overrides):
                result = self.run_import(overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_provider_resilience_configuration_is_fail_closed(self):
        cases = [
            (
                {"SAFEDBA_LLM_CIRCUIT_FAILURE_THRESHOLD": "0"},
                "must be positive",
            ),
            (
                {
                    "SAFEDBA_LLM_FALLBACK_PROVIDER": (
                        "openai_compatible"
                    ),
                },
                "Fallback provider configuration is incomplete",
            ),
            (
                {
                    "SAFEDBA_LLM_FALLBACK_PROVIDER": "unknown",
                    "SAFEDBA_LLM_FALLBACK_MODEL": "model",
                    "SAFEDBA_LLM_FALLBACK_API_KEY": "test-key",
                },
                "is unsupported",
            ),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                result = self.run_import(overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
