import importlib.util
import json
from pathlib import Path
from types import ModuleType
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def load_audit():
    config = ModuleType("config")
    config.AUDIT_INCLUDE_QUERY_TEXT = False
    config.AUDIT_LOG_PATH = Path("unused.jsonl")
    prior = sys.modules.get("config")
    sys.modules["config"] = config

    try:
        spec = importlib.util.spec_from_file_location(
            "audit_under_test",
            SRC / "audit.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if prior is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = prior

    return module


AUDIT = load_audit()


class AuditTests(unittest.TestCase):
    def test_sanitizer_redacts_queries_secrets_and_data_distribution(self):
        sanitized = AUDIT.sanitize_audit_value({
            "query": "SELECT * FROM customers WHERE id = 42",
            "password": "do-not-store",
            "most_common_vals": ["customer@example.com"],
        })
        self.assertNotIn(
            "customers",
            json.dumps(sanitized),
        )
        self.assertEqual(
            sanitized["password"],
            "[REDACTED]",
        )
        self.assertTrue(
            sanitized["most_common_vals"]["redacted"]
        )

    def test_write_produces_strict_json_line(self):
        with tempfile.TemporaryDirectory() as directory:
            AUDIT.AUDIT_LOG_PATH = (
                Path(directory) / "audit.jsonl"
            )
            AUDIT.write_audit_log({
                "query": "SELECT 1",
                "status": "SUCCESS",
            })
            line = AUDIT.AUDIT_LOG_PATH.read_text(
                encoding="utf-8"
            )
            decoded = json.loads(line)
            self.assertEqual(
                decoded["status"],
                "SUCCESS",
            )
            self.assertTrue(
                decoded["query"]["redacted"]
            )

    def test_non_finite_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            AUDIT.AUDIT_LOG_PATH = (
                Path(directory) / "audit.jsonl"
            )
            with self.assertRaises(ValueError):
                AUDIT.write_audit_log({
                    "value": float("nan")
                })


if __name__ == "__main__":
    unittest.main()
