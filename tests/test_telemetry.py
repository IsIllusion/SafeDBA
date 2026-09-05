from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from telemetry import TelemetryManager
from incident_workflow import (
    create_lock_incident,
    run_lock_incident,
)
from workflow_store import SQLiteIncidentStore
from tests.test_incident_workflow import (
    LockEnvironment,
    lock_row,
    proposal,
)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(
            SimpleSpanProcessor(self.exporter)
        )
        self.manager = TelemetryManager(
            tracer=self.provider.get_tracer("safedba.tests"),
            enabled=True,
            provider=self.provider,
        )

    def tearDown(self):
        self.manager.shutdown()

    def test_run_and_child_span_share_trace_and_export_safe_metadata(self):
        run = self.manager.start_run(
            mode="diagnose",
            memory_enabled=True,
            experience_enabled=False,
            environment_verified=True,
        )
        with run.span(
            "safedba.tool.call",
            {
                "safedba.tool.name": "get_database_health",
                "safedba.tool.risk": "READ_ONLY",
            },
        ):
            pass
        run.finish(
            status="completed",
            stop_reason="assistant_response",
            llm_turns=1,
            tool_calls_attempted=1,
            tool_calls_succeeded=1,
            total_tokens=25,
            error_count=0,
        )

        spans = self.exporter.get_finished_spans()
        self.assertEqual(2, len(spans))
        by_name = {span.name: span for span in spans}
        root = by_name["safedba.agent.run"]
        child = by_name["safedba.tool.call"]
        self.assertEqual(root.context.trace_id, child.context.trace_id)
        self.assertEqual(
            f"{root.context.trace_id:032x}",
            run.trace_id,
        )
        self.assertEqual(
            "completed",
            root.attributes["safedba.run.status"],
        )
        attribute_names = {
            name
            for span in spans
            for name in span.attributes
        }
        for forbidden in (
            "prompt",
            "sql",
            "arguments",
            "result",
            "session_id",
            "thread_id",
        ):
            self.assertFalse(
                any(forbidden in name.lower() for name in attribute_names)
            )

    def test_exception_message_is_not_exported(self):
        run = self.manager.start_run(
            mode="diagnose",
            memory_enabled=False,
            experience_enabled=False,
            environment_verified=False,
        )
        secret_message = "SELECT secret_value FROM private_table"
        with self.assertRaises(RuntimeError):
            with run.span("safedba.llm.complete"):
                raise RuntimeError(secret_message)
        run.finish(
            status="failed",
            stop_reason="provider_error",
            llm_turns=0,
            tool_calls_attempted=0,
            tool_calls_succeeded=0,
            total_tokens=0,
            error_count=1,
        )

        exported = self.exporter.get_finished_spans()
        serialized = repr(exported)
        self.assertNotIn(secret_message, serialized)
        child = next(
            span
            for span in exported
            if span.name == "safedba.llm.complete"
        )
        self.assertEqual("RuntimeError", child.attributes["error.type"])
        self.assertEqual((), child.events)

    def test_lock_workflow_emits_correlated_privacy_safe_spans(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIncidentStore(
                Path(directory) / "incidents.sqlite3"
            )
            incident = create_lock_incident(
                proposals=[proposal(row)],
                user_request="Sensitive operator request",
                store=store,
                observe_locks=environment.observe,
            )
            result = run_lock_incident(
                incident["incident_id"],
                store=store,
                observe_locks=environment.observe,
                execute_action=environment.execute,
                approval_decider=lambda _: {
                    "approved": True,
                    "actor": "sensitive-operator-name",
                },
                telemetry_manager=self.manager,
            )

        self.assertEqual("COMPLETED", result["state"])
        self.assertIsNotNone(result["trace_id"])
        spans = self.exporter.get_finished_spans()
        names = [span.name for span in spans]
        self.assertIn("safedba.incident.run", names)
        self.assertIn("safedba.incident.approval", names)
        self.assertIn("safedba.incident.execute", names)
        self.assertGreaterEqual(
            names.count("safedba.incident.observe"),
            3,
        )
        trace_ids = {span.context.trace_id for span in spans}
        self.assertEqual(1, len(trace_ids))
        serialized = repr(spans)
        for forbidden in (
            incident["incident_id"],
            "sensitive-operator-name",
            "Sensitive operator request",
            "benchmark",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_untrusted_executor_status_is_not_exported(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        original_execute = environment.execute
        secret_status = "SELECT secret_value FROM private_table"

        def untrusted_execute(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            result["status"] = secret_status
            return result

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIncidentStore(
                Path(directory) / "incidents.sqlite3"
            )
            incident = create_lock_incident(
                proposals=[proposal(row)],
                user_request="resolve",
                store=store,
                observe_locks=environment.observe,
            )
            run_lock_incident(
                incident["incident_id"],
                store=store,
                observe_locks=environment.observe,
                execute_action=untrusted_execute,
                approval_decider=lambda _: True,
                telemetry_manager=self.manager,
            )

        spans = self.exporter.get_finished_spans()
        serialized = repr(spans)
        self.assertNotIn(secret_status, serialized)
        execution = next(
            span
            for span in spans
            if span.name == "safedba.incident.execute"
        )
        self.assertEqual(
            "UNRECOGNIZED",
            execution.attributes["safedba.action.status"],
        )


if __name__ == "__main__":
    unittest.main()
