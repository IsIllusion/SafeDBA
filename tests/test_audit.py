import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch


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


# Separate processes exercise OS lock ownership (not an in-process mock).
AUDIT_WORKER = r'''
import os, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
config = types.ModuleType("config")
config.AUDIT_INCLUDE_QUERY_TEXT = False
config.AUDIT_LOG_PATH = Path(sys.argv[2])
sys.modules["config"] = config
import audit
audit._LOCK_TIMEOUT_SECONDS = 0.2 if sys.argv[3] == "probe" else 5.0
if sys.argv[3] == "append":
    for number in range(12):
        audit.write_audit_log({"worker": sys.argv[4], "number": number})
elif sys.argv[3] == "crash":
    with audit._exclusive_audit_lock(config.AUDIT_LOG_PATH):
        os._exit(0)
else:
    try:
        with audit._exclusive_audit_lock(config.AUDIT_LOG_PATH):
            print("acquired")
    except audit.AuditIntegrityError:
        print("blocked")
'''


class AuditTests(unittest.TestCase):
    def worker_args(self, path, mode, worker="0"):
        return [sys.executable, "-B", "-c", AUDIT_WORKER, str(SRC), str(path), mode, worker]

    def test_live_lock_is_not_stolen_even_with_ancient_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with AUDIT._exclusive_audit_lock(path):
                os.utime(AUDIT._lock_path(path), (1, 1))
                probe = subprocess.run(self.worker_args(path, "probe"), capture_output=True, text=True, timeout=10)
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertEqual(probe.stdout.strip(), "blocked")
            self.assertTrue(AUDIT._lock_path(path).exists())
            # The persistent sidecar itself is not an outstanding lock.
            with AUDIT._exclusive_audit_lock(path):
                pass

    def test_process_crash_releases_lock_without_stale_file_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            crashed = subprocess.run(self.worker_args(path, "crash"), capture_output=True, text=True, timeout=10)
            self.assertEqual(crashed.returncode, 0, crashed.stderr)
            self.assertTrue(AUDIT._lock_path(path).exists())
            with patch.object(AUDIT, "_LOCK_TIMEOUT_SECONDS", 0.2):
                with AUDIT._exclusive_audit_lock(path):
                    pass

    def test_concurrent_processes_produce_one_contiguous_audit_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            processes = []
            try:
                for worker in range(4):
                    processes.append(subprocess.Popen(
                        self.worker_args(path, "append", str(worker)),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    ))
                for process in processes:
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, stderr or stdout)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
            verification = AUDIT.verify_audit_log(path)
            self.assertTrue(verification["valid"], verification)
            self.assertEqual(verification["chained_records"], 48)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len({(r["worker"], r["number"]) for r in records}), 48)

    def test_verifier_uses_writer_lock_to_avoid_reading_partial_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with AUDIT._exclusive_audit_lock(path), patch.object(AUDIT, "_LOCK_TIMEOUT_SECONDS", 0.1):
                with self.assertRaises(AUDIT.AuditIntegrityError):
                    AUDIT.verify_audit_log(path)

    def test_ambiguous_json_and_partial_lines_block_verification_and_append(self):
        cases = [
            ('{"status":"a","status":"b"}\n', "MALFORMED_AUDIT_RECORD"),
            ('{"value":NaN}\n', "MALFORMED_AUDIT_RECORD"),
            ('{"value":1e999}\n', "MALFORMED_AUDIT_RECORD"),
            ('{"_audit":null}\n', "MALFORMED_AUDIT_ENVELOPE"),
            ('{"_audit":{}}\n', "MALFORMED_AUDIT_ENVELOPE"),
            ('{"status":"complete_json_but_incomplete_line"}', "INCOMPLETE_AUDIT_LINE"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch.object(AUDIT, "AUDIT_LOG_PATH", path):
                for raw, expected in cases:
                    with self.subTest(raw=raw):
                        path.write_text(raw, encoding="utf-8")
                        before = path.read_bytes()
                        result = AUDIT.verify_audit_log(path)
                        self.assertFalse(result["valid"])
                        self.assertEqual(result["error"]["type"], expected)
                        with self.assertRaises(AUDIT.AuditIntegrityError):
                            AUDIT.write_audit_log({"status": "new"})
                        self.assertEqual(path.read_bytes(), before)

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

    def test_records_form_a_verifiable_hash_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            AUDIT.AUDIT_LOG_PATH = Path(directory) / "audit.jsonl"
            AUDIT.write_audit_log({"status": "INTENT"})
            AUDIT.write_audit_log({"status": "SUCCESS"})

            verification = AUDIT.verify_audit_log(
                AUDIT.AUDIT_LOG_PATH
            )
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["records"], 2)
            self.assertEqual(verification["chained_records"], 2)
            records = [
                json.loads(line)
                for line in AUDIT.AUDIT_LOG_PATH.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(records[0]["_audit"]["sequence"], 1)
            self.assertEqual(records[1]["_audit"]["sequence"], 2)
            self.assertEqual(
                records[1]["_audit"]["previous_hash"],
                records[0]["_audit"]["record_hash"],
            )

    def test_tampering_is_detected_and_future_append_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            AUDIT.AUDIT_LOG_PATH = Path(directory) / "audit.jsonl"
            AUDIT.write_audit_log({"status": "INTENT"})
            AUDIT.write_audit_log({"status": "SUCCESS"})
            records = [
                json.loads(line)
                for line in AUDIT.AUDIT_LOG_PATH.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            records[0]["status"] = "TAMPERED"
            AUDIT.AUDIT_LOG_PATH.write_text(
                "\n".join(json.dumps(item) for item in records) + "\n",
                encoding="utf-8",
            )

            verification = AUDIT.verify_audit_log(
                AUDIT.AUDIT_LOG_PATH
            )
            self.assertFalse(verification["valid"])
            self.assertEqual(
                verification["error"]["type"],
                "AUDIT_RECORD_HASH_MISMATCH",
            )
            with self.assertRaises(AUDIT.AuditIntegrityError):
                AUDIT.write_audit_log({"status": "ANOTHER_ACTION"})

    def test_existing_legacy_log_is_anchored_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            AUDIT.AUDIT_LOG_PATH = Path(directory) / "audit.jsonl"
            legacy = {"status": "LEGACY"}
            AUDIT.AUDIT_LOG_PATH.write_text(
                json.dumps(legacy) + "\n",
                encoding="utf-8",
            )
            AUDIT.write_audit_log({"status": "CHAINED"})

            verification = AUDIT.verify_audit_log(
                AUDIT.AUDIT_LOG_PATH
            )
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["legacy_records"], 1)
            self.assertEqual(verification["chained_records"], 1)
            chained = json.loads(
                AUDIT.AUDIT_LOG_PATH.read_text(
                    encoding="utf-8"
                ).splitlines()[1]
            )
            self.assertEqual(
                chained["_audit"]["anchor"],
                "legacy_tail_sha256",
            )


if __name__ == "__main__":
    unittest.main()
