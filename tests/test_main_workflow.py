import importlib.util
import io
import sys
import unittest

from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def load_main():
    agent = ModuleType("agent")
    agent.run_agent = lambda message, mode: {}
    agent.review_execution_result = lambda **kwargs: "review"

    executor = ModuleType("executor")
    executor.execute_action_proposal = lambda proposal: {}

    db_tools = ModuleType("db_tools")
    db_tools.verify_runtime_security = lambda: {
        "observer": {},
        "executor": {},
        "terminator": {},
    }

    workflow = ModuleType("incident_workflow")
    workflow.create_lock_incident = lambda **kwargs: {}
    workflow.incident_public_view = lambda value: value
    workflow.run_lock_incident = lambda *args, **kwargs: {}

    store = ModuleType("workflow_store")
    store.SQLiteIncidentStore = object

    stubs = {
        "agent": agent,
        "db_tools": db_tools,
        "executor": executor,
        "incident_workflow": workflow,
        "workflow_store": store,
    }
    previous = {
        name: sys.modules.get(name)
        for name in stubs
    }
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "main_under_test",
            SRC / "main.py",
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


MAIN = load_main()
ORIGINAL_INCIDENT_PUBLIC_VIEW = MAIN.incident_public_view
ORIGINAL_RUN_INCIDENT = MAIN.run_incident
ORIGINAL_RUN_LOCK_INCIDENT = MAIN.run_lock_incident
ORIGINAL_STORE = MAIN.SQLiteIncidentStore


def terminate_proposal(blocked, blocker):
    return {
        "type": "TERMINATE_BACKEND",
        "blocked_pid": blocked,
        "blocker_pid": blocker,
    }


class MainIncidentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow_calls = []
        self.executor_calls = []
        MAIN.incident_public_view = ORIGINAL_INCIDENT_PUBLIC_VIEW
        MAIN.run_incident = ORIGINAL_RUN_INCIDENT
        MAIN.run_lock_incident = ORIGINAL_RUN_LOCK_INCIDENT
        MAIN.SQLiteIncidentStore = ORIGINAL_STORE
        MAIN.verify_runtime_security = lambda: {
            "observer": {},
            "executor": {},
            "terminator": {},
        }
        MAIN.run_termination_incident = (
            lambda **kwargs: self.workflow_calls.append(kwargs)
        )
        MAIN.execute_action_proposal = (
            lambda proposal: self.executor_calls.append(proposal)
        )

    def test_multiple_termination_proposals_route_to_one_incident(self):
        proposals = [
            terminate_proposal(101, 201),
            terminate_proposal(102, 202),
            terminate_proposal(103, 203),
        ]
        MAIN.run_agent = lambda message, mode: {
            "status": "completed",
            "answer": "lock diagnosis",
            "tool_trace": [],
            "proposals": proposals,
        }

        with redirect_stdout(io.StringIO()):
            MAIN.run_incident(
                "resolve the lock incident",
                mode="propose",
            )

        self.assertEqual(len(self.workflow_calls), 1)
        self.assertEqual(
            self.workflow_calls[0]["proposals"],
            proposals,
        )
        self.assertFalse(
            self.workflow_calls[0]["expand_all_actionable"]
        )
        self.assertEqual(self.executor_calls, [])

    def test_explicit_lock_workflow_expands_from_one_agent_proposal(self):
        proposal = terminate_proposal(101, 201)
        MAIN.run_agent = lambda message, mode: {
            "status": "completed",
            "answer": "lock diagnosis",
            "tool_trace": [],
            "proposals": [proposal],
        }

        with redirect_stdout(io.StringIO()):
            MAIN.run_incident(
                "resolve every current blocker",
                mode="propose",
                lock_workflow=True,
            )

        self.assertEqual(len(self.workflow_calls), 1)
        self.assertTrue(
            self.workflow_calls[0]["expand_all_actionable"]
        )
        self.assertEqual(self.executor_calls, [])

    def test_mixed_batch_still_fails_closed(self):
        proposals = [
            terminate_proposal(101, 201),
            {"type": "CREATE_INDEX"},
        ]
        MAIN.run_agent = lambda message, mode: {
            "status": "completed",
            "answer": "mixed diagnosis",
            "tool_trace": [],
            "proposals": proposals,
        }

        with redirect_stdout(io.StringIO()):
            MAIN.run_incident(
                "do several things",
                mode="propose",
            )

        self.assertEqual(self.workflow_calls, [])
        self.assertEqual(self.executor_calls, [])

    def test_explicit_lock_workflow_without_proposal_fails_closed(self):
        MAIN.run_agent = lambda message, mode: {
            "status": "completed",
            "answer": "No supported termination target.",
            "tool_trace": [],
            "proposals": [],
        }

        output = io.StringIO()
        with redirect_stdout(output):
            MAIN.run_incident(
                "resolve every current blocker",
                mode="propose",
                lock_workflow=True,
            )

        self.assertIn("failed closed", output.getvalue())
        self.assertEqual(self.workflow_calls, [])
        self.assertEqual(self.executor_calls, [])

    def test_explicit_lock_workflow_with_non_termination_fails_closed(self):
        MAIN.run_agent = lambda message, mode: {
            "status": "completed",
            "answer": "Unsupported action for lock workflow.",
            "tool_trace": [],
            "proposals": [{"type": "CREATE_INDEX"}],
        }

        output = io.StringIO()
        with redirect_stdout(output):
            MAIN.run_incident(
                "resolve every current blocker",
                mode="propose",
                lock_workflow=True,
            )

        self.assertIn("failed closed", output.getvalue())
        self.assertEqual(self.workflow_calls, [])
        self.assertEqual(self.executor_calls, [])

    def test_explicit_lock_workflow_with_mixed_proposals_fails_closed(self):
        MAIN.run_agent = lambda message, mode: {
            "status": "completed",
            "answer": "Mixed actions.",
            "tool_trace": [],
            "proposals": [
                terminate_proposal(101, 201),
                {"type": "ANALYZE_TABLE"},
            ],
        }

        with redirect_stdout(io.StringIO()):
            MAIN.run_incident(
                "resolve every current blocker",
                mode="propose",
                lock_workflow=True,
            )

        self.assertEqual(self.workflow_calls, [])
        self.assertEqual(self.executor_calls, [])

    def test_one_shot_resolve_locks_sets_explicit_workflow_scope(self):
        calls = []
        MAIN.run_incident = lambda message, **kwargs: calls.append(
            (message, kwargs)
        )
        with patch.object(
            sys,
            "argv",
            ["main.py", "--resolve-locks", "all locks"],
        ):
            MAIN.main()

        self.assertEqual(calls[0][0], "all locks")
        self.assertEqual(calls[0][1]["mode"], "propose")
        self.assertTrue(calls[0][1]["lock_workflow"])

    def test_one_shot_session_and_thread_enable_persistent_scope(self):
        calls = []
        MAIN.run_incident = lambda message, **kwargs: calls.append(
            (message, kwargs)
        )
        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--session",
                "session-42",
                "--thread",
                "tenant-7",
                "diagnose current load",
            ],
        ):
            MAIN.main()

        self.assertEqual(calls[0][0], "diagnose current load")
        self.assertEqual(calls[0][1]["session_id"], "session-42")
        self.assertEqual(calls[0][1]["thread_id"], "tenant-7")

    def test_batch_approval_displays_exact_waiter_identity(self):
        incident = {
            "incident_id": "12345678-0000-0000-0000-000000000001",
            "actions": [{
                "target": {
                    "blocker_pid": 900,
                    "blocker_backend_start": (
                        "2026-08-14T00:00:00+00:00"
                    ),
                    "blocker_xact_start": (
                        "2026-08-14T00:01:00+00:00"
                    ),
                },
                "approved_waiters": [{
                    "blocked_pid": 101,
                    "blocked_backend_start": (
                        "2026-08-14T00:02:00+00:00"
                    ),
                    "blocked_xact_start": (
                        "2026-08-14T00:03:00+00:00"
                    ),
                }],
            }],
        }

        output = io.StringIO()
        with patch("builtins.input", return_value="approve 12345678"):
            with redirect_stdout(output):
                decision = MAIN.incident_approval_decider(incident)

        rendered = output.getvalue()
        self.assertTrue(decision["approved"])
        self.assertIn("approved waiter PID 101", rendered)
        self.assertIn(
            "backend_start=2026-08-14T00:02:00+00:00",
            rendered,
        )
        self.assertIn(
            "xact_start=2026-08-14T00:03:00+00:00",
            rendered,
        )

    def test_resume_attests_before_loading_or_running_workflow(self):
        events = []
        incident_id = "00000000-0000-0000-0000-000000000001"

        class FakeStore:
            def __init__(self):
                events.append("store-created")

            def load_incident(self, value):
                events.append("incident-loaded")
                return {
                    "incident_id": value,
                    "state": "RUNNING",
                }

        MAIN.verify_runtime_security = lambda: events.append(
            "runtime-attested"
        )
        MAIN.SQLiteIncidentStore = FakeStore
        MAIN.incident_public_view = lambda value: value

        def run_workflow(value, **kwargs):
            events.append("workflow-run")
            return {
                "incident_id": value,
                "state": "COMPLETED",
            }

        MAIN.run_lock_incident = run_workflow

        with redirect_stdout(io.StringIO()):
            result = MAIN.resume_termination_incident(
                incident_id
            )

        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(
            events,
            [
                "runtime-attested",
                "store-created",
                "incident-loaded",
                "workflow-run",
            ],
        )

    def test_failed_resume_attestation_touches_no_workflow_state(self):
        events = []

        def reject_runtime():
            events.append("runtime-attested")
            raise RuntimeError("unsafe runtime identities")

        class ForbiddenStore:
            def __init__(self):
                events.append("store-created")

        MAIN.verify_runtime_security = reject_runtime
        MAIN.SQLiteIncidentStore = ForbiddenStore
        MAIN.run_lock_incident = lambda *args, **kwargs: events.append(
            "workflow-run"
        )

        with self.assertRaises(RuntimeError):
            MAIN.resume_termination_incident(
                "00000000-0000-0000-0000-000000000001"
            )

        self.assertEqual(events, ["runtime-attested"])


if __name__ == "__main__":
    unittest.main()
