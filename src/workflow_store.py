from state_database import open_state_database
from serialization import canonical_json, json_digest
from identifiers import is_valid_uuid as _valid_uuid
from identifiers import is_positive_int as _valid_positive_int
import json
import math
import sqlite3

from contextlib import closing
from copy import deepcopy
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from typing import Callable

from audit import sanitize_audit_value
from config import INCIDENT_STATE_DB_PATH
from incident_approval import (
    canonical_approved_actions,
    compute_scope_digest,
    parse_utc_timestamp,
    validate_approval_context,
)


SCHEMA_VERSION = 2

INCIDENT_TRANSITIONS = {
    "AWAITING_APPROVAL": {
        "AWAITING_APPROVAL",
        "RUNNING",
        "CANCELLED",
    },
    "AWAITING_REAPPROVAL": {
        "AWAITING_REAPPROVAL",
        "RUNNING",
        "CANCELLED",
    },
    "RUNNING": {
        "RUNNING",
        "AWAITING_REAPPROVAL",
        "COMPLETED",
        "COMPLETED_WITH_UNAPPROVED_REMAINDER",
        "REVIEW_REQUIRED",
    },
    "COMPLETED": {"COMPLETED"},
    "COMPLETED_WITH_UNAPPROVED_REMAINDER": {
        "COMPLETED_WITH_UNAPPROVED_REMAINDER"
    },
    "CANCELLED": {"CANCELLED"},
    "REVIEW_REQUIRED": {"REVIEW_REQUIRED"},
}

ACTION_TRANSITIONS = {
    "PLANNED": {
        "PLANNED",
        "EXECUTING",
        "SKIPPED_RESOLVED",
        "STALE_IDENTITY",
        "REAPPROVAL_REQUIRED",
    },
    "EXECUTING": {
        "EXECUTING",
        # A fail-closed executor may reject authorization before the atomic
        # claim.  The workflow can reload the still-EXECUTING row and safely
        # return that unclaimed intent to the approval queue.
        "PLANNED",
        "APPLYING",
        "IN_DOUBT",
        "RECONCILED_RESOLVED",
        "STALE_IDENTITY",
    },
    "APPLYING": {
        "APPLYING",
        "RESULT_RECORDED",
        "IN_DOUBT",
        "RECONCILED_RESOLVED",
        "STALE_IDENTITY",
    },
    "RESULT_RECORDED": {
        "RESULT_RECORDED",
        "SUCCEEDED",
        "FAILED",
        "INCONCLUSIVE",
    },
    "SUCCEEDED": {"SUCCEEDED"},
    "SKIPPED_RESOLVED": {"SKIPPED_RESOLVED"},
    "RECONCILED_RESOLVED": {"RECONCILED_RESOLVED"},
    "STALE_IDENTITY": {"STALE_IDENTITY"},
    "REAPPROVAL_REQUIRED": {"REAPPROVAL_REQUIRED"},
    "FAILED": {"FAILED"},
    "INCONCLUSIVE": {"INCONCLUSIVE"},
    "IN_DOUBT": {"IN_DOUBT"},
}


class IncidentStoreError(RuntimeError):
    pass


class IncidentNotFound(IncidentStoreError):
    pass


class ConcurrentIncidentUpdate(IncidentStoreError):
    pass


class IncidentLeaseUnavailable(IncidentStoreError):
    pass


class IncidentApprovalUnavailable(IncidentStoreError):
    pass


ACTIVE_INCIDENT_STATES = {
    "AWAITING_APPROVAL",
    "AWAITING_REAPPROVAL",
    "RUNNING",
}


def _json_dump(value: object) -> str:
    return canonical_json(sanitize_audit_value(value))


def _json_load(value: str | None):
    if value is None:
        return None
    return json.loads(value)


def _digest(value: object) -> str:
    return json_digest(value)


def _utc_iso(
    value: datetime | None = None,
) -> str:
    current = value or datetime.now(
        timezone.utc
    )
    if current.tzinfo is None:
        raise ValueError(
            "Workflow timestamps must be timezone-aware."
        )
    return current.astimezone(
        timezone.utc
    ).isoformat()


def _waiter_identity(value: dict) -> tuple:
    return (
        value.get("blocked_pid"),
        value.get("blocked_backend_start"),
        value.get("blocked_xact_start"),
    )


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


