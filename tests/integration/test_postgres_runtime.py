import os
from pathlib import Path
import sys
import unittest


RUN_INTEGRATION = (
    os.getenv("SAFEDBA_RUN_POSTGRES_INTEGRATION", "").strip().lower()
    in {"1", "true", "yes", "on"}
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import psycopg

import db_tools
from integration_guard import verify_disposable_target


@unittest.skipUnless(
    RUN_INTEGRATION,
    "Set SAFEDBA_RUN_POSTGRES_INTEGRATION=1 for live PostgreSQL tests.",
)
class PostgreSQLRuntimeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        verify_disposable_target(
            db_tools.DB_CONFIG, db_tools.EXECUTOR_DB_CONFIG,
            db_tools.TERMINATOR_DB_CONFIG, os.getenv("SAFEDBA_TEST_INSTANCE_ID"),
        )

    def test_runtime_roles_match_least_privilege_contract(self):
        identities = db_tools.verify_runtime_security()

        self.assertEqual(
            {
                "observer": "safedba_observer",
                "executor": "safedba_executor",
                "terminator": "safedba_terminator",
            },
            {
                label: identity["user"]
                for label, identity in identities.items()
            },
        )
        self.assertTrue(identities["observer"]["default_read_only"])
        self.assertFalse(identities["observer"]["table_write_privilege"])
        self.assertTrue(identities["executor"]["schema_create"])
        self.assertFalse(identities["executor"]["signal_backend"])
        self.assertTrue(identities["terminator"]["signal_backend"])
        self.assertFalse(identities["terminator"]["table_write_privilege"])

    def test_observer_role_rejects_data_mutation(self):
        with psycopg.connect(
            **db_tools.DB_CONFIG,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW default_transaction_read_only")
                self.assertEqual("on", cursor.fetchone()[0])
                with self.assertRaises(
                    psycopg.errors.ReadOnlySqlTransaction
                ):
                    cursor.execute(
                        "INSERT INTO public.integration_probe "
                        "(id, payload) VALUES (100, 'must_fail')"
                    )

    def test_plan_and_index_tools_use_real_postgresql(self):
        plan = db_tools.get_estimated_query_plan(
            "SELECT * FROM public.integration_probe WHERE id = 1"
        )
        indexes = db_tools.get_indexes("integration_probe")

        self.assertIn("Plan", plan)
        self.assertIsInstance(plan["Plan"].get("Total Cost"), float)
        self.assertTrue(
            any(
                item["index_name"] == "integration_probe_pkey"
                for item in indexes
            )
        )

    def test_database_health_returns_typed_snapshot(self):
        health = db_tools.get_database_health()

        self.assertEqual("benchmark", health["database"])
        for key in (
            "total_client_sessions",
            "active_sessions",
            "blocked_sessions",
            "long_running_queries",
        ):
            self.assertIsInstance(health[key], int)
            self.assertGreaterEqual(health[key], 0)

    def test_operational_snapshot_covers_capacity_vacuum_and_storage(self):
        snapshot = db_tools.get_operational_snapshot()

        self.assertEqual("benchmark", snapshot["database"])
        self.assertEqual(
            "primary",
            snapshot["replication"]["server_role"],
        )
        self.assertGreaterEqual(
            snapshot["connection_capacity"]["max_connections"],
            1,
        )
        self.assertGreater(
            snapshot["storage_usage"]["database_bytes"],
            0,
        )
        self.assertFalse(
            snapshot["storage_usage"][
                "filesystem_free_space_available"
            ]
        )
        observed_tables = {
            item["table"]
            for item in snapshot["vacuum"]["tables"]
        }
        self.assertIn("integration_probe", observed_tables)

    def test_statement_timeout_cancels_slow_observation(self):
        original_timeout = db_tools.DB_STATEMENT_TIMEOUT_MS
        db_tools.DB_STATEMENT_TIMEOUT_MS = 100
        try:
            with self.assertRaises(psycopg.errors.QueryCanceled):
                with db_tools.readonly_connection() as connection:
                    connection.execute("SELECT pg_sleep(1)")
        finally:
            db_tools.DB_STATEMENT_TIMEOUT_MS = original_timeout


if __name__ == "__main__":
    unittest.main()
