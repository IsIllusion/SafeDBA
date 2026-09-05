import copy
import hashlib
import json
import sys
import tempfile
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


# The workflow tests inject all database observations and executions.  Keep
# imports dependency-free so the deterministic state machine can be tested
# without a PostgreSQL driver or a local .env loader.
if "psycopg" not in sys.modules:
    psycopg = ModuleType("psycopg")
    psycopg.connect = lambda **kwargs: None
    psycopg.sql = SimpleNamespace()
    sys.modules["psycopg"] = psycopg

if "dotenv" not in sys.modules:
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv


from incident_workflow import (  # noqa: E402
    IncidentPlanError,
    build_termination_plan,
    create_lock_incident,
    run_lock_incident,
)
from workflow_store import (  # noqa: E402
    ConcurrentIncidentUpdate,
    SQLiteIncidentStore,
)


def lock_row(
    blocked_pid,
    blocker_pid,
    *,
    backend_start=None,
    xact_start=None,
    blocked_backend_start=None,
    blocked_xact_start=None,
    blocker_state="idle in transaction",
):
    return {
        "database_name": "benchmark",
        "blocked_pid": blocked_pid,
        "blocked_backend_start": (
            blocked_backend_start
            or f"2026-08-14T00:02:{blocked_pid % 60:02d}+00:00"
        ),
        "blocked_xact_start": (
            blocked_xact_start
            or f"2026-08-14T00:03:{blocked_pid % 60:02d}+00:00"
        ),
        "blocked_wait_event_type": "Lock",
        "blocker_pid": blocker_pid,
        "blocker_database_name": "benchmark",
        "blocker_backend_type": "client backend",
        "blocker_state": blocker_state,
        "blocker_backend_start": (
            backend_start
            or f"2026-08-14T00:00:{blocker_pid % 60:02d}+00:00"
        ),
        "blocker_xact_start": (
            xact_start
            or f"2026-08-14T00:01:{blocker_pid % 60:02d}+00:00"
        ),
    }


