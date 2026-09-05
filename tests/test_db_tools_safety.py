import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


EXECUTIONS = []
FETCHALL_ROWS = []
FETCHONE_ROWS = []
FETCHALL_BATCHES = []


class FakeCursor:
    def __init__(self):
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, params=None, **kwargs):
        EXECUTIONS.append((statement, params, kwargs))
        if isinstance(statement, str) and statement.startswith("EXPLAIN"):
            self.result = ([{"Plan": {"Total Cost": 1}}],)
        elif (
            isinstance(statement, str)
            and "WITH original_result AS" in statement
        ):
            self.result = (1, 1, 0, 0)

    def fetchone(self):
        if FETCHONE_ROWS:
            return FETCHONE_ROWS.pop(0)
        return self.result

    def fetchall(self):
        if FETCHALL_BATCHES:
            return list(FETCHALL_BATCHES.pop(0))
        return list(FETCHALL_ROWS)


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeCursor()


def load_db_tools():
    psycopg = ModuleType("psycopg")
    psycopg.connect = lambda **kwargs: FakeConnection()
    psycopg.sql = SimpleNamespace()

    config = ModuleType("config")
    config.DB_CONFIG = {"user": "observer"}
    config.DB_INCLUDE_OBSERVED_QUERY_TEXT = False
    config.EXECUTOR_DB_CONFIG = {"user": "executor"}
    config.TERMINATOR_DB_CONFIG = {"user": "terminator"}
    config.DB_LOCK_TIMEOUT_MS = 3_000
    config.DB_MAX_EXPLAIN_TOTAL_COST = 1_000_000.0
    config.DB_MAX_OBSERVATION_ROWS = 50
    config.DB_MAX_OBSERVED_QUERY_CHARS = 2_000
    config.DB_MAX_QUERY_LENGTH = 50_000
    config.DB_STATEMENT_TIMEOUT_MS = 30_000
    config.HEALTH_LONG_QUERY_SECONDS = 30.0
    config.HEALTH_LONG_TRANSACTION_SECONDS = 60.0

    stubs = {
        "psycopg": psycopg,
        "config": config,
    }
    previous = {
        name: sys.modules.get(name)
        for name in stubs
    }
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "db_tools_under_test",
            SRC / "db_tools.py",
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


DB_TOOLS = load_db_tools()


def identity(user, **overrides):
    value = {
        "user": user,
        "database": "benchmark",
        "superuser": False,
        "create_role": False,
        "create_database": False,
        "replication": False,
        "bypass_rls": False,
        "default_read_only": user != "executor",
        "temporary": False,
        "schema_create": user == "executor",
        "signal_backend": user == "terminator",
        "table_write_privilege": user == "executor",
    }
    value.update(overrides)
    return value


class DatabaseToolSafetyTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()
        FETCHALL_ROWS.clear()
        FETCHONE_ROWS.clear()
        FETCHALL_BATCHES.clear()

    def test_explain_forces_extended_single_statement_protocol(self):
        plan = DB_TOOLS.get_estimated_query_plan(
            "SELECT * FROM orders"
        )

        self.assertEqual(plan["Plan"]["Total Cost"], 1)
        explain_calls = [
            call
            for call in EXECUTIONS
            if isinstance(call[0], str)
            and call[0].startswith("EXPLAIN")
        ]
        self.assertEqual(len(explain_calls), 1)
        self.assertIsNone(explain_calls[0][1])
        self.assertTrue(explain_calls[0][2]["prepare"])

    def test_dynamic_rewrite_comparison_forces_extended_protocol(self):
        result = DB_TOOLS.compare_query_results(
            "SELECT 1 AS value",
            "SELECT 1 AS value",
        )

        self.assertTrue(result["equivalent"])
        comparison_calls = [
            call
            for call in EXECUTIONS
            if (
                isinstance(call[0], str)
                and "WITH original_result AS" in call[0]
            )
        ]
        self.assertEqual(len(comparison_calls), 1)
        self.assertTrue(comparison_calls[0][2]["prepare"])

    def test_runtime_identity_check_rejects_superuser_observer(self):
        identities = {
            "observer": identity(
                "observer",
                superuser=True,
            ),
            "executor": identity("executor"),
            "terminator": identity("terminator"),
        }

        with patch.object(
            DB_TOOLS,
            "_inspect_runtime_identity",
            side_effect=lambda config: identities[
                {
                    "observer": "observer",
                    "executor": "executor",
                    "terminator": "terminator",
                }[config["user"]]
            ],
        ):
            with self.assertRaises(RuntimeError):
                DB_TOOLS.verify_runtime_security()

    def test_runtime_identity_check_accepts_split_least_privilege_roles(self):
        identities = {
            "observer": identity("observer"),
            "executor": identity("executor"),
            "terminator": identity("terminator"),
        }

        with patch.object(
            DB_TOOLS,
            "_inspect_runtime_identity",
            side_effect=lambda config: identities[config["user"]],
        ):
            result = DB_TOOLS.verify_runtime_security()

        self.assertEqual(
            set(result),
            {"observer", "executor", "terminator"},
        )

    def test_operational_snapshot_combines_primary_evidence_safely(self):
        timestamp = datetime(
            2026,
            8,
            23,
            tzinfo=timezone.utc,
        )
        FETCHONE_ROWS.extend([
            ("benchmark", 10, 2, 0, 0, 0, None, None, None),
            ("benchmark", 100, 3, 2, 80, 60, 10, 5),
            (False,),
            (1_000_000, 4, 8192, 2, timestamp),
        ])
        FETCHALL_BATCHES.extend([
            [(
                "public",
                "orders",
                800,
                200,
                20.0,
                None,
                timestamp,
                0,
                4,
                None,
                timestamp,
                100_000,
                200_000_000,
                0.05,
            )],
            [(
                "standby-a",
                "streaming",
                "async",
                4096,
                0.1,
                0.2,
                0.3,
                timestamp,
                timestamp,
            )],
            [(
                "public",
                "orders",
                "r",
                700_000,
                200_000,
                900_000,
            )],
        ])

        result = DB_TOOLS.get_operational_snapshot()

        self.assertEqual(result["database"], "benchmark")
        self.assertEqual(
            result["connection_capacity"][
                "regular_connection_capacity"
            ],
            95,
        )
        self.assertEqual(
            result["connection_capacity"][
                "max_connection_utilization_pct"
            ],
            80.0,
        )
        self.assertEqual(
            result["vacuum"]["tables"][0]["table"],
            "orders",
        )
        self.assertEqual(
            result["replication"]["server_role"],
            "primary",
        )
        self.assertEqual(
            result["replication"]["standbys"][0][
                "wal_bytes_behind"
            ],
            4096,
        )
        self.assertEqual(
            result["storage_usage"]["database_bytes"],
            1_000_000,
        )
        self.assertFalse(
            result["storage_usage"][
                "filesystem_free_space_available"
            ]
        )
        statements = "\n".join(
            statement
            for statement, _, _ in EXECUTIONS
            if isinstance(statement, str)
        )
        self.assertIn(
            "current_setting('reserved_connections', true)",
            statements,
        )
        self.assertIn("FROM pg_stat_replication", statements)
        self.assertIn("pg_database_size", statements)
        self.assertNotIn("sender_host", statements)
        self.assertNotIn("conninfo", statements)

    def test_operational_snapshot_parses_standby_receiver(self):
        timestamp = datetime(
            2026,
            8,
            23,
            tzinfo=timezone.utc,
        )
        FETCHONE_ROWS.extend([
            ("benchmark", 1, 0, 0, 0, 0, None, None, None),
            ("benchmark", 100, 3, 0, 10, 8, 1, 0),
            (True,),
            (500_000, 0, 0, 0, None),
        ])
        FETCHALL_BATCHES.extend([
            [],
            [(
                "streaming",
                "replication_slot",
                "0/100",
                "0/100",
                "0/100",
                timestamp,
                timestamp,
                timestamp,
            )],
            [],
        ])

        result = DB_TOOLS.get_operational_snapshot()

        self.assertEqual(
            result["replication"]["server_role"],
            "standby",
        )
        receiver = result["replication"]["receivers"][0]
        self.assertEqual(receiver["status"], "streaming")
        self.assertEqual(receiver["slot_name"], "replication_slot")

    def test_lock_graph_snapshot_is_complete_scoped_and_identity_rich(self):
        timestamp = datetime(
            2026,
            8,
            14,
            tzinfo=timezone.utc,
        )
        FETCHALL_ROWS.append((
            101,
            "app",
            "benchmark",
            "worker",
            "active",
            "Lock",
            "transactionid",
            "SELECT blocked",
            timestamp,
            timestamp,
            timestamp,
            5.0,
            6.0,
            202,
            "app",
            "worker",
            "benchmark",
            "client backend",
            "idle in transaction",
            "Client",
            "ClientRead",
            "SELECT blocker",
            timestamp,
            timestamp,
            timestamp,
            30.0,
            [],
        ))

        result = DB_TOOLS.get_lock_graph_snapshot()

        self.assertFalse(result["truncated"])
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(
            result["rows"][0]["blocker_database_name"],
            "benchmark",
        )
        self.assertEqual(
            result["rows"][0]["blocker_backend_type"],
            "client backend",
        )
        lock_calls = [
            call
            for call in EXECUTIONS
            if (
                isinstance(call[0], str)
                and "pg_blocking_pids" in call[0]
            )
        ]
        self.assertEqual(lock_calls[-1][1], (51,))
        self.assertIn(
            "blocked.datname = current_database()",
            lock_calls[-1][0],
        )
        self.assertIn(
            "blocker.datname = current_database()",
            lock_calls[-1][0],
        )

    def test_lock_graph_snapshot_detects_limit_truncation(self):
        timestamp = datetime(
            2026,
            8,
            14,
            tzinfo=timezone.utc,
        )
        row = (
            101, "app", "benchmark", "worker", "active",
            "Lock", "transactionid", "SELECT blocked",
            timestamp, timestamp, timestamp, 5.0, 6.0,
            202, "app", "worker", "benchmark", "client backend",
            "idle in transaction", "Client", "ClientRead",
            "SELECT blocker", timestamp, timestamp, timestamp, 30.0, [],
        )
        FETCHALL_ROWS.extend([row] * 51)

        result = DB_TOOLS.get_lock_graph_snapshot()

        self.assertTrue(result["truncated"])
        self.assertEqual(result["row_count"], 50)
        self.assertEqual(len(result["rows"]), 50)

    def test_termination_sql_binds_both_backend_identities(self):
        blocked_backend_start = "2026-08-14T10:00:00+00:00"
        blocked_xact_start = "2026-08-14T10:00:01+00:00"
        blocker_backend_start = "2026-08-14T09:00:00+00:00"
        blocker_xact_start = "2026-08-14T09:00:01+00:00"

        result = DB_TOOLS.terminate_blocking_backend(
            blocked_pid=101,
            blocker_pid=202,
            blocker_backend_start=blocker_backend_start,
            blocker_xact_start=blocker_xact_start,
            blocked_backend_start=blocked_backend_start,
            blocked_xact_start=blocked_xact_start,
        )

        self.assertFalse(result["final_validation_passed"])
        terminate_calls = [
            call
            for call in EXECUTIONS
            if (
                isinstance(call[0], str)
                and "pg_terminate_backend" in call[0]
            )
        ]
        self.assertEqual(len(terminate_calls), 1)
        statement, params, _ = terminate_calls[0]
        self.assertIn(
            "blocked.backend_start = %s::timestamptz",
            statement,
        )
        self.assertIn(
            "blocked.xact_start = %s::timestamptz",
            statement,
        )
        self.assertEqual(
            params,
            (
                202,
                101,
                blocked_backend_start,
                blocked_xact_start,
                blocker_backend_start,
                blocker_xact_start,
                5000,
            ),
        )


if __name__ == "__main__":
    unittest.main()
