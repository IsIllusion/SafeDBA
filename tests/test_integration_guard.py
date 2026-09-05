from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import integration_guard


class IntegrationTargetTests(unittest.TestCase):
    def setUp(self):
        self.instance_id = str(uuid.uuid4())
        self.configs = [dict(host="127.0.0.1", port=54567, dbname="benchmark", user="safedba_" + role) for role in ("observer", "executor", "terminator")]
        self.row = (self.instance_id, "DISPOSABLE_SAFEDBA_INTEGRATION", "benchmark", "safedba_observer", "safedba_bootstrap", "safedba_bootstrap")

    def test_missing_instance_id_never_connects(self):
        with patch.object(integration_guard.psycopg, "connect") as connect:
            for marker in (None, "", "not-a-uuid"):
                with self.assertRaises(RuntimeError):
                    integration_guard.verify_disposable_target(*self.configs, marker)
            connect.assert_not_called()

    def test_remote_wrong_database_role_or_split_endpoints_never_connect(self):
        for index, change in [
            (0, {"host": "production.example"}), (1, {"dbname": "business"}),
            (2, {"port": 54568}), (1, {"user": "postgres"}),
            (0, {"hostaddr": "203.0.113.2"}), (0, {"service": "production"}),
            (0, {"port": "invalid"}), (0, {"port": 0}),
        ]:
            with self.subTest(change=change):
                configs = deepcopy(self.configs)
                configs[index].update(change)
                with patch.object(integration_guard.psycopg, "connect") as connect:
                    with self.assertRaises(RuntimeError):
                        integration_guard.verify_disposable_target(*configs, self.instance_id)
                    connect.assert_not_called()

    def test_only_exact_single_bootstrap_owned_marker_passes(self):
        variants = [[], [self.row, self.row], [(str(uuid.uuid4()), *self.row[1:])], [(*self.row[:-1], "safedba_executor")], [(*self.row[:4], "safedba_executor", self.row[5])]]
        with patch.object(integration_guard.psycopg, "connect") as connect:
            rows = connect.return_value.__enter__.return_value.execute.return_value.fetchall
            for value in variants:
                rows.return_value = value
                with self.assertRaises(RuntimeError):
                    integration_guard.verify_disposable_target(*self.configs, self.instance_id)
            rows.return_value = [self.row]
            self.assertEqual(integration_guard.verify_disposable_target(*self.configs, self.instance_id), self.instance_id)


class IsolatedRunnerEnvironmentTests(unittest.TestCase):
    def test_runner_overrides_inherited_secrets_targets_and_live_provider(self):
        spec = importlib.util.spec_from_file_location("pg_runner_under_test", ROOT / "scripts/run_postgres_integration.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with patch.dict(os.environ, {
            "SAFEDBA_DB_HOST": "production.example", "SAFEDBA_LLM_API_KEY": "private-secret",
            "SAFEDBA_LLM_FALLBACK_API_KEY": "private-fallback", "SAFEDBA_RUNTIME_CONTROLS_PATH": "wrong-path",
            "PGSERVICE": "production", "PGHOSTADDR": "203.0.113.1",
        }):
            env = runner.isolated_environment(54321, Path("test-output"), str(uuid.uuid4()))
        self.assertEqual(env["SAFEDBA_SKIP_DOTENV"], "1")
        self.assertEqual(env["SAFEDBA_DB_HOST"], "127.0.0.1")
        self.assertEqual(env["SAFEDBA_LLM_BASE_URL"], "http://127.0.0.1:1/v1")
        self.assertNotIn("SAFEDBA_LLM_FALLBACK_API_KEY", env)
        self.assertNotIn("PGSERVICE", env)
        self.assertNotIn("private-secret", str(env))


if __name__ == "__main__":
    unittest.main()
