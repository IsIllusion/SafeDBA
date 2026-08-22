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


if __name__ == "__main__":
    unittest.main()