def snapshot(rows, *, truncated=False):
    safe_rows = copy.deepcopy(rows)
    digest = hashlib.sha256(
        json.dumps(
            safe_rows,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "captured_at": "2026-08-14T00:02:00+00:00",
        "database_name": "benchmark",
        "rows": safe_rows,
        "row_count": len(safe_rows),
        "truncated": truncated,
        "snapshot_digest": digest,
    }


def proposal(row):
    return {
        "type": "TERMINATE_BACKEND",
        "blocked_pid": row["blocked_pid"],
        "blocker_pid": row["blocker_pid"],
        "blocker_backend_start": row["blocker_backend_start"],
        "blocker_xact_start": row["blocker_xact_start"],
        "reason": "Current idle transaction blocks a lock waiter.",
        "confidence": 0.95,
        "risk": "HIGH",
    }


class LockEnvironment:
    def __init__(self, rows, *, clock=None):
        self.rows = copy.deepcopy(rows)
        self.execute_calls = []
        self.execution_contexts = []
        self.approval_ids = []
        self.fail_on_call = None
        self.crash_on_call = None
        self.crash_before_claim = False
        self.crash_after_side_effect = False
        self.return_none_on_call = None
        self.raise_on_call = None
        self.return_success_without_claim_on_call = None
        self.return_policy_blocked_on_call = None
        self.return_blocked_after_claim_on_call = None
        self.blocked_status = "BLOCKED_INCIDENT_APPROVAL"
        self.remove_representative_only = False
        self.inject_after_call = {}
        self.observe_calls = 0
        self.observe_hooks = {}
        self.before_claim_hooks = {}
        self.clock = clock or (
            lambda: datetime.now(timezone.utc)
        )

    def observe(self):
        self.observe_calls += 1
        hook = self.observe_hooks.get(
            self.observe_calls
        )
        if hook is not None:
            hook()
        return snapshot(self.rows)

    def execute(
        self,
        action_proposal,
        *,
        operation_id,
        approval_context,
        execution_context,
    ):
        self.execute_calls.append(copy.deepcopy(action_proposal))
        self.execution_contexts.append(
            copy.deepcopy(execution_context)
        )
        self.approval_ids.append(
            approval_context["approval_id"]
        )
        call_number = len(self.execute_calls)

        if self.return_policy_blocked_on_call == call_number:
            return {
                "operation_id": operation_id,
                "status": "BLOCKED_RUNTIME_POLICY",
                "executed": False,
                "policy_reason": "action_disabled",
                "audit": {"status": "WRITTEN"},
            }

        if (
            self.crash_on_call == call_number
            and self.crash_before_claim
        ):
            raise SystemExit("simulated pre-claim interruption")

        before_claim = self.before_claim_hooks.get(call_number)
        if before_claim is not None:
            before_claim()

        if self.return_success_without_claim_on_call == call_number:
            return {
                "operation_id": operation_id,
                "status": "BACKEND_TERMINATION_CONFIRMED",
                "blocking_relationship_removed": True,
                "audit": {"status": "WRITTEN"},
            }

        try:
            self._claim(
                action_proposal=action_proposal,
                operation_id=operation_id,
                approval_context=approval_context,
                execution_context=execution_context,
            )
        except Exception as exc:
            # Match the production executor: a refused durable claim is a
            # structured authorization result, never a database side effect.
            return {
                "operation_id": operation_id,
                "status": "BLOCKED_INCIDENT_APPROVAL",
                "blocking_relationship_removed": False,
                "approval_error_type": type(exc).__name__,
                "audit": {"status": "WRITTEN"},
            }

        if self.raise_on_call == call_number:
            raise RuntimeError("simulated executor failure after claim")

        if self.return_blocked_after_claim_on_call == call_number:
            return {
                "operation_id": operation_id,
                "status": self.blocked_status,
                "blocking_relationship_removed": False,
                "approval_error_type": "LateExecutorError",
                "audit": {"status": "WRITTEN"},
            }

        if self.crash_on_call == call_number:
            if self.crash_after_side_effect:
                self._remove_target(action_proposal)
            raise SystemExit("simulated process interruption")

        if self.return_none_on_call == call_number:
            return None

        if self.fail_on_call == call_number:
            return {
                "operation_id": operation_id,
                "status": "TERMINATION_FAILED",
                "blocking_relationship_removed": False,
                "audit": {"status": "WRITTEN"},
            }

        self._remove_target(action_proposal)
        self.rows.extend(
            copy.deepcopy(
                self.inject_after_call.get(call_number, [])
            )
        )
        return {
            "operation_id": operation_id,
            "status": "BACKEND_TERMINATION_CONFIRMED",
            "blocking_relationship_removed": True,
            "audit": {"status": "WRITTEN"},
        }

    def _remove_target(self, action_proposal):
        if self.remove_representative_only:
            self.rows = [
                row
                for row in self.rows
                if not (
                    row["blocked_pid"]
                    == action_proposal["blocked_pid"]
                    and row["blocker_pid"]
                    == action_proposal["blocker_pid"]
                    and row["blocker_backend_start"]
                    == action_proposal["blocker_backend_start"]
                    and row["blocker_xact_start"]
                    == action_proposal["blocker_xact_start"]
                )
            ]
            return

        identity = (
            action_proposal["blocker_pid"],
            action_proposal["blocker_backend_start"],
            action_proposal["blocker_xact_start"],
        )
        self.rows = [
            row
            for row in self.rows
            if (
                row["blocker_pid"],
                row["blocker_backend_start"],
                row["blocker_xact_start"],
            )
            != identity
        ]

    def _claim(
        self,
        *,
        action_proposal,
        operation_id,
        approval_context,
        execution_context,
    ):
        self.assert_execution_context(execution_context)
        claim_store = SQLiteIncidentStore(
            execution_context["store_path"],
            clock=self.clock,
        )
        evidence = next(
            copy.deepcopy(row)
            for row in self.rows
            if (
                row["blocked_pid"]
                == action_proposal["blocked_pid"]
                and row["blocker_pid"]
                == action_proposal["blocker_pid"]
                and row["blocker_backend_start"]
                == action_proposal["blocker_backend_start"]
                and row["blocker_xact_start"]
                == action_proposal["blocker_xact_start"]
            )
        )
        claim_store.claim_action_execution(
            approval_context=approval_context,
            operation_id=operation_id,
            proposal=action_proposal,
            current_evidence=evidence,
            execution_context=execution_context,
        )

    def assert_execution_context(self, context):
        required = {
            "kind",
            "store_path",
            "worker_id",
            "incident_id",
            "action_id",
            "plan_revision",
        }
        if not isinstance(context, dict):
            raise AssertionError("execution_context must be an object")
        if set(context) != required:
            raise AssertionError(
                f"unexpected execution_context keys: {set(context)}"
            )


class FakeClock:
    def __init__(self):
        self.current = datetime(
            2026,
            8,
            14,
            tzinfo=timezone.utc,
        )

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


class CrashAfterEventStore(SQLiteIncidentStore):
    def __init__(self, *args, crash_event, **kwargs):
        self.crash_event = crash_event
        self.crashed = False
        super().__init__(*args, **kwargs)

    def save_incident(self, incident, **kwargs):
        result = super().save_incident(incident, **kwargs)
        if (
            not self.crashed
            and kwargs.get("event_type") == self.crash_event
        ):
            self.crashed = True
            raise SystemExit(
                f"simulated crash after {self.crash_event}"
            )
        return result


class IncidentWorkflowTests(unittest.TestCase):
    def test_policy_block_before_claim_preserves_pending_actions_and_requires_reapproval(self):
        rows = [lock_row(101, 201), lock_row(102, 202), lock_row(103, 203)]
        environment = LockEnvironment(rows)
        environment.return_policy_blocked_on_call = 2
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(store, environment, [proposal(rows[0])], expand=True)
            result, approvals = self.run_workflow(store, environment, incident)
            self.assertEqual(result["state"], "AWAITING_REAPPROVAL")
            self.assertEqual(len(environment.execute_calls), 2)
            self.assertEqual(len(approvals), 1)
            self.assertEqual([action["state"] for action in result["actions"]], ["SUCCEEDED", "PLANNED", "PLANNED"])
            self.assertEqual(result["actions"][1]["last_error"]["policy_reason"], "action_disabled")

            # Releasing a switch does not grant approval for the rest of the
            # batch. Explicit resume asks again, without repeating step 1.
            environment.return_policy_blocked_on_call = None
            resumed, new_approvals = self.run_workflow(store, environment, result)
            self.assertEqual(resumed["state"], "COMPLETED")
            self.assertEqual(len(new_approvals), 1)
            self.assertEqual(len(environment.execute_calls), 4)

    def test_policy_block_after_claim_cannot_be_treated_as_safe_to_retry(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        environment.return_blocked_after_claim_on_call = 1
        environment.blocked_status = "BLOCKED_RUNTIME_POLICY"
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(store, environment, [proposal(row)])
            result, _ = self.run_workflow(store, environment, incident)
            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertNotEqual(result["actions"][0]["state"], "PLANNED")

    def make_store(self, directory, *, clock=None):
        return SQLiteIncidentStore(
            Path(directory) / "incidents.sqlite3",
            clock=clock,
        )

    def create(
        self,
        store,
        environment,
        proposals,
        *,
        expand=False,
        now=None,
    ):
        kwargs = {}
        if now is not None:
            kwargs["now"] = now
        return create_lock_incident(
            proposals=proposals,
            user_request="Resolve all current lock blockers.",
            expand_all_actionable=expand,
            store=store,
            observe_locks=environment.observe,
            **kwargs,
        )

    def run_workflow(
        self,
        store,
        environment,
        incident,
        decision=True,
        now=None,
    ):
        approvals = []

        def approve(value):
            approvals.append(value["incident_id"])
            return {
                "approved": decision,
                "actor": "test-operator",
            }

        kwargs = {}
        if now is not None:
            kwargs["now"] = now
        result = run_lock_incident(
            incident["incident_id"],
            store=store,
            observe_locks=environment.observe,
            execute_action=environment.execute,
            approval_decider=approve,
            **kwargs,
        )
        return result, approvals

    def test_three_independent_blockers_use_one_approval_and_three_steps(self):
        rows = [
            lock_row(101, 201),
            lock_row(102, 202),
            lock_row(103, 203),
        ]
        environment = LockEnvironment(rows)
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(rows[0])],
                expand=True,
            )
            self.assertEqual(len(incident["actions"]), 3)

            result, approvals = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(len(environment.execute_calls), 3)
            self.assertEqual(
                len(set(environment.approval_ids)),
                1,
            )
            self.assertTrue(
                all(
                    action["state"] == "SUCCEEDED"
                    for action in result["actions"]
                )
            )

    def test_shared_blocker_is_deduplicated_and_all_edges_are_verified(self):
        rows = [
            lock_row(101, 900),
            lock_row(102, 900),
            lock_row(103, 900),
        ]
        environment = LockEnvironment(rows)
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(rows[0])],
                expand=True,
            )
            self.assertEqual(len(incident["actions"]), 1)
            self.assertEqual(
                incident["actions"][0]["allowed_blocked_pids"],
                [101, 102, 103],
            )
            self.assertEqual(
                {
                    waiter["blocked_pid"]
                    for waiter in incident["actions"][0][
                        "approved_waiters"
                    ]
                },
                {101, 102, 103},
            )

            result, approvals = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(environment.rows, [])

    def test_shared_blocker_remaining_edge_makes_success_inconclusive(self):
        rows = [
            lock_row(101, 900),
            lock_row(102, 900),
            lock_row(103, 900),
        ]
        environment = LockEnvironment(rows)
        environment.remove_representative_only = True
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(rows[0])],
                expand=True,
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(
                result["actions"][0]["state"],
                "INCONCLUSIVE",
            )
            self.assertEqual(len(environment.rows), 2)

    def test_representative_can_move_to_another_original_waiter(self):
        rows = [
            lock_row(101, 900),
            lock_row(102, 900),
        ]
        environment = LockEnvironment(rows)
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(rows[0])],
                expand=True,
            )
            environment.rows = [rows[1]]

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(
                environment.execute_calls[0]["blocked_pid"],
                102,
            )

    def test_pid_reuse_invalidates_the_approved_target(self):
        original = lock_row(101, 900)
        environment = LockEnvironment([original])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(original)],
                expand=True,
            )
            environment.rows = [
                lock_row(
                    101,
                    900,
                    backend_start="2026-08-14T03:00:00+00:00",
                    xact_start="2026-08-14T03:01:00+00:00",
                )
            ]

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(len(environment.execute_calls), 0)
            self.assertEqual(
                result["actions"][0]["state"],
                "STALE_IDENTITY",
            )

    def test_new_unapproved_blocker_is_reported_but_never_executed(self):
        original = lock_row(101, 201)
        new_blocker = lock_row(104, 204)
        environment = LockEnvironment([original])
        environment.inject_after_call[1] = [new_blocker]
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(original)],
                expand=True,
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(
                result["state"],
                "COMPLETED_WITH_UNAPPROVED_REMAINDER",
            )
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(environment.rows, [new_blocker])

    def test_new_waiter_alone_cannot_extend_an_existing_target_scope(self):
        original = lock_row(101, 201)
        environment = LockEnvironment([original])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(original)],
                expand=True,
            )
            environment.rows = [lock_row(999, 201)]

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(len(environment.execute_calls), 0)
            self.assertEqual(
                result["actions"][0]["state"],
                "REAPPROVAL_REQUIRED",
            )

    def test_reused_waiter_pid_does_not_extend_approved_waiter_identity(self):
        original = lock_row(101, 201)
        environment = LockEnvironment([original])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(original)],
                expand=True,
            )
            environment.rows = [
                lock_row(
                    101,
                    201,
                    blocked_backend_start=(
                        "2026-08-14T04:00:00+00:00"
                    ),
                    blocked_xact_start=(
                        "2026-08-14T04:01:00+00:00"
                    ),
                )
            ]

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(environment.execute_calls, [])
            self.assertEqual(
                result["actions"][0]["last_error"]["type"],
                "ONLY_NEW_WAITERS_REMAIN",
            )

    def test_fresh_snapshot_reapplies_termination_policy(self):
        original = lock_row(101, 201)
        environment = LockEnvironment([original])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(original)],
                expand=True,
            )
            environment.rows = [
                lock_row(
                    101,
                    201,
                    blocker_state="active",
                )
            ]

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(environment.execute_calls, [])
            self.assertEqual(
                result["actions"][0]["last_error"]["type"],
                "TARGET_OUTSIDE_TERMINATION_POLICY",
            )

    def test_partial_failure_stops_later_high_risk_actions(self):
        rows = [
            lock_row(101, 201),
            lock_row(102, 202),
            lock_row(103, 203),
        ]
        environment = LockEnvironment(rows)
        environment.fail_on_call = 2
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(rows[0])],
                expand=True,
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(len(environment.execute_calls), 2)
            self.assertEqual(
                [action["state"] for action in result["actions"]],
                ["SUCCEEDED", "FAILED", "PLANNED"],
            )

    def test_malformed_executor_result_fails_closed_and_stops_batch(self):
        rows = [
            lock_row(101, 201),
            lock_row(102, 202),
        ]
        environment = LockEnvironment(rows)
        environment.return_none_on_call = 1
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row) for row in rows],
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(
                [action["state"] for action in result["actions"]],
                ["IN_DOUBT", "PLANNED"],
            )
            self.assertEqual(
                result["actions"][0]["last_error"]["type"],
                "MALFORMED_EXECUTOR_RESULT",
            )

    def test_executor_exception_after_claim_is_in_doubt_and_stops_batch(self):
        rows = [
            lock_row(101, 201),
            lock_row(102, 202),
        ]
        environment = LockEnvironment(rows)
        environment.raise_on_call = 1
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row) for row in rows],
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(
                [action["state"] for action in result["actions"]],
                ["IN_DOUBT", "PLANNED"],
            )
            self.assertEqual(
                result["actions"][0]["last_error"]["type"],
                "RuntimeError",
            )

    def test_claim_time_expiry_returns_to_reapproval_without_side_effect(self):
        clock = FakeClock()
        row = lock_row(101, 201)
        environment = LockEnvironment([row], clock=clock)
        environment.before_claim_hooks[1] = (
            lambda: clock.advance(2)
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory, clock=clock)
            with patch(
                "incident_workflow.INCIDENT_APPROVAL_TTL_SECONDS",
                1.0,
            ):
                incident = self.create(
                    store,
                    environment,
                    [proposal(row)],
                    expand=True,
                    now=clock,
                )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
                now=clock,
            )

            self.assertEqual(result["state"], "AWAITING_REAPPROVAL")
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(environment.rows, [row])
            self.assertEqual(result["actions"][0]["state"], "PLANNED")
            self.assertEqual(
                result["actions"][0]["last_error"]["type"],
                "EXECUTION_AUTHORIZATION_BLOCKED",
            )

    def test_success_without_atomic_claim_is_never_trusted(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        environment.return_success_without_claim_on_call = 1
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(result["actions"][0]["state"], "IN_DOUBT")
            self.assertEqual(environment.rows, [row])
            self.assertEqual(
                result["actions"][0]["last_error"]["type"],
                "EXECUTION_CLAIM_MISSING",
            )

    def test_blocked_result_after_claim_cannot_return_to_planned(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        environment.return_blocked_after_claim_on_call = 1
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertEqual(result["actions"][0]["state"], "FAILED")
            self.assertEqual(environment.rows, [row])
            self.assertEqual(
                result["actions"][0]["last_error"]["type"],
                "EXECUTOR_RESULT_NOT_CONFIRMED",
            )

    def test_rejected_batch_executes_nothing(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )

            result, approvals = self.run_workflow(
                store,
                environment,
                incident,
                decision=False,
            )

            self.assertEqual(result["state"], "CANCELLED")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(environment.execute_calls, [])

    def test_crash_after_side_effect_reconciles_without_repeating(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        environment.crash_on_call = 1
        environment.crash_after_side_effect = True
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )

            with self.assertRaises(SystemExit):
                self.run_workflow(store, environment, incident)

            environment.crash_on_call = None
            resumed = run_lock_incident(
                incident["incident_id"],
                store=store,
                observe_locks=environment.observe,
                execute_action=environment.execute,
            )

            self.assertEqual(resumed["state"], "COMPLETED")
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(
                resumed["actions"][0]["state"],
                "RECONCILED_RESOLVED",
            )

    def test_crash_before_side_effect_is_in_doubt_and_never_retried(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        environment.crash_on_call = 1
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )

            with self.assertRaises(SystemExit):
                self.run_workflow(store, environment, incident)

            environment.crash_on_call = None
            resumed = run_lock_incident(
                incident["incident_id"],
                store=store,
                observe_locks=environment.observe,
                execute_action=environment.execute,
            )

            self.assertEqual(resumed["state"], "REVIEW_REQUIRED")
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(
                resumed["actions"][0]["state"],
                "IN_DOUBT",
            )

    def test_crash_before_executor_claim_reconciles_without_retry(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        environment.crash_on_call = 1
        environment.crash_before_claim = True
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )

            with self.assertRaises(SystemExit):
                self.run_workflow(store, environment, incident)

            environment.crash_on_call = None
            resumed = run_lock_incident(
                incident["incident_id"],
                store=store,
                observe_locks=environment.observe,
                execute_action=environment.execute,
            )

            self.assertEqual(resumed["state"], "REVIEW_REQUIRED")
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(
                resumed["actions"][0]["state"],
                "IN_DOUBT",
            )

    def test_result_recorded_checkpoint_recovers_without_reexecution(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incidents.sqlite3"
            crashing_store = CrashAfterEventStore(
                path,
                crash_event="STEP_RESULT_RECORDED",
            )
            incident = self.create(
                crashing_store,
                environment,
                [proposal(row)],
                expand=True,
            )

            with self.assertRaises(SystemExit):
                self.run_workflow(
                    crashing_store,
                    environment,
                    incident,
                )

            resumed_store = SQLiteIncidentStore(path)
            resumed = run_lock_incident(
                incident["incident_id"],
                store=resumed_store,
                observe_locks=environment.observe,
                execute_action=environment.execute,
            )

            self.assertEqual(resumed["state"], "COMPLETED")
            self.assertEqual(len(environment.execute_calls), 1)
            self.assertEqual(
                resumed["actions"][0]["state"],
                "SUCCEEDED",
            )

    def test_approval_expiring_during_fresh_observation_executes_nothing(self):
        clock = FakeClock()
        row = lock_row(101, 201)
        environment = LockEnvironment([row], clock=clock)
        # create_lock_incident consumes observation 1; run initial reconciliation
        # consumes observation 2; step policy observation is number 3.
        environment.observe_hooks[3] = lambda: clock.advance(2)
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory, clock=clock)
            with patch(
                "incident_workflow.INCIDENT_APPROVAL_TTL_SECONDS",
                1.0,
            ):
                incident = self.create(
                    store,
                    environment,
                    [proposal(row)],
                    expand=True,
                    now=clock,
                )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
                now=clock,
            )

            self.assertEqual(result["state"], "AWAITING_REAPPROVAL")
            self.assertEqual(environment.execute_calls, [])

    def test_multiple_explicit_proposals_build_three_actions_without_expansion(self):
        rows = [
            lock_row(101, 201),
            lock_row(102, 202),
            lock_row(103, 203),
        ]
        environment = LockEnvironment(rows)
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row) for row in rows],
                expand=False,
            )
            self.assertEqual(len(incident["actions"]), 3)

            result, approvals = self.run_workflow(
                store,
                environment,
                incident,
            )

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(len(environment.execute_calls), 3)

    def test_returned_incident_matches_post_release_durable_version(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )

            result, _ = self.run_workflow(
                store,
                environment,
                incident,
            )
            durable = store.load_incident(
                incident["incident_id"]
            )

            self.assertEqual(result["version"], durable["version"])
            self.assertEqual(result["lease"], durable["lease"])
            self.assertIsNone(result["lease"]["owner"])

    def test_store_compare_and_swap_rejects_stale_checkpoint(self):
        row = lock_row(101, 201)
        environment = LockEnvironment([row])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            incident = self.create(
                store,
                environment,
                [proposal(row)],
                expand=True,
            )
            owner = "test-cas-worker"
            current = datetime.now(timezone.utc)
            incident = store.acquire_lease(
                incident["incident_id"],
                owner=owner,
                now=current,
                lease_seconds=60,
            )
            stale = copy.deepcopy(incident)
            incident["state"] = "CANCELLED"
            incident = store.save_incident(
                incident,
                event_type="TEST_UPDATE",
                lease_owner=owner,
            )
            self.assertEqual(incident["state"], "CANCELLED")

            stale["state"] = "RUNNING"
            with self.assertRaises(ConcurrentIncidentUpdate):
                store.save_incident(
                    stale,
                    event_type="STALE_UPDATE",
                    lease_owner=owner,
                )

    def test_truncated_snapshot_and_mixed_actions_fail_closed(self):
        row = lock_row(101, 201)
        with self.assertRaises(IncidentPlanError):
            build_termination_plan(
                incident_id="00000000-0000-0000-0000-000000000001",
                snapshot=snapshot([row], truncated=True),
                proposals=[proposal(row)],
            )

        mixed = proposal(row)
        mixed["type"] = "CREATE_INDEX"
        with self.assertRaises(IncidentPlanError):
            build_termination_plan(
                incident_id="00000000-0000-0000-0000-000000000001",
                snapshot=snapshot([row]),
                proposals=[proposal(row), mixed],
            )


if __name__ == "__main__":
    unittest.main()
