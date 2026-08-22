import json
import sqlite3
import sys
import tempfile
import unittest
import uuid

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

if "dotenv" not in sys.modules:
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv


from incident_approval import (  # noqa: E402
    canonical_approved_actions,
    compute_scope_digest,
)
from workflow_store import (  # noqa: E402
    IncidentApprovalUnavailable,
    IncidentLeaseUnavailable,
    SQLiteIncidentStore,
)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class WorkflowStoreSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = datetime(
            2026,
            8,
            14,
            0,
            0,
            tzinfo=timezone.utc,
        )
        self.clock = MutableClock(self.now)
        self.store = SQLiteIncidentStore(
            Path(self.temporary.name) / "incidents.sqlite3",
            clock=self.clock,
        )
        self.worker_id = "worker-security-test"
        self.incident_id = str(uuid.uuid4())
        self.action_id = str(uuid.uuid4())
        self.operation_id = str(uuid.uuid4())
        self.approval_id = str(uuid.uuid4())
        self.target = {
            "database_name": "benchmark",
            "blocker_pid": 202,
            "blocker_backend_start": "2026-08-14T00:00:20+00:00",
            "blocker_xact_start": "2026-08-14T00:00:21+00:00",
        }
        self.waiter = {
            "blocked_pid": 101,
            "blocked_backend_start": "2026-08-14T00:00:10+00:00",
            "blocked_xact_start": "2026-08-14T00:00:11+00:00",
        }
        self.proposal = {
            "type": "TERMINATE_BACKEND",
            "database_name": "benchmark",
            "blocked_pid": 101,
            "blocker_pid": 202,
            "blocker_backend_start": self.target[
                "blocker_backend_start"
            ],
            "blocker_xact_start": self.target[
                "blocker_xact_start"
            ],
            "reason": "Exact current lock blocker.",
            "confidence": 0.95,
            "risk": "HIGH",
        }
        self.evidence = {
            **self.waiter,
            **self.target,
            "blocked_pid": self.waiter["blocked_pid"],
            "database_name": "benchmark",
            "blocked_wait_event_type": "Lock",
            "blocker_state": "idle in transaction",
        }
        self._create_ready_incident(
            approval_ttl_seconds=90,
            lease_seconds=120,
        )

    def _create_ready_incident(
        self,
        *,
        approval_ttl_seconds,
        lease_seconds,
    ):
        timestamp = self.now.isoformat()
        action = {
            "action_id": self.action_id,
            "ordinal": 1,
            "plan_revision": 1,
            "type": "TERMINATE_BACKEND",
            "risk": "HIGH",
            "state": "PLANNED",
            "idempotency_key": str(uuid.uuid4()),
            "target": deepcopy(self.target),
            "allowed_blocked_pids": [101],
            "approved_waiters": [deepcopy(self.waiter)],
            "proposal": deepcopy(self.proposal),
            "attempt_count": 0,
            "operation_id": None,
            "result": None,
            "last_error": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        incident = {
            "schema_version": 2,
            "incident_id": self.incident_id,
            "workflow_type": "LOCK_CONTENTION",
            "state": "AWAITING_APPROVAL",
            "plan_revision": 1,
            "request": {},
            "policy": {
                "deadline_at": (
                    self.now + timedelta(minutes=5)
                ).isoformat(),
            },
            "final_summary": None,
            "last_error": None,
            "terminal_reason": None,
            "version": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "actions": [action],
            "approval": None,
        }
        self.store.create_incident(incident)
        incident = self.store.acquire_lease(
            self.incident_id,
            owner=self.worker_id,
            now=self.now,
            lease_seconds=lease_seconds,
        )
        approved_actions = canonical_approved_actions(
            incident["actions"]
        )
        approval = {
            "kind": "LOCK_INCIDENT_APPROVAL",
            "approval_id": self.approval_id,
            "incident_id": self.incident_id,
            "plan_revision": 1,
            "actor": "security-test",
            "approved_at": self.now.isoformat(),
            "expires_at": (
                self.now + timedelta(seconds=approval_ttl_seconds)
            ).isoformat(),
            "revoked_at": None,
            "max_risk": "HIGH",
            "max_actions": 1,
            "approved_actions": approved_actions,
        }
        approval["scope_digest"] = compute_scope_digest(
            incident_id=self.incident_id,
            plan_revision=1,
            approved_actions=approved_actions,
            max_actions=1,
            max_risk="HIGH",
        )
        incident["approval"] = approval
        incident["state"] = "RUNNING"
        incident["actions"][0]["state"] = "EXECUTING"
        incident["actions"][0]["operation_id"] = self.operation_id
        self.ready = self.store.save_incident(
            incident,
            event_type="STEP_EXECUTION_INTENT",
            action_id=self.action_id,
            lease_owner=self.worker_id,
        )

    def approval_reference(self):
        return {
            "incident_id": self.incident_id,
            "approval_id": self.approval_id,
            "current_action_id": self.action_id,
        }

    def execution_context(self, **changes):
        value = {
            "kind": "LOCK_INCIDENT_EXECUTION",
            "store_path": str(self.store.path.resolve()),
            "worker_id": self.worker_id,
            "incident_id": self.incident_id,
            "action_id": self.action_id,
            "plan_revision": 1,
        }
        value.update(changes)
        return value

    def claim(self, **changes):
        arguments = {
            "approval_context": self.approval_reference(),
            "operation_id": self.operation_id,
            "proposal": deepcopy(self.proposal),
            "current_evidence": deepcopy(self.evidence),
            "execution_context": self.execution_context(),
        }
        arguments.update(changes)
        return self.store.claim_action_execution(**arguments)

    def test_claim_is_atomic_versioned_and_one_time(self):
        old_version = self.ready["version"]

        authorization = self.claim()

        self.assertEqual(
            authorization["kind"],
            "PERSISTED_ACTION_EXECUTION_CLAIM",
        )
        loaded = self.store.load_incident(self.incident_id)
        self.assertEqual(loaded["actions"][0]["state"], "APPLYING")
        self.assertEqual(loaded["version"], old_version + 1)
        self.assertEqual(
            self.store.list_events(self.incident_id)[-1]["event_type"],
            "ACTION_EXECUTION_CLAIMED",
        )

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim()
        self.assertEqual(
            self.store.load_incident(self.incident_id)["version"],
            old_version + 1,
        )

    def test_forged_approval_reference_is_rejected(self):
        reference = self.approval_reference()
        reference["approval_id"] = str(uuid.uuid4())

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(approval_context=reference)

        self.assertEqual(
            self.store.load_incident(self.incident_id)["actions"][0][
                "state"
            ],
            "EXECUTING",
        )

    def test_revoked_persisted_approval_is_rejected(self):
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute(
                "UPDATE approvals SET revoked_at = ? WHERE approval_id = ?",
                (self.now.isoformat(), self.approval_id),
            )
            connection.commit()

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim()

    def test_expired_persisted_approval_is_rejected(self):
        self.clock.advance(seconds=91)

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim()

    def test_incident_deadline_is_checked_at_atomic_claim_time(self):
        deadline = self.now + timedelta(seconds=60)
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute(
                "UPDATE incidents SET policy_json = ? WHERE incident_id = ?",
                (
                    json.dumps({"deadline_at": deadline.isoformat()}),
                    self.incident_id,
                ),
            )
            connection.commit()

        self.clock.advance(seconds=60)
        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim()

        loaded = self.store.load_incident(self.incident_id)
        self.assertEqual(loaded["actions"][0]["state"], "EXECUTING")

    def test_operation_mismatch_is_rejected(self):
        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(operation_id=str(uuid.uuid4()))

    def test_action_and_plan_reference_mismatch_is_rejected(self):
        reference = self.approval_reference()
        reference["current_action_id"] = str(uuid.uuid4())
        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(approval_context=reference)

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(
                execution_context=self.execution_context(
                    plan_revision=2
                )
            )

    def test_wrong_store_path_or_context_kind_is_rejected(self):
        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(
                execution_context=self.execution_context(
                    store_path=str(
                        Path(self.temporary.name) / "forged.sqlite3"
                    )
                )
            )

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(
                execution_context=self.execution_context(
                    kind="FORGED_EXECUTION_CONTEXT"
                )
            )

    def test_wrong_or_expired_lease_is_rejected(self):
        with self.assertRaises(IncidentLeaseUnavailable):
            self.claim(
                execution_context=self.execution_context(
                    worker_id="another-worker"
                )
            )

        self.clock.advance(seconds=121)
        with self.assertRaises(IncidentLeaseUnavailable):
            self.claim()

    def test_hard_crash_lease_blocks_takeover_until_expiry(self):
        # A killed process does not run workflow ``finally``.  Model that by
        # leaving the original lease row untouched and opening a new store
        # instance, as a replacement process would do.
        replacement = SQLiteIncidentStore(
            self.store.path,
            clock=self.clock,
        )

        with self.assertRaises(IncidentLeaseUnavailable):
            replacement.acquire_lease(
                self.incident_id,
                owner="replacement-worker",
                now=self.clock(),
                lease_seconds=60,
            )

        self.clock.advance(seconds=121)
        taken_over = replacement.acquire_lease(
            self.incident_id,
            owner="replacement-worker",
            now=self.clock(),
            lease_seconds=60,
        )
        self.assertEqual(
            taken_over["lease"]["owner"],
            "replacement-worker",
        )
        self.assertEqual(
            taken_over["actions"][0]["state"],
            "EXECUTING",
        )

        # Even with the replacement's current CAS version, the old worker's
        # owner token cannot checkpoint after takeover.
        stale = replacement.load_incident(self.incident_id)
        stale["last_error"] = {"type": "OLD_WORKER_WRITE"}
        with self.assertRaises(IncidentLeaseUnavailable):
            self.store.save_incident(
                stale,
                event_type="OLD_WORKER_CHECKPOINT",
                lease_owner=self.worker_id,
            )

    def test_waiter_evidence_mismatch_is_rejected(self):
        evidence = deepcopy(self.evidence)
        evidence["blocked_backend_start"] = (
            "2026-08-14T00:00:12+00:00"
        )

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(current_evidence=evidence)

    def test_proposal_mismatch_is_rejected(self):
        proposal = deepcopy(self.proposal)
        proposal["reason"] = "Unpersisted reason change."

        with self.assertRaises(IncidentApprovalUnavailable):
            self.claim(proposal=proposal)

    def test_terminal_incident_cannot_acquire_lease(self):
        incident = self.store.load_incident(self.incident_id)
        incident["state"] = "REVIEW_REQUIRED"
        incident["actions"][0]["state"] = "IN_DOUBT"
        self.store.save_incident(
            incident,
            event_type="REVIEW_REQUIRED",
            lease_owner=self.worker_id,
        )
        self.store.release_lease(
            self.incident_id,
            owner=self.worker_id,
            now=self.now,
        )

        with self.assertRaises(IncidentLeaseUnavailable):
            self.store.acquire_lease(
                self.incident_id,
                owner=self.worker_id,
                now=self.now,
                lease_seconds=60,
            )


if __name__ == "__main__":
    unittest.main()