class SQLiteIncidentStore:
    def __init__(
        self,
        path: str | Path = INCIDENT_STATE_DB_PATH,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return open_state_database(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "PRAGMA journal_mode = WAL"
            )
            connection.execute(
                "PRAGMA synchronous = FULL"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    schema_version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    workflow_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    plan_revision INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    final_summary_json TEXT,
                    last_error_json TEXT,
                    terminal_reason TEXT,
                    current_approval_id TEXT,
                    version INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    plan_revision INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    state TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    target_json TEXT NOT NULL,
                    allowed_blocked_pids_json TEXT NOT NULL,
                    approved_waiters_json TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    operation_id TEXT,
                    result_json TEXT,
                    last_error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (incident_id)
                        REFERENCES incidents(incident_id)
                        ON DELETE CASCADE,
                    UNIQUE (incident_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    plan_revision INTEGER NOT NULL,
                    scope_digest TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    max_risk TEXT NOT NULL,
                    max_actions INTEGER NOT NULL,
                    approved_actions_json TEXT NOT NULL,
                    FOREIGN KEY (incident_id)
                        REFERENCES incidents(incident_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS workflow_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL,
                    action_id TEXT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (incident_id)
                        REFERENCES incidents(incident_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS
                    idx_incidents_state_updated
                    ON incidents(state, updated_at);

                CREATE INDEX IF NOT EXISTS
                    idx_events_incident
                    ON workflow_events(incident_id, event_id);
                """
            )
            row = connection.execute(
                "SELECT schema_version FROM schema_meta LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_meta(schema_version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif row["schema_version"] != SCHEMA_VERSION:
                raise IncidentStoreError(
                    "Unsupported incident store schema version."
                )

    def create_incident(
        self,
        incident: dict,
    ) -> dict:
        value = deepcopy(incident)
        incident_id = value["incident_id"]
        created_at = value["created_at"]
        actions = list(value.get("actions", []))

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO incidents (
                        incident_id,
                        workflow_type,
                        state,
                        plan_revision,
                        request_json,
                        policy_json,
                        final_summary_json,
                        last_error_json,
                        terminal_reason,
                        current_approval_id,
                        version,
                        lease_owner,
                        lease_expires_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        incident_id,
                        value["workflow_type"],
                        value["state"],
                        value["plan_revision"],
                        _json_dump(value.get("request", {})),
                        _json_dump(value.get("policy", {})),
                        None,
                        None,
                        None,
                        1,
                        created_at,
                        created_at,
                    ),
                )

                for action in actions:
                    self._insert_action(
                        connection,
                        incident_id,
                        action,
                    )

                connection.execute(
                    """
                    INSERT INTO workflow_events (
                        incident_id,
                        action_id,
                        timestamp,
                        event_type,
                        from_state,
                        to_state,
                        payload_json
                    ) VALUES (?, NULL, ?, ?, NULL, ?, ?)
                    """,
                    (
                        incident_id,
                        created_at,
                        "INCIDENT_CREATED",
                        value["state"],
                        _json_dump({
                            "plan_revision": value["plan_revision"],
                            "action_count": len(actions),
                        }),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return self.load_incident(
            incident_id
        )

    @staticmethod
    def _insert_action(
        connection: sqlite3.Connection,
        incident_id: str,
        action: dict,
    ) -> None:
        connection.execute(
            """
            INSERT INTO actions (
                action_id,
                incident_id,
                ordinal,
                plan_revision,
                action_type,
                risk,
                state,
                idempotency_key,
                target_json,
                allowed_blocked_pids_json,
                approved_waiters_json,
                proposal_json,
                attempt_count,
                operation_id,
                result_json,
                last_error_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action["action_id"],
                incident_id,
                action["ordinal"],
                action["plan_revision"],
                action["type"],
                action["risk"],
                action["state"],
                action["idempotency_key"],
                _json_dump(action["target"]),
                _json_dump(action["allowed_blocked_pids"]),
                _json_dump(action["approved_waiters"]),
                _json_dump(action["proposal"]),
                action.get("attempt_count", 0),
                action.get("operation_id"),
                (
                    _json_dump(action["result"])
                    if action.get("result") is not None
                    else None
                ),
                (
                    _json_dump(action["last_error"])
                    if action.get("last_error") is not None
                    else None
                ),
                action["created_at"],
                action["updated_at"],
            ),
        )

    def load_incident(
        self,
        incident_id: str,
    ) -> dict:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise IncidentNotFound(
                    f"Incident not found: {incident_id}"
                )

            action_rows = connection.execute(
                """
                SELECT * FROM actions
                WHERE incident_id = ?
                ORDER BY ordinal ASC
                """,
                (incident_id,),
            ).fetchall()
            approval_row = None
            if row["current_approval_id"] is not None:
                approval_row = connection.execute(
                    """
                    SELECT * FROM approvals
                    WHERE incident_id = ?
                      AND approval_id = ?
                    """,
                    (
                        incident_id,
                        row["current_approval_id"],
                    ),
                ).fetchone()
                if approval_row is None:
                    connection.rollback()
                    raise IncidentStoreError(
                        "Incident references a missing approval."
                    )
            connection.commit()

        incident = {
            "schema_version": SCHEMA_VERSION,
            "incident_id": row["incident_id"],
            "workflow_type": row["workflow_type"],
            "state": row["state"],
            "plan_revision": row["plan_revision"],
            "request": _json_load(row["request_json"]),
            "policy": _json_load(row["policy_json"]),
            "final_summary": _json_load(
                row["final_summary_json"]
            ),
            "last_error": _json_load(
                row["last_error_json"]
            ),
            "terminal_reason": row["terminal_reason"],
            "current_approval_id": row["current_approval_id"],
            "version": row["version"],
            "lease": {
                "owner": row["lease_owner"],
                "expires_at": row["lease_expires_at"],
            },
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "actions": [
                self._load_action(action_row)
                for action_row in action_rows
            ],
            "approval": (
                self._load_approval(approval_row)
                if approval_row is not None
                else None
            ),
        }
        return incident

    @staticmethod
    def _load_action(row: sqlite3.Row) -> dict:
        target = _json_load(row["target_json"])
        return {
            "action_id": row["action_id"],
            "ordinal": row["ordinal"],
            "plan_revision": row["plan_revision"],
            "type": row["action_type"],
            "risk": row["risk"],
            "state": row["state"],
            "idempotency_key": row["idempotency_key"],
            "target": target,
            "target_digest": _digest(target),
            "allowed_blocked_pids": _json_load(
                row["allowed_blocked_pids_json"]
            ),
            "approved_waiters": _json_load(
                row["approved_waiters_json"]
            ),
            "proposal": _json_load(row["proposal_json"]),
            "attempt_count": row["attempt_count"],
            "operation_id": row["operation_id"],
            "result": _json_load(row["result_json"]),
            "last_error": _json_load(row["last_error_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _load_approval(row: sqlite3.Row) -> dict:
        return {
            "kind": "LOCK_INCIDENT_APPROVAL",
            "approval_id": row["approval_id"],
            "incident_id": row["incident_id"],
            "plan_revision": row["plan_revision"],
            "scope_digest": row["scope_digest"],
            "actor": row["actor"],
            "approved_at": row["approved_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "max_risk": row["max_risk"],
            "max_actions": row["max_actions"],
            "approved_actions": _json_load(
                row["approved_actions_json"]
            ),
        }

    def save_incident(
        self,
        incident: dict,
        *,
        event_type: str,
        event_payload: dict | None = None,
        action_id: str | None = None,
        lease_owner: str | None = None,
    ) -> dict:
        value = deepcopy(incident)
        incident_id = value["incident_id"]
        expected_version = value["version"]
        updated_at = value.get(
            "updated_at"
        ) or _utc_iso()

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT state, version, lease_owner,
                           lease_expires_at, current_approval_id
                    FROM incidents
                    WHERE incident_id = ?
                    """,
                    (incident_id,),
                ).fetchone()
                if current is None:
                    raise IncidentNotFound(
                        f"Incident not found: {incident_id}"
                    )
                if current["version"] != expected_version:
                    raise ConcurrentIncidentUpdate(
                        "Incident revision changed before checkpoint."
                    )
                if not isinstance(lease_owner, str) or not lease_owner:
                    raise IncidentLeaseUnavailable(
                        "A live execution lease is required for checkpoints."
                    )
                actual_now = _utc_iso(self.clock())
                if (
                    current["lease_owner"] != lease_owner
                    or current["lease_expires_at"] is None
                    or current["lease_expires_at"] <= actual_now
                ):
                    raise IncidentLeaseUnavailable(
                        "Incident execution lease is absent, expired, or owned "
                        "by another worker."
                    )

                allowed_incident_states = INCIDENT_TRANSITIONS.get(
                    current["state"],
                    set(),
                )
                if value["state"] not in allowed_incident_states:
                    raise IncidentStoreError(
                        f"Illegal incident transition: {current['state']} "
                        f"-> {value['state']}"
                    )

                current_action_rows = connection.execute(
                    """
                    SELECT action_id, state
                    FROM actions
                    WHERE incident_id = ?
                    """,
                    (incident_id,),
                ).fetchall()
                current_action_states = {
                    row["action_id"]: row["state"]
                    for row in current_action_rows
                }
                proposed_action_ids = {
                    action["action_id"]
                    for action in value.get("actions", [])
                }
                if proposed_action_ids != set(current_action_states):
                    raise IncidentStoreError(
                        "Incident action membership cannot change in-place."
                    )
                for action in value.get("actions", []):
                    old_state = current_action_states[action["action_id"]]
                    if action["state"] not in ACTION_TRANSITIONS.get(
                        old_state,
                        set(),
                    ):
                        raise IncidentStoreError(
                            f"Illegal action transition: {old_state} "
                            f"-> {action['state']}"
                        )

                new_version = expected_version + 1
                changed = connection.execute(
                    """
                    UPDATE incidents
                    SET state = ?,
                        plan_revision = ?,
                        request_json = ?,
                        policy_json = ?,
                        final_summary_json = ?,
                        last_error_json = ?,
                        terminal_reason = ?,
                        current_approval_id = ?,
                        version = ?,
                        updated_at = ?
                    WHERE incident_id = ?
                      AND version = ?
                    """,
                    (
                        value["state"],
                        value["plan_revision"],
                        _json_dump(value.get("request", {})),
                        _json_dump(value.get("policy", {})),
                        (
                            _json_dump(value["final_summary"])
                            if value.get("final_summary") is not None
                            else None
                        ),
                        (
                            _json_dump(value["last_error"])
                            if value.get("last_error") is not None
                            else None
                        ),
                        value.get("terminal_reason"),
                        (
                            value["approval"]["approval_id"]
                            if value.get("approval") is not None
                            else current["current_approval_id"]
                        ),
                        new_version,
                        updated_at,
                        incident_id,
                        expected_version,
                    ),
                ).rowcount
                if changed != 1:
                    raise ConcurrentIncidentUpdate(
                        "Incident checkpoint compare-and-swap failed."
                    )

                for action in value.get("actions", []):
                    action_changed = connection.execute(
                        """
                        UPDATE actions
                        SET state = ?,
                            proposal_json = ?,
                            attempt_count = ?,
                            operation_id = ?,
                            result_json = ?,
                            last_error_json = ?,
                            updated_at = ?
                        WHERE action_id = ?
                          AND incident_id = ?
                        """,
                        (
                            action["state"],
                            _json_dump(action["proposal"]),
                            action.get("attempt_count", 0),
                            action.get("operation_id"),
                            (
                                _json_dump(action["result"])
                                if action.get("result") is not None
                                else None
                            ),
                            (
                                _json_dump(action["last_error"])
                                if action.get("last_error") is not None
                                else None
                            ),
                            action.get("updated_at", updated_at),
                            action["action_id"],
                            incident_id,
                        ),
                    ).rowcount
                    if action_changed != 1:
                        raise IncidentStoreError(
                            "Incident action set changed unexpectedly."
                        )

                approval = value.get("approval")
                if approval is not None:
                    approval_values = (
                        approval["approval_id"],
                        incident_id,
                        approval["plan_revision"],
                        approval["scope_digest"],
                        approval["actor"],
                        approval["approved_at"],
                        approval["expires_at"],
                        approval.get("revoked_at"),
                        approval["max_risk"],
                        approval["max_actions"],
                        _json_dump(
                            approval["approved_actions"]
                        ),
                    )
                    existing_approval = connection.execute(
                        """
                        SELECT approval_id, incident_id, plan_revision,
                               scope_digest, actor, approved_at, expires_at,
                               revoked_at, max_risk, max_actions,
                               approved_actions_json
                        FROM approvals
                        WHERE approval_id = ?
                        """,
                        (approval["approval_id"],),
                    ).fetchone()
                    if existing_approval is None:
                        connection.execute(
                            """
                            INSERT INTO approvals (
                            approval_id,
                            incident_id,
                            plan_revision,
                            scope_digest,
                            actor,
                            approved_at,
                            expires_at,
                            revoked_at,
                            max_risk,
                            max_actions,
                            approved_actions_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            approval_values,
                        )
                    elif tuple(existing_approval) != approval_values:
                        raise IncidentStoreError(
                            "A persisted approval cannot be modified in-place."
                        )

                connection.execute(
                    """
                    INSERT INTO workflow_events (
                        incident_id,
                        action_id,
                        timestamp,
                        event_type,
                        from_state,
                        to_state,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        action_id,
                        updated_at,
                        event_type,
                        current["state"],
                        value["state"],
                        _json_dump(event_payload or {}),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return self.load_incident(
            incident_id
        )

    def claim_action_execution(
        self,
        *,
        approval_context: object,
        operation_id: str,
        proposal: object,
        current_evidence: object,
        execution_context: object,
    ) -> dict:
        """Atomically consume a persisted approval for one action attempt.

        All caller-provided values are treated as references or evidence.  The
        approval, action scope, operation identifier, plan revision, and lease
        are reloaded and checked in the same SQLite write transaction that
        changes the action from EXECUTING to APPLYING.
        """
        if not isinstance(approval_context, dict):
            raise IncidentApprovalUnavailable(
                "Incident approval reference must be an object."
            )
        if not isinstance(execution_context, dict):
            raise IncidentApprovalUnavailable(
                "Incident execution context must be an object."
            )
        if not isinstance(proposal, dict):
            raise IncidentApprovalUnavailable(
                "Incident proposal must be an object."
            )
        if not isinstance(current_evidence, dict):
            raise IncidentApprovalUnavailable(
                "Current lock evidence must be an object."
            )
        if not _valid_uuid(operation_id):
            raise IncidentApprovalUnavailable(
                "Incident operation_id must be a valid UUID."
            )

        incident_id = approval_context.get("incident_id")
        approval_id = approval_context.get("approval_id")
        action_id = approval_context.get("current_action_id")
        if not all(
            _valid_uuid(value)
            for value in (incident_id, approval_id, action_id)
        ):
            raise IncidentApprovalUnavailable(
                "Incident approval reference contains an invalid identifier."
            )

        worker_id = execution_context.get("worker_id")
        plan_revision = execution_context.get("plan_revision")
        store_path = execution_context.get("store_path")
        if execution_context.get("kind") != "LOCK_INCIDENT_EXECUTION":
            raise IncidentApprovalUnavailable(
                "Execution context has an unsupported kind."
            )
        if (
            execution_context.get("incident_id") != incident_id
            or execution_context.get("action_id") != action_id
        ):
            raise IncidentApprovalUnavailable(
                "Execution context does not identify the approved action."
            )
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise IncidentApprovalUnavailable(
                "Execution context has no lease owner."
            )
        if not _valid_positive_int(plan_revision):
            raise IncidentApprovalUnavailable(
                "Execution context has an invalid plan revision."
            )
        if not isinstance(store_path, (str, Path)):
            raise IncidentApprovalUnavailable(
                "Execution context has no incident store path."
            )
        try:
            supplied_store_path = _resolved_path(store_path)
            actual_store_path = _resolved_path(self.path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise IncidentApprovalUnavailable(
                "Incident store path could not be resolved."
            ) from exc
        if supplied_store_path != actual_store_path:
            raise IncidentApprovalUnavailable(
                "Execution context references a different incident store."
            )

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                # Sample time only after SQLite grants the write transaction;
                # lock contention must not extend an approval or lease using a
                # timestamp captured before BEGIN IMMEDIATE waited.
                current = self.clock()
                current_iso = _utc_iso(current)
                current_utc = current.astimezone(timezone.utc)
                row = connection.execute(
                    """
                    SELECT
                        i.workflow_type AS incident_workflow_type,
                        i.state AS incident_state,
                        i.version AS incident_version,
                        i.plan_revision AS incident_plan_revision,
                        i.policy_json,
                        i.current_approval_id,
                        i.lease_owner,
                        i.lease_expires_at,
                        a.action_id,
                        a.ordinal,
                        a.plan_revision AS action_plan_revision,
                        a.action_type,
                        a.risk AS action_risk,
                        a.state AS action_state,
                        a.target_json,
                        a.allowed_blocked_pids_json,
                        a.approved_waiters_json,
                        a.proposal_json,
                        a.operation_id AS persisted_operation_id,
                        p.approval_id,
                        p.plan_revision AS approval_plan_revision,
                        p.scope_digest,
                        p.actor,
                        p.approved_at,
                        p.expires_at,
                        p.revoked_at,
                        p.max_risk,
                        p.max_actions,
                        p.approved_actions_json
                    FROM incidents AS i
                    JOIN actions AS a
                      ON a.incident_id = i.incident_id
                    JOIN approvals AS p
                      ON p.incident_id = i.incident_id
                     AND p.approval_id = i.current_approval_id
                    WHERE i.incident_id = ?
                      AND a.action_id = ?
                    """,
                    (incident_id, action_id),
                ).fetchone()
                if row is None:
                    raise IncidentApprovalUnavailable(
                        "No current persisted approval exists for this action."
                    )

                if row["incident_workflow_type"] != "LOCK_CONTENTION":
                    raise IncidentApprovalUnavailable(
                        "Persisted incident type cannot authorize this action."
                    )
                if row["incident_state"] != "RUNNING":
                    raise IncidentApprovalUnavailable(
                        "Incident is not in an executable state."
                    )
                try:
                    policy = _json_load(row["policy_json"])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise IncidentApprovalUnavailable(
                        "Persisted incident policy is invalid."
                    ) from exc
                deadline_at = (
                    parse_utc_timestamp(policy.get("deadline_at"))
                    if isinstance(policy, dict)
                    else None
                )
                if deadline_at is None or current_utc >= deadline_at:
                    raise IncidentApprovalUnavailable(
                        "Incident workflow deadline is invalid or expired."
                    )
                if row["action_state"] != "EXECUTING":
                    raise IncidentApprovalUnavailable(
                        "Action intent is absent or has already been claimed."
                    )
                if (
                    row["action_type"] != "TERMINATE_BACKEND"
                    or row["action_risk"] != "HIGH"
                ):
                    raise IncidentApprovalUnavailable(
                        "Persisted action is outside the supported risk scope."
                    )
                if (
                    row["current_approval_id"] != approval_id
                    or row["approval_id"] != approval_id
                ):
                    raise IncidentApprovalUnavailable(
                        "Approval is not the incident's current approval."
                    )
                if (
                    row["incident_plan_revision"] != plan_revision
                    or row["action_plan_revision"] != plan_revision
                    or row["approval_plan_revision"] != plan_revision
                ):
                    raise IncidentApprovalUnavailable(
                        "Approval, incident, action, and execution plan differ."
                    )
                if row["persisted_operation_id"] != operation_id:
                    raise IncidentApprovalUnavailable(
                        "Operation identifier does not match the durable intent."
                    )

                lease_expires_at = parse_utc_timestamp(
                    row["lease_expires_at"]
                )
                if (
                    row["lease_owner"] != worker_id
                    or lease_expires_at is None
                    or lease_expires_at <= current_utc
                ):
                    raise IncidentLeaseUnavailable(
                        "Incident execution lease is absent, expired, or owned "
                        "by another worker."
                    )

                persisted_proposal = _json_load(row["proposal_json"])
                if _json_dump(persisted_proposal) != _json_dump(proposal):
                    raise IncidentApprovalUnavailable(
                        "Proposal does not match the durable action intent."
                    )

                target = _json_load(row["target_json"])
                allowed_blocked_pids = _json_load(
                    row["allowed_blocked_pids_json"]
                )
                approved_waiters = _json_load(
                    row["approved_waiters_json"]
                )
                # The worker injects this deny-only check from its private
                # grant database, never from the HTTP caller. Recheck while
                # holding the write lock: Agent state may have changed since
                # the separate operator grant was claimed.
                isolated_grant = execution_context.get("isolated_grant")
                if isolated_grant is not None:
                    from execution_protocol import action_scope, digest
                    expiry = isolated_grant.get("expires_at") if isinstance(isolated_grant, dict) else None
                    scope = action_scope(
                        {"incident_id": incident_id, "plan_revision": row["incident_plan_revision"]},
                        {"action_id": action_id, "proposal": persisted_proposal, "target": target,
                         "approved_waiters": approved_waiters, "allowed_blocked_pids": allowed_blocked_pids},
                    )
                    if (type(expiry) not in (int, float) or not math.isfinite(expiry)
                            or current_utc.timestamp() >= expiry
                            or isolated_grant.get("scope_digest") != digest(scope)):
                        raise IncidentApprovalUnavailable("Worker-side grant expired or action scope changed before execution claim.")
                if (
                    not isinstance(allowed_blocked_pids, list)
                    or not isinstance(approved_waiters, list)
                    or not approved_waiters
                ):
                    raise IncidentApprovalUnavailable(
                        "Persisted action has no valid waiter scope."
                    )
                waiter_identities = {
                    _waiter_identity(waiter)
                    for waiter in approved_waiters
                    if isinstance(waiter, dict)
                }
                waiter_pids = {
                    waiter[0]
                    for waiter in waiter_identities
                    if _valid_positive_int(waiter[0])
                }
                if (
                    len(waiter_identities) != len(approved_waiters)
                    or any(
                        not _valid_positive_int(waiter[0])
                        or not isinstance(waiter[1], str)
                        or not waiter[1].strip()
                        or not isinstance(waiter[2], str)
                        or not waiter[2].strip()
                        for waiter in waiter_identities
                    )
                    or set(allowed_blocked_pids) != waiter_pids
                ):
                    raise IncidentApprovalUnavailable(
                        "Persisted waiter identities are incomplete or inconsistent."
                    )

                evidence_waiter = _waiter_identity(current_evidence)
                if evidence_waiter not in waiter_identities:
                    raise IncidentApprovalUnavailable(
                        "Current blocked backend identity is outside approval scope."
                    )
                if (
                    current_evidence.get("database_name")
                    != target.get("database_name")
                    or current_evidence.get("blocked_pid")
                    != proposal.get("blocked_pid")
                    or current_evidence.get("blocker_pid")
                    != target.get("blocker_pid")
                    or current_evidence.get("blocker_backend_start")
                    != target.get("blocker_backend_start")
                    or current_evidence.get("blocker_xact_start")
                    != target.get("blocker_xact_start")
                ):
                    raise IncidentApprovalUnavailable(
                        "Current lock evidence does not match the approved target."
                    )

                approved_actions = _json_load(
                    row["approved_actions_json"]
                )
                authoritative_context = {
                    "kind": "LOCK_INCIDENT_APPROVAL",
                    "approval_id": row["approval_id"],
                    "incident_id": incident_id,
                    "plan_revision": row["approval_plan_revision"],
                    "scope_digest": row["scope_digest"],
                    "actor": row["actor"],
                    "approved_at": row["approved_at"],
                    "expires_at": row["expires_at"],
                    "revoked_at": row["revoked_at"],
                    "max_risk": row["max_risk"],
                    "max_actions": row["max_actions"],
                    "approved_actions": approved_actions,
                    "current_action_id": action_id,
                }
                approval_validation = validate_approval_context(
                    proposal,
                    authoritative_context,
                    now=current_utc,
                )
                if not approval_validation["valid"]:
                    raise IncidentApprovalUnavailable(
                        "Persisted approval is invalid: "
                        + "; ".join(approval_validation["errors"])
                    )

                expected_action_scope = canonical_approved_actions([{
                    "action_id": action_id,
                    "ordinal": row["ordinal"],
                    "target": target,
                    "allowed_blocked_pids": allowed_blocked_pids,
                    "approved_waiters": approved_waiters,
                }])[0]
                canonical_scope = canonical_approved_actions(
                    approved_actions
                )
                matching_scopes = [
                    item
                    for item in canonical_scope
                    if item["action_id"] == action_id
                ]
                if matching_scopes != [expected_action_scope]:
                    raise IncidentApprovalUnavailable(
                        "Persisted action differs from the approved action scope."
                    )

                expected_digest = compute_scope_digest(
                    incident_id=incident_id,
                    plan_revision=row["approval_plan_revision"],
                    approved_actions=approved_actions,
                    max_actions=row["max_actions"],
                    max_risk=row["max_risk"],
                )
                if row["scope_digest"] != expected_digest:
                    raise IncidentApprovalUnavailable(
                        "Persisted approval scope digest does not match."
                    )

                changed = connection.execute(
                    """
                    UPDATE actions
                    SET state = 'APPLYING',
                        updated_at = ?
                    WHERE incident_id = ?
                      AND action_id = ?
                      AND state = 'EXECUTING'
                      AND operation_id = ?
                    """,
                    (
                        current_iso,
                        incident_id,
                        action_id,
                        operation_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise IncidentApprovalUnavailable(
                        "Action intent was concurrently claimed or changed."
                    )

                incident_changed = connection.execute(
                    """
                    UPDATE incidents
                    SET version = version + 1,
                        updated_at = ?
                    WHERE incident_id = ?
                      AND version = ?
                      AND state = 'RUNNING'
                      AND current_approval_id = ?
                      AND lease_owner = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        current_iso,
                        incident_id,
                        row["incident_version"],
                        approval_id,
                        worker_id,
                        current_iso,
                    ),
                ).rowcount
                if incident_changed != 1:
                    raise IncidentApprovalUnavailable(
                        "Incident revision, approval, or lease changed while "
                        "claiming the action."
                    )

                evidence_digest = _digest(
                    sanitize_audit_value(current_evidence)
                )
                authorization = {
                    "kind": "PERSISTED_ACTION_EXECUTION_CLAIM",
                    "incident_id": incident_id,
                    "approval_id": approval_id,
                    "action_id": action_id,
                    "operation_id": operation_id,
                    "plan_revision": plan_revision,
                    "scope_digest": row["scope_digest"],
                    "actor": row["actor"],
                    "approved_at": row["approved_at"],
                    "expires_at": row["expires_at"],
                    "lease_owner": worker_id,
                    "waiter_identity": {
                        "blocked_pid": evidence_waiter[0],
                        "blocked_backend_start": evidence_waiter[1],
                        "blocked_xact_start": evidence_waiter[2],
                    },
                    "evidence_digest": evidence_digest,
                    "claimed_at": current_iso,
                    "incident_version": row["incident_version"] + 1,
                }
                connection.execute(
                    """
                    INSERT INTO workflow_events (
                        incident_id, action_id, timestamp,
                        event_type, from_state, to_state, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        action_id,
                        current_iso,
                        "ACTION_EXECUTION_CLAIMED",
                        "EXECUTING",
                        "APPLYING",
                        _json_dump(authorization),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return authorization

    def acquire_lease(
        self,
        incident_id: str,
        *,
        owner: str,
        now: datetime,
        lease_seconds: float,
    ) -> dict:
        # Keep ``now`` in the public signature for workflow compatibility, but
        # never let a caller backdate a security-sensitive lease decision.
        _utc_iso(now)
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Lease owner must be non-empty.")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("Lease duration must be positive and finite.")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                authoritative_now = self.clock()
                now_iso = _utc_iso(authoritative_now)
                expires_iso = _utc_iso(
                    authoritative_now + timedelta(
                        seconds=lease_seconds
                    )
                )
                row = connection.execute(
                    """
                    SELECT state, version
                    FROM incidents
                    WHERE incident_id = ?
                    """,
                    (incident_id,),
                ).fetchone()
                if row is None:
                    raise IncidentNotFound(
                        f"Incident not found: {incident_id}"
                    )
                if row["state"] not in ACTIVE_INCIDENT_STATES:
                    raise IncidentLeaseUnavailable(
                        "A terminal incident cannot acquire an execution lease."
                    )

                changed = connection.execute(
                    """
                    UPDATE incidents
                    SET lease_owner = ?,
                        lease_expires_at = ?,
                        version = version + 1,
                        updated_at = ?
                    WHERE incident_id = ?
                      AND state IN (
                          'AWAITING_APPROVAL',
                          'AWAITING_REAPPROVAL',
                          'RUNNING'
                      )
                      AND (
                          lease_owner IS NULL
                          OR lease_expires_at <= ?
                          OR lease_owner = ?
                      )
                    """,
                    (
                        owner,
                        expires_iso,
                        now_iso,
                        incident_id,
                        now_iso,
                        owner,
                    ),
                ).rowcount
                if changed != 1:
                    raise IncidentLeaseUnavailable(
                        "Incident is currently owned by another worker."
                    )

                connection.execute(
                    """
                    INSERT INTO workflow_events (
                        incident_id, action_id, timestamp,
                        event_type, from_state, to_state, payload_json
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        now_iso,
                        "LEASE_ACQUIRED",
                        row["state"],
                        row["state"],
                        _json_dump({
                            "owner": owner,
                            "expires_at": expires_iso,
                        }),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return self.load_incident(
            incident_id
        )

    def renew_lease(
        self,
        incident_id: str,
        *,
        owner: str,
        now: datetime,
        lease_seconds: float,
    ) -> dict:
        _utc_iso(now)
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Lease owner must be non-empty.")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("Lease duration must be positive and finite.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            authoritative_now = self.clock()
            now_iso = _utc_iso(authoritative_now)
            expires_iso = _utc_iso(
                authoritative_now + timedelta(seconds=lease_seconds)
            )
            changed = connection.execute(
                """
                UPDATE incidents
                SET lease_expires_at = ?,
                    version = version + 1,
                    updated_at = ?
                WHERE incident_id = ?
                  AND lease_owner = ?
                  AND lease_expires_at > ?
                  AND state IN (
                      'AWAITING_APPROVAL',
                      'AWAITING_REAPPROVAL',
                      'RUNNING'
                  )
                """,
                (
                    expires_iso,
                    now_iso,
                    incident_id,
                    owner,
                    now_iso,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise IncidentLeaseUnavailable(
                    "Incident execution lease expired or changed."
                )
            connection.commit()
        return self.load_incident(
            incident_id
        )

    def release_lease(
        self,
        incident_id: str,
        *,
        owner: str,
        now: datetime,
    ) -> dict:
        _utc_iso(now)
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Lease owner must be non-empty.")
        now_iso = _utc_iso(self.clock())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise IncidentNotFound(
                    f"Incident not found: {incident_id}"
                )
            changed = connection.execute(
                """
                UPDATE incidents
                SET lease_owner = NULL,
                    lease_expires_at = NULL,
                    version = version + 1,
                    updated_at = ?
                WHERE incident_id = ?
                  AND lease_owner = ?
                """,
                (now_iso, incident_id, owner),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise IncidentLeaseUnavailable(
                    "Incident execution lease is not owned by this worker."
                )
            connection.execute(
                """
                INSERT INTO workflow_events (
                    incident_id, action_id, timestamp,
                    event_type, from_state, to_state, payload_json
                ) VALUES (?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    now_iso,
                    "LEASE_RELEASED",
                    row["state"],
                    row["state"],
                    _json_dump({"owner": owner}),
                ),
            )
            connection.commit()
        return self.load_incident(
            incident_id
        )

    def list_incidents(
        self,
        *,
        limit: int = 20,
    ) -> list[dict]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > 100
        ):
            raise ValueError(
                "Incident list limit must be between 1 and 100."
            )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT incident_id, workflow_type, state,
                       plan_revision, version, created_at, updated_at
                FROM incidents
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(
        self,
        incident_id: str,
    ) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_events
                WHERE incident_id = ?
                ORDER BY event_id ASC
                """,
                (incident_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": _json_load(row["payload_json"]),
            }
            for row in rows
        ]
