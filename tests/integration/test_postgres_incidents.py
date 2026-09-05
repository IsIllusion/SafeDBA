"""Actual PostgreSQL locks and mutations, with no paid model calls.

Only the model/fault injector is scripted. Observations, approvals, execution
claims, pg_terminate_backend, persistence and postconditions use real code.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import psycopg
import audit
import config
import db_tools
import executor
from agent import run_agent
from integration_guard import verify_disposable_target
from incident_workflow import create_lock_incident, run_lock_incident
from workflow_store import SQLiteIncidentStore

RUN_INTEGRATION = os.getenv("SAFEDBA_RUN_POSTGRES_INTEGRATION", "").lower() in {"1", "true", "yes", "on"}


class LockFixture:
    def __init__(self):
        self.blockers = []
        self.waiters = []
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.tag = "safedba-integration-" + uuid.uuid4().hex[:12]

    def connection(self):
        return psycopg.connect(
            **config.EXECUTOR_DB_CONFIG, application_name=self.tag,
            options="-c statement_timeout=30000 -c lock_timeout=30000",
        )

    def add_blocker(self, row_id):
        connection = self.connection()
        self.blockers.append(connection)
        connection.execute("SELECT id FROM public.integration_probe WHERE id = %s FOR UPDATE", (row_id,)).fetchone()
        return connection

    def add_waiter(self, row_id):
        connection = self.connection()

        def wait():
            result = connection.execute("SELECT id FROM public.integration_probe WHERE id = %s FOR UPDATE", (row_id,)).fetchone()
            connection.rollback()
            return result

        future = self.pool.submit(wait)
        self.waiters.append((connection, future))
        return connection

    def wait_for(self, count):
        deadline = time.monotonic() + 8
        blocker_ids = {connection.info.backend_pid for connection in self.blockers}
        waiter_ids = {connection.info.backend_pid for connection, _ in self.waiters}
        while time.monotonic() < deadline:
            snapshot = db_tools.get_lock_graph_snapshot()
            rows = [row for row in snapshot["rows"] if row["blocker_pid"] in blocker_ids and row["blocked_pid"] in waiter_ids]
            if len(rows) == count:
                return rows
            time.sleep(0.05)
        raise AssertionError(f"Expected {count} synthetic lock relationships; observed {len(rows)}")

    def close(self):
        for connection in self.blockers:
            connection.close()
        for connection, future in self.waiters:
            try:
                future.result(timeout=5)
            except Exception:
                pass
            finally:
                connection.close()
        self.pool.shutdown(wait=True)


def proposal(row):
    return {
        "type": "TERMINATE_BACKEND", "risk": "HIGH", "confidence": 0.95,
        "reason": "Synthetic integration fixture: idle transaction blocks a waiter.",
        **{key: row[key] for key in ("blocked_pid", "blocker_pid", "blocker_backend_start", "blocker_xact_start")},
    }


@unittest.skipUnless(RUN_INTEGRATION, "Opt in to the disposable PostgreSQL integration suite.")
class PostgreSQLIncidentIntegrationTests(unittest.TestCase):
    def setUp(self):
        verify_disposable_target(config.DB_CONFIG, config.EXECUTOR_DB_CONFIG, config.TERMINATOR_DB_CONFIG, os.getenv("SAFEDBA_TEST_INSTANCE_ID"))
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directory = Path(self.contexts.enter_context(tempfile.TemporaryDirectory(prefix="safedba-incident-test-")))
        self.audit_path = directory / "audit.jsonl"
        self.controls = directory / "controls.json"
        self.controls.write_text('{"version":1}', encoding="utf-8")
        self.contexts.enter_context(patch.object(audit, "AUDIT_LOG_PATH", self.audit_path))
        self.contexts.enter_context(patch.object(config, "RUNTIME_CONTROLS_PATH", self.controls))
        self.store = SQLiteIncidentStore(directory / "incidents.sqlite3")
        self.contexts.enter_context(patch.object(executor, "incident_store_factory", lambda: self.store))
        self.locks = LockFixture()
        self.addCleanup(self.locks.close)

    def create(self, count=1):
        for row_id in range(1, count + 1):
            self.locks.add_blocker(row_id)
            self.locks.add_waiter(row_id)
        rows = self.locks.wait_for(count)
        return create_lock_incident(proposals=[proposal(rows[0])], user_request="Resolve the disposable lock fixture", expand_all_actionable=True, store=self.store)

    def run_workflow(self, incident, *, decision=True, execute_action=None):
        approvals = []

        def approve(value):
            approvals.append(value)
            return decision(value) if callable(decision) else decision

        kwargs = {"execute_action": execute_action} if execute_action else {}
        with redirect_stdout(io.StringIO()):
            result = run_lock_incident(incident["incident_id"], store=self.store, approval_decider=approve, **kwargs)
        return result, approvals

    def test_three_real_blockers_complete_with_one_approval(self):
        incident = self.create(3)
        self.assertEqual(len(incident["actions"]), 3)
        result, approvals = self.run_workflow(incident)
        self.assertEqual(result["state"], "COMPLETED", result.get("last_error"))
        self.assertEqual(len(approvals), 1)
        self.assertEqual([action["state"] for action in result["actions"]], ["SUCCEEDED"] * 3)
        for connection, future in self.locks.waiters:
            self.assertIsNotNone(future.result(timeout=5))
            self.assertFalse(connection.closed)
        self.assertEqual(db_tools.get_lock_graph_snapshot()["rows"], [])
        self.assertTrue(audit.verify_audit_log(self.audit_path)["valid"])
        resumed, approvals = self.run_workflow(result)
        self.assertEqual(resumed["state"], "COMPLETED")
        self.assertEqual(approvals, [])

    def test_rejected_approval_leaves_all_real_blockers_alive(self):
        result, approvals = self.run_workflow(self.create(3), decision=False)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(len(self.locks.wait_for(3)), 3)
        self.assertTrue(all(action["attempt_count"] == 0 for action in result["actions"]))

    def test_changed_transaction_identity_cannot_terminate_same_pid(self):
        self.create()
        old = self.locks.wait_for(1)[0]
        blocker = self.locks.blockers[0]
        blocker.rollback()
        self.locks.waiters[0][1].result(timeout=5)
        # Same backend, genuinely new transaction; approval for the old
        # transaction must not authorize this replacement blocker.
        blocker.execute("SELECT id FROM public.integration_probe WHERE id = 1 FOR UPDATE").fetchone()
        self.locks.add_waiter(1)
        row = self.locks.wait_for(1)[0]
        self.assertEqual(row["blocker_pid"], old["blocker_pid"])
        self.assertNotEqual(row["blocker_xact_start"], old["blocker_xact_start"])
        result = db_tools.terminate_blocking_backend(
                blocked_pid=row["blocked_pid"], blocker_pid=row["blocker_pid"],
                blocker_backend_start=row["blocker_backend_start"],
                blocker_xact_start=old["blocker_xact_start"],
                blocked_backend_start=row["blocked_backend_start"], blocked_xact_start=row["blocked_xact_start"],
        )
        self.assertFalse(result["final_validation_passed"])
        self.assertFalse(result["terminated"])
        self.assertEqual(len(self.locks.wait_for(1)), 1)

    def test_audit_corruption_blocks_real_termination(self):
        incident = self.create()
        self.audit_path.write_text('{"broken":', encoding="utf-8")
        result, _ = self.run_workflow(incident)
        self.assertNotEqual(result["state"], "COMPLETED")
        self.assertEqual(len(self.locks.wait_for(1)), 1)

    def test_stop_after_first_real_termination_requires_reapproval_for_rest(self):
        incident = self.create(3)
        calls = []

        def execute(value, **kwargs):
            result = executor.execute_action_proposal(value, **kwargs)
            calls.append(result)
            if len(calls) == 1:
                self.controls.write_text('{"version":1,"disabled_actions":["TERMINATE_BACKEND"]}', encoding="utf-8")
            return result

        result, approvals = self.run_workflow(incident, execute_action=execute)
        self.assertEqual(result["state"], "AWAITING_REAPPROVAL")
        self.assertEqual(len(approvals), 1)
        self.assertEqual([action["state"] for action in result["actions"]], ["SUCCEEDED", "PLANNED", "PLANNED"])
        self.assertEqual(len(self.locks.wait_for(2)), 2)
        self.controls.write_text('{"version":1}', encoding="utf-8")
        result, approvals = self.run_workflow(result)
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(len(approvals), 1)
        self.assertTrue(audit.verify_audit_log(self.audit_path)["valid"])

    def test_lost_result_after_real_termination_is_reconciled_without_retry(self):
        incident = self.create()

        def interrupt(value, **kwargs):
            executor.execute_action_proposal(value, **kwargs)
            raise SystemExit("Injected interruption after DB effect, before workflow result checkpoint")

        with self.assertRaises(SystemExit):
            self.run_workflow(incident, execute_action=interrupt)
        stored = self.store.load_incident(incident["incident_id"])
        self.assertEqual(stored["actions"][0]["state"], "APPLYING")

        def must_not_retry(*args, **kwargs):
            raise AssertionError("An ambiguous action must never be retried")

        result, _ = self.run_workflow(stored, execute_action=must_not_retry)
        self.assertEqual(result["actions"][0]["state"], "RECONCILED_RESOLVED")
        self.assertEqual(db_tools.get_lock_graph_snapshot()["rows"], [])

    def test_index_creation_and_identity_bound_cleanup_use_real_catalog(self):
        index_name = "integration_probe_payload_test_idx"
        identity = db_tools.create_index("integration_probe", "payload", index_name)
        try:
            with self.assertRaisesRegex(RuntimeError, "catalog identity"):
                db_tools.drop_index(index_name, expected_index_oid=identity["index_oid"] + 1, expected_table_oid=identity["table_oid"])
            self.assertTrue(any(row["index_name"] == index_name for row in db_tools.get_indexes("integration_probe")))
        finally:
            db_tools.drop_index(index_name, expected_index_oid=identity["index_oid"], expected_table_oid=identity["table_oid"])
        self.assertFalse(any(row["index_name"] == index_name for row in db_tools.get_indexes("integration_probe")))

    def test_scripted_agent_collects_real_evidence_without_executing_actions(self):
        self.create()

        class ScriptedProvider:
            model = "scripted-control-flow-not-an-LLM"

            def __init__(self):
                self.calls = 0

            def complete(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    message = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="locks-1", function=SimpleNamespace(name="get_lock_waits", arguments="{}"))])
                else:
                    message = SimpleNamespace(content="Observed lock evidence [ev-0001]. No database modification requested.", tool_calls=[])
                return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if self.calls == 1 else "stop")], usage=None)

            @staticmethod
            def assistant_message_to_dict(message):
                value = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    value["tool_calls"] = [{"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}} for call in message.tool_calls]
                return value

        result = run_agent("Observe this lock incident, diagnosis only", provider=ScriptedProvider(), use_memory=False, capture_experience=False)
        self.assertEqual(result["status"], "completed", result.get("errors"))
        self.assertEqual(result["proposals"], [])
        self.assertTrue(any(item["tool"] == "get_lock_waits" for item in result["tool_trace"]))
        self.assertEqual(len(self.locks.wait_for(1)), 1)


if __name__ == "__main__":
    unittest.main()
