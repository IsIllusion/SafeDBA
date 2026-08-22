import hashlib
import json
import math
import uuid

from copy import deepcopy
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from typing import Callable

from actions import validate_proposal_shape
from config import (
    INCIDENT_APPROVAL_TTL_SECONDS,
    INCIDENT_DEADLINE_SECONDS,
    INCIDENT_LEASE_SECONDS,
    INCIDENT_MAX_ACTIONS,
)
from db_tools import get_lock_graph_snapshot
from executor import execute_action_proposal
from incident_approval import (
    APPROVAL_KIND,
    canonical_approved_actions,
    compute_scope_digest,
    parse_utc_timestamp,
    target_key,
    validate_approval_context,
)
from workflow_store import (
    IncidentLeaseUnavailable,
    SQLiteIncidentStore,
)


WORKFLOW_TYPE = "LOCK_CONTENTION"

TERMINAL_STATES = {
    "COMPLETED",
    "COMPLETED_WITH_UNAPPROVED_REMAINDER",
    "CANCELLED",
    "REVIEW_REQUIRED",
}

FINISHED_ACTION_STATES = {
    "SUCCEEDED",
    "SKIPPED_RESOLVED",
    "RECONCILED_RESOLVED",
}

SAFE_BLOCKER_STATES = {
    "idle in transaction",
    "idle in transaction (aborted)",
}


class IncidentWorkflowError(RuntimeError):
    pass


class IncidentPlanError(IncidentWorkflowError):
    pass


def utc_now() -> datetime:
    return datetime.now(
        timezone.utc
    )


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError(
            "Workflow timestamps must be timezone-aware."
        )
    return value.astimezone(
        timezone.utc
    ).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _valid_positive_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def _nonblank(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
    )


def _snapshot_rows(snapshot: dict) -> list[dict]:
    if not isinstance(snapshot, dict):
        raise IncidentPlanError(
            "Lock snapshot must be an object."
        )
    if snapshot.get("truncated") is not False:
        raise IncidentPlanError(
            "The lock snapshot is truncated or has no completeness flag."
        )
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise IncidentPlanError(
            "Lock snapshot rows must be a list."
        )
    if any(not isinstance(row, dict) for row in rows):
        raise IncidentPlanError(
            "Every lock relationship must be an object."
        )
    return rows


def relationship_policy_errors(
    row: dict,
    *,
    database_name: str | None,
) -> list[str]:
    errors = []
    blocked_pid = row.get("blocked_pid")
    blocker_pid = row.get("blocker_pid")

    if not _valid_positive_int(blocked_pid):
        errors.append("invalid blocked_pid")
    if not _valid_positive_int(blocker_pid):
        errors.append("invalid blocker_pid")
    if (
        _valid_positive_int(blocked_pid)
        and _valid_positive_int(blocker_pid)
        and blocked_pid == blocker_pid
    ):
        errors.append("blocked and blocker PIDs are equal")

    for field in (
        "blocked_backend_start",
        "blocked_xact_start",
        "blocker_backend_start",
        "blocker_xact_start",
    ):
        if not _nonblank(row.get(field)):
            errors.append(f"missing {field}")

    if row.get("blocked_wait_event_type") != "Lock":
        errors.append("blocked backend is not waiting on a Lock event")
    if row.get("blocker_state") not in SAFE_BLOCKER_STATES:
        errors.append("blocker is not idle in transaction")
    if row.get("blocker_backend_type") != "client backend":
        errors.append("blocker is not a client backend")

    row_database = row.get("database_name")
    blocker_database = row.get("blocker_database_name")
    if not _nonblank(row_database):
        errors.append("blocked database is missing")
    if not _nonblank(blocker_database):
        errors.append("blocker database is missing")
    if (
        _nonblank(database_name)
        and row_database != database_name
    ):
        errors.append("blocked backend is outside the snapshot database")
    if blocker_database != row_database:
        errors.append("blocker and blocked backend databases differ")

    return errors


def actionable_relationships(
    snapshot: dict,
) -> list[dict]:
    rows = _snapshot_rows(snapshot)
    database_name = snapshot.get("database_name")
    return [
        row
        for row in rows
        if not relationship_policy_errors(
            row,
            database_name=database_name,
        )
    ]


def _relationship_target(row: dict) -> dict:
    return {
        "database_name": row.get("database_name"),
        "blocker_pid": row.get("blocker_pid"),
        "blocker_backend_start": row.get(
            "blocker_backend_start"
        ),
        "blocker_xact_start": row.get(
            "blocker_xact_start"
        ),
    }


def _waiter_identity(row: dict) -> dict:
    return {
        "blocked_pid": row.get("blocked_pid"),
        "blocked_backend_start": row.get(
            "blocked_backend_start"
        ),
        "blocked_xact_start": row.get(
            "blocked_xact_start"
        ),
    }


def _waiter_key(value: dict) -> tuple:
    return (
        value.get("blocked_pid"),
        value.get("blocked_backend_start"),
        value.get("blocked_xact_start"),
    )


def _proposal_matches_row(
    proposal: dict,
    row: dict,
) -> bool:
    return (
        proposal.get("blocked_pid") == row.get("blocked_pid")
        and proposal.get("blocker_pid") == row.get("blocker_pid")
        and proposal.get("blocker_backend_start")
        == row.get("blocker_backend_start")
        and proposal.get("blocker_xact_start")
        == row.get("blocker_xact_start")
    )


def _safe_confidence(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > 1
    ):
        return 1.0
    return float(value)


def build_termination_plan(
    *,
    incident_id: str,
    snapshot: dict,
    proposals: list[dict],
    expand_all_actionable: bool = False,
    max_actions: int = INCIDENT_MAX_ACTIONS,
    now: datetime | None = None,
) -> list[dict]:
    rows = _snapshot_rows(snapshot)
    eligible = actionable_relationships(snapshot)

    if not isinstance(proposals, list) or not proposals:
        raise IncidentPlanError(
            "At least one Agent termination proposal is required."
        )

    proposal_matches: list[tuple[dict, dict]] = []
    validation_errors: list[str] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            validation_errors.append(
                f"proposal {index + 1} is not an object"
            )
            continue
        if proposal.get("type") != "TERMINATE_BACKEND":
            validation_errors.append(
                f"proposal {index + 1} is not TERMINATE_BACKEND"
            )
            continue
        shape = validate_proposal_shape(proposal)
        if not shape["valid"]:
            validation_errors.extend(
                f"proposal {index + 1}: {error}"
                for error in shape["errors"]
            )
            continue
        match = next(
            (
                row
                for row in eligible
                if _proposal_matches_row(proposal, row)
            ),
            None,
        )
        if match is None:
            raw_match = next(
                (
                    row
                    for row in rows
                    if _proposal_matches_row(proposal, row)
                ),
                None,
            )
            if raw_match is None:
                validation_errors.append(
                    f"proposal {index + 1} is stale or absent from the snapshot"
                )
            else:
                reasons = relationship_policy_errors(
                    raw_match,
                    database_name=snapshot.get("database_name"),
                )
                validation_errors.append(
                    f"proposal {index + 1} is outside policy: "
                    + ", ".join(reasons)
                )
            continue
        proposal_matches.append((proposal, match))

    if validation_errors:
        raise IncidentPlanError(
            "Cannot create a lock incident: "
            + "; ".join(validation_errors)
        )

    approved_target_keys = {
        target_key(_relationship_target(row))
        for _, row in proposal_matches
    }
    if expand_all_actionable:
        approved_target_keys.update(
            target_key(_relationship_target(row))
            for row in eligible
        )

    identities_by_pid: dict[int, set[tuple]] = {}
    for row in eligible:
        pid = row["blocker_pid"]
        identities_by_pid.setdefault(pid, set()).add(
            target_key(_relationship_target(row))
        )
    conflicting_pids = [
        pid
        for pid, identities in identities_by_pid.items()
        if len(identities) > 1
    ]
    if conflicting_pids:
        raise IncidentPlanError(
            "Conflicting backend identities were observed for blocker PID(s): "
            + ", ".join(str(pid) for pid in sorted(conflicting_pids))
        )

    grouped: dict[tuple, list[dict]] = {}
    for row in eligible:
        key = target_key(_relationship_target(row))
        if key in approved_target_keys:
            grouped.setdefault(key, []).append(row)

    if not grouped:
        raise IncidentPlanError(
            "No currently actionable blocker matched the approved scope."
        )
    if len(grouped) > max_actions:
        raise IncidentPlanError(
            "The incident exceeds the configured action limit."
        )

    blocked_pid_set = {
        row["blocked_pid"]
        for row in eligible
    }
    ordered_groups = sorted(
        grouped.items(),
        key=lambda item: (
            item[0][1] in blocked_pid_set,
            item[0][3],
            item[0][1],
        ),
    )
    timestamp = iso_utc(now or utc_now())
    actions = []

    for ordinal, (key, group_rows) in enumerate(
        ordered_groups,
        start=1,
    ):
        target = _relationship_target(group_rows[0])
        allowed_blocked_pids = sorted({
            row["blocked_pid"]
            for row in group_rows
        })
        approved_waiters = sorted(
            {
                _waiter_key(_waiter_identity(row))
                for row in group_rows
            },
            key=lambda waiter: (
                waiter[0],
                waiter[1],
                waiter[2],
            ),
        )
        approved_waiters = [
            {
                "blocked_pid": waiter[0],
                "blocked_backend_start": waiter[1],
                "blocked_xact_start": waiter[2],
            }
            for waiter in approved_waiters
        ]
        matching_proposals = [
            proposal
            for proposal, row in proposal_matches
            if target_key(_relationship_target(row)) == key
        ]
        proposed_blocked_pids = [
            proposal["blocked_pid"]
            for proposal in matching_proposals
            if proposal["blocked_pid"] in allowed_blocked_pids
        ]
        representative = (
            min(proposed_blocked_pids)
            if proposed_blocked_pids
            else min(allowed_blocked_pids)
        )
        confidence = max(
            (
                _safe_confidence(proposal.get("confidence"))
                for proposal in matching_proposals
            ),
            default=1.0,
        )
        proposal = {
            "type": "TERMINATE_BACKEND",
            "database_name": target["database_name"],
            "blocked_pid": representative,
            "blocker_pid": target["blocker_pid"],
            "blocker_backend_start": target[
                "blocker_backend_start"
            ],
            "blocker_xact_start": target[
                "blocker_xact_start"
            ],
            "reason": (
                "IncidentWorkflow identified this exact idle-in-transaction "
                "backend as a current lock blocker."
            ),
            "confidence": confidence,
            "risk": "HIGH",
        }
        action_id = str(uuid.uuid4())
        target_digest = _sha256(target)
        idempotency_key = _sha256({
            "incident_id": incident_id,
            "plan_revision": 1,
            "target_digest": target_digest,
        })
        actions.append({
            "action_id": action_id,
            "ordinal": ordinal,
            "plan_revision": 1,
            "type": "TERMINATE_BACKEND",
            "risk": "HIGH",
            "state": "PLANNED",
            "idempotency_key": idempotency_key,
            "target": target,
            "target_digest": target_digest,
            "allowed_blocked_pids": allowed_blocked_pids,
            "approved_waiters": approved_waiters,
            "proposal": proposal,
            "attempt_count": 0,
            "operation_id": action_id,
            "result": None,
            "last_error": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        })

    return actions


def summarize_snapshot(snapshot: dict) -> dict:
    rows = _snapshot_rows(snapshot)
    return {
        "captured_at": snapshot.get("captured_at"),
        "database_name": snapshot.get("database_name"),
        "snapshot_digest": snapshot.get("snapshot_digest"),
        "row_count": len(rows),
        "truncated": False,
        "relationships": [
            {
                "blocked_pid": row.get("blocked_pid"),
                "blocked_backend_start": row.get(
                    "blocked_backend_start"
                ),
                "blocked_xact_start": row.get(
                    "blocked_xact_start"
                ),
                "blocker_pid": row.get("blocker_pid"),
                "blocker_backend_start": row.get(
                    "blocker_backend_start"
                ),
                "blocker_xact_start": row.get(
                    "blocker_xact_start"
                ),
                "blocker_state": row.get("blocker_state"),
                "eligible": not relationship_policy_errors(
                    row,
                    database_name=snapshot.get("database_name"),
                ),
            }
            for row in rows
        ],
    }


def create_lock_incident(
    *,
    proposals: list[dict],
    user_request: str,
    expand_all_actionable: bool = False,
    store: SQLiteIncidentStore | None = None,
    observe_locks: Callable[[], dict] = get_lock_graph_snapshot,
    now: Callable[[], datetime] = utc_now,
) -> dict:
    incident_store = store or SQLiteIncidentStore()
    current = now()
    snapshot = observe_locks()
    incident_id = str(uuid.uuid4())
    actions = build_termination_plan(
        incident_id=incident_id,
        snapshot=snapshot,
        proposals=proposals,
        expand_all_actionable=expand_all_actionable,
        max_actions=INCIDENT_MAX_ACTIONS,
        now=current,
    )
    request_text = (
        user_request
        if isinstance(user_request, str)
        else str(user_request)
    )
    incident = {
        "schema_version": 1,
        "incident_id": incident_id,
        "workflow_type": WORKFLOW_TYPE,
        "state": "AWAITING_APPROVAL",
        "plan_revision": 1,
        "request": {
            "sha256": hashlib.sha256(
                request_text.encode("utf-8")
            ).hexdigest(),
            "characters": len(request_text),
            "raw_text_stored": False,
        },
        "policy": {
            "max_actions": INCIDENT_MAX_ACTIONS,
            "approval_ttl_seconds": (
                INCIDENT_APPROVAL_TTL_SECONDS
            ),
            "lease_seconds": INCIDENT_LEASE_SECONDS,
            "deadline_at": iso_utc(
                current
                + timedelta(seconds=INCIDENT_DEADLINE_SECONDS)
            ),
            "stop_on_first_failure": True,
            "initial_snapshot": summarize_snapshot(snapshot),
        },
        "final_summary": None,
        "last_error": None,
        "terminal_reason": None,
        "version": 0,
        "lease": {
            "owner": None,
            "expires_at": None,
        },
        "approval": None,
        "actions": actions,
        "created_at": iso_utc(current),
        "updated_at": iso_utc(current),
    }
    return incident_store.create_incident(
        incident
    )


def build_approval(
    incident: dict,
    *,
    actor: str,
    now: datetime,
) -> dict:
    approved_actions = canonical_approved_actions(
        incident["actions"]
    )
    max_actions = min(
        incident["policy"]["max_actions"],
        len(approved_actions),
    )
    approved_at = iso_utc(now)
    expires_at = iso_utc(
        now
        + timedelta(
            seconds=incident["policy"][
                "approval_ttl_seconds"
            ]
        )
    )
    scope_digest = compute_scope_digest(
        incident_id=incident["incident_id"],
        plan_revision=incident["plan_revision"],
        approved_actions=approved_actions,
        max_actions=max_actions,
        max_risk="HIGH",
    )
    return {
        "kind": APPROVAL_KIND,
        "approval_id": str(uuid.uuid4()),
        "incident_id": incident["incident_id"],
        "plan_revision": incident["plan_revision"],
        "scope_digest": scope_digest,
        "actor": actor,
        "approved_at": approved_at,
        "expires_at": expires_at,
        "revoked_at": None,
        "max_risk": "HIGH",
        "max_actions": max_actions,
        "approved_actions": approved_actions,
    }


def _target_rows(
    rows: list[dict],
    action: dict,
) -> list[dict]:
    key = target_key(action["target"])
    return [
        row
        for row in rows
        if target_key(_relationship_target(row)) == key
    ]


def _approved_target_rows(
    rows: list[dict],
    action: dict,
) -> list[dict]:
    allowed = {
        _waiter_key(waiter)
        for waiter in action.get(
            "approved_waiters",
            [],
        )
        if isinstance(waiter, dict)
    }
    return [
        row
        for row in _target_rows(rows, action)
        if _waiter_key(_waiter_identity(row)) in allowed
    ]


def _pid_reused(
    rows: list[dict],
    action: dict,
) -> bool:
    expected = target_key(action["target"])
    blocker_pid = action["target"]["blocker_pid"]
    return any(
        row.get("blocker_pid") == blocker_pid
        and target_key(_relationship_target(row)) != expected
        for row in rows
    )


def _approval_context_for_action(
    incident: dict,
    action: dict,
) -> dict:
    context = deepcopy(incident["approval"])
    context["current_action_id"] = action["action_id"]
    return context


def _action_by_id(
    incident: dict,
    action_id: str,
) -> dict:
    for action in incident.get("actions", []):
        if action.get("action_id") == action_id:
            return action
    raise IncidentWorkflowError(
        f"Incident action not found: {action_id}"
    )


def _execution_context_for_action(
    *,
    store: SQLiteIncidentStore,
    incident: dict,
    action: dict,
    worker_id: str,
) -> dict:
    return {
        "kind": "LOCK_INCIDENT_EXECUTION",
        "store_path": str(store.path.resolve()),
        "worker_id": worker_id,
        "incident_id": incident["incident_id"],
        "action_id": action["action_id"],
        "plan_revision": incident["plan_revision"],
    }


def _checkpoint(
    store: SQLiteIncidentStore,
    incident: dict,
    *,
    worker_id: str,
    timestamp: datetime,
    event_type: str,
    payload: dict | None = None,
    action_id: str | None = None,
) -> dict:
    incident["updated_at"] = iso_utc(timestamp)
    return store.save_incident(
        incident,
        event_type=event_type,
        event_payload=payload,
        action_id=action_id,
        lease_owner=worker_id,
    )


def _decision_value(
    decision: object,
) -> tuple[bool, str]:
    if isinstance(decision, bool):
        return decision, "interactive-user"
    if isinstance(decision, dict):
        approved = decision.get("approved") is True
        actor = decision.get("actor", "interactive-user")
        if not _nonblank(actor):
            actor = "interactive-user"
        return approved, actor
    return False, "interactive-user"


def _approval_is_current(
    incident: dict,
    action: dict,
    *,
    now: datetime,
) -> dict:
    context = _approval_context_for_action(
        incident,
        action,
    )
    return validate_approval_context(
        action["proposal"],
        context,
        now=now,
    )


def _workflow_deadline_passed(
    incident: dict,
    current: datetime,
) -> bool:
    deadline = parse_utc_timestamp(
        incident["policy"].get("deadline_at")
    )
    return deadline is None or current >= deadline


def _executor_result_confirmed(
    result: object,
) -> bool:
    audit = (
        result.get("audit")
        if isinstance(result, dict)
        else None
    )
    return bool(
        isinstance(result, dict)
        and result.get("status")
        == "BACKEND_TERMINATION_CONFIRMED"
        and result.get("blocking_relationship_removed") is True
        and isinstance(audit, dict)
        and audit.get("status") == "WRITTEN"
    )


def _reconcile_inflight(
    incident: dict,
    *,
    snapshot: dict,
    store: SQLiteIncidentStore,
    worker_id: str,
    now: datetime,
) -> tuple[dict, bool]:
    rows = _snapshot_rows(snapshot)
    inflight = [
        action
        for action in incident["actions"]
        if action["state"] in {
            "EXECUTING",
            "APPLYING",
            "RESULT_RECORDED",
        }
    ]
    if not inflight:
        return incident, True
    if len(inflight) > 1:
        incident["state"] = "REVIEW_REQUIRED"
        incident["last_error"] = {
            "type": "MULTIPLE_INFLIGHT_ACTIONS",
            "message": "More than one high-risk action was in flight.",
        }
        return _checkpoint(
            store,
            incident,
            worker_id=worker_id,
            timestamp=now,
            event_type="RECONCILIATION_BLOCKED",
            payload=incident["last_error"],
        ), False

    action = inflight[0]
    exact_rows = _target_rows(rows, action)

    if action["state"] == "RESULT_RECORDED":
        result = action.get("result")
        if not _executor_result_confirmed(result):
            action["state"] = "FAILED"
            action["last_error"] = {
                "type": "RECORDED_EXECUTOR_RESULT_NOT_CONFIRMED",
                "status": (
                    result.get("status")
                    if isinstance(result, dict)
                    else None
                ),
                "message": (
                    "A durable executor result exists but does not prove a "
                    "successful audited termination."
                ),
            }
            action["updated_at"] = iso_utc(now)
            incident["state"] = "REVIEW_REQUIRED"
            incident["last_error"] = action["last_error"]
            return _checkpoint(
                store,
                incident,
                worker_id=worker_id,
                timestamp=now,
                event_type="RECORDED_RESULT_FAILED_CLOSED",
                payload=action["last_error"],
                action_id=action["action_id"],
            ), False

        if exact_rows:
            action["state"] = "INCONCLUSIVE"
            action["last_error"] = {
                "type": "RECORDED_SUCCESS_TARGET_STILL_PRESENT",
                "message": (
                    "The executor recorded success, but the exact blocker "
                    "identity still appears in the fresh lock graph."
                ),
            }
            action["updated_at"] = iso_utc(now)
            incident["state"] = "REVIEW_REQUIRED"
            incident["last_error"] = action["last_error"]
            return _checkpoint(
                store,
                incident,
                worker_id=worker_id,
                timestamp=now,
                event_type="RECORDED_RESULT_VERIFICATION_FAILED",
                payload=action["last_error"],
                action_id=action["action_id"],
            ), False

        action["state"] = "SUCCEEDED"
        action["last_error"] = None
        action["updated_at"] = iso_utc(now)
        incident = _checkpoint(
            store,
            incident,
            worker_id=worker_id,
            timestamp=now,
            event_type="RECORDED_RESULT_RECONCILED",
            payload={
                "operation_id": action.get("operation_id"),
                "side_effect_repeated": False,
            },
            action_id=action["action_id"],
        )
        return incident, True

    if _pid_reused(rows, action):
        action["state"] = "STALE_IDENTITY"
        action["last_error"] = {
            "type": "PID_REUSED",
            "message": "The blocker PID now identifies a different backend or transaction.",
        }
        action["updated_at"] = iso_utc(now)
        incident["state"] = "REVIEW_REQUIRED"
        incident["last_error"] = action["last_error"]
        return _checkpoint(
            store,
            incident,
            worker_id=worker_id,
            timestamp=now,
            event_type="RECONCILIATION_STALE_IDENTITY",
            payload=action["last_error"],
            action_id=action["action_id"],
        ), False

    if not exact_rows:
        action["state"] = "RECONCILED_RESOLVED"
        action["last_error"] = None
        action["updated_at"] = iso_utc(now)
        incident = _checkpoint(
            store,
            incident,
            worker_id=worker_id,
            timestamp=now,
            event_type="INFLIGHT_RELATIONSHIP_GONE",
            payload={
                "causality_confirmed": False,
                "message": (
                    "The approved blocker identity is no longer present; "
                    "the workflow does not claim which actor resolved it."
                ),
            },
            action_id=action["action_id"],
        )
        return incident, True

    action["state"] = "IN_DOUBT"
    action["last_error"] = {
        "type": "AMBIGUOUS_PRIOR_EXECUTION",
        "message": (
            "The exact blocker still exists after an interrupted execution; "
            "automatic retry is disabled."
        ),
    }
    action["updated_at"] = iso_utc(now)
    incident["state"] = "REVIEW_REQUIRED"
    incident["last_error"] = action["last_error"]
    return _checkpoint(
        store,
        incident,
        worker_id=worker_id,
        timestamp=now,
        event_type="INFLIGHT_REQUIRES_REVIEW",
        payload=action["last_error"],
        action_id=action["action_id"],
    ), False


def _run_lock_incident_once(
    incident_id: str,
    *,
    store: SQLiteIncidentStore | None = None,
    observe_locks: Callable[[], dict] = get_lock_graph_snapshot,
    execute_action: Callable[..., dict] = execute_action_proposal,
    approval_decider: Callable[[dict], object] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> dict:
    incident_store = store or SQLiteIncidentStore()
    incident = incident_store.load_incident(
        incident_id
    )

    if incident["state"] in TERMINAL_STATES:
        return incident

    decision = None
    prompted_revision = None
    prompted_actions_digest = None
    if incident["state"] in {
        "AWAITING_APPROVAL",
        "AWAITING_REAPPROVAL",
    }:
        if approval_decider is None:
            return incident
        prompted_revision = incident["plan_revision"]
        prompted_actions_digest = _sha256(
            canonical_approved_actions(incident["actions"])
        )
        decision = approval_decider(
            deepcopy(incident)
        )

    worker_id = str(uuid.uuid4())
    current = now()
    incident = incident_store.acquire_lease(
        incident_id,
        owner=worker_id,
        now=current,
        lease_seconds=incident["policy"]["lease_seconds"],
    )

    try:
        if incident["state"] in {
            "AWAITING_APPROVAL",
            "AWAITING_REAPPROVAL",
        }:
            if decision is None:
                return incident
            current_digest = _sha256(
                canonical_approved_actions(incident["actions"])
            )
            if (
                incident["plan_revision"] != prompted_revision
                or current_digest != prompted_actions_digest
            ):
                raise IncidentWorkflowError(
                    "The plan changed while approval was being collected."
                )
            approved, actor = _decision_value(decision)
            current = now()
            if not approved:
                incident["state"] = "CANCELLED"
                incident["terminal_reason"] = "USER_REJECTED_BATCH"
                incident = _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=current,
                    event_type="APPROVAL_REJECTED",
                    payload={"actor": actor},
                )
                return incident

            incident["approval"] = build_approval(
                incident,
                actor=actor,
                now=current,
            )
            incident["state"] = "RUNNING"
            incident = _checkpoint(
                incident_store,
                incident,
                worker_id=worker_id,
                timestamp=current,
                event_type="PLAN_APPROVED",
                payload={
                    "approval_id": incident["approval"]["approval_id"],
                    "scope_digest": incident["approval"]["scope_digest"],
                    "action_count": len(incident["actions"]),
                },
            )

        if incident["state"] != "RUNNING":
            return incident

        current = now()
        incident = incident_store.renew_lease(
            incident_id,
            owner=worker_id,
            now=current,
            lease_seconds=incident["policy"]["lease_seconds"],
        )
        initial_snapshot = observe_locks()
        if initial_snapshot.get("truncated") is not False:
            incident["state"] = "REVIEW_REQUIRED"
            incident["last_error"] = {
                "type": "TRUNCATED_LOCK_SNAPSHOT",
                "message": "A complete lock graph is required before execution.",
            }
            return _checkpoint(
                incident_store,
                incident,
                worker_id=worker_id,
                timestamp=now(),
                event_type="EXECUTION_BLOCKED_TRUNCATED_SNAPSHOT",
                payload=incident["last_error"],
            )

        incident, can_continue = _reconcile_inflight(
            incident,
            snapshot=initial_snapshot,
            store=incident_store,
            worker_id=worker_id,
            now=now(),
        )
        if not can_continue:
            return incident

        for action_index in range(len(incident["actions"])):
            action = incident["actions"][action_index]
            if action["state"] in FINISHED_ACTION_STATES:
                continue
            if action["state"] != "PLANNED":
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = {
                    "type": "UNEXPECTED_ACTION_STATE",
                    "action_id": action["action_id"],
                    "state": action["state"],
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="UNEXPECTED_ACTION_STATE",
                    payload=incident["last_error"],
                    action_id=action["action_id"],
                )

            current = now()
            if _workflow_deadline_passed(incident, current):
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = {
                    "type": "WORKFLOW_DEADLINE_EXPIRED",
                    "message": "The incident execution deadline expired.",
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=current,
                    event_type="WORKFLOW_DEADLINE_EXPIRED",
                    payload=incident["last_error"],
                )

            incident = incident_store.renew_lease(
                incident_id,
                owner=worker_id,
                now=current,
                lease_seconds=incident["policy"]["lease_seconds"],
            )
            action = incident["actions"][action_index]
            snapshot = observe_locks()
            if snapshot.get("truncated") is not False:
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = {
                    "type": "TRUNCATED_LOCK_SNAPSHOT",
                    "message": "The lock graph became incomplete before an action.",
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="STEP_BLOCKED_TRUNCATED_SNAPSHOT",
                    payload=incident["last_error"],
                    action_id=action["action_id"],
                )
            rows = _snapshot_rows(snapshot)

            if _pid_reused(rows, action):
                action["state"] = "STALE_IDENTITY"
                action["last_error"] = {
                    "type": "PID_REUSED",
                    "message": "The approved PID now has a different identity.",
                }
                action["updated_at"] = iso_utc(now())
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = action["last_error"]
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="STEP_STALE_IDENTITY",
                    payload=action["last_error"],
                    action_id=action["action_id"],
                )

            exact_rows = _target_rows(rows, action)
            policy_rows = actionable_relationships(snapshot)
            policy_exact_rows = _target_rows(
                policy_rows,
                action,
            )
            approved_rows = _approved_target_rows(
                policy_rows,
                action,
            )
            if not approved_rows:
                if exact_rows:
                    action["state"] = "REAPPROVAL_REQUIRED"
                    if policy_exact_rows:
                        action["last_error"] = {
                            "type": "ONLY_NEW_WAITERS_REMAIN",
                            "message": (
                                "The blocker now affects only waiter sessions "
                                "outside the approved waiter-identity set."
                            ),
                        }
                    else:
                        action["last_error"] = {
                            "type": "TARGET_OUTSIDE_TERMINATION_POLICY",
                            "message": (
                                "The exact blocker remains visible but no "
                                "current relationship satisfies the lock "
                                "termination policy."
                            ),
                        }
                    action["updated_at"] = iso_utc(now())
                    incident["state"] = "REVIEW_REQUIRED"
                    incident["last_error"] = action["last_error"]
                    return _checkpoint(
                        incident_store,
                        incident,
                        worker_id=worker_id,
                        timestamp=now(),
                        event_type="STEP_REAPPROVAL_REQUIRED",
                        payload=action["last_error"],
                        action_id=action["action_id"],
                    )

                action["state"] = "SKIPPED_RESOLVED"
                action["updated_at"] = iso_utc(now())
                incident = _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="STEP_ALREADY_RESOLVED",
                    payload={
                        "target_digest": action["target_digest"]
                    },
                    action_id=action["action_id"],
                )
                continue

            representative = min(
                row["blocked_pid"]
                for row in approved_rows
            )
            action["proposal"]["blocked_pid"] = representative

            current = now()
            if _workflow_deadline_passed(incident, current):
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = {
                    "type": "WORKFLOW_DEADLINE_EXPIRED",
                    "message": (
                        "The incident deadline expired while the fresh lock "
                        "snapshot was being collected."
                    ),
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=current,
                    event_type="WORKFLOW_DEADLINE_EXPIRED_AFTER_OBSERVATION",
                    payload=incident["last_error"],
                    action_id=action["action_id"],
                )

            approval_check = _approval_is_current(
                incident,
                action,
                now=current,
            )
            if not approval_check["valid"]:
                incident["state"] = "AWAITING_REAPPROVAL"
                incident["last_error"] = {
                    "type": "APPROVAL_INVALID_OR_EXPIRED",
                    "errors": approval_check["errors"],
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="APPROVAL_EXPIRED_OR_INVALID",
                    payload=incident["last_error"],
                    action_id=action["action_id"],
                )

            # Observation and policy evaluation can consume most of a lease.
            # Renew immediately before the durable execution intent, then use
            # a newly sampled time for the final approval/deadline gate.
            incident = incident_store.renew_lease(
                incident_id,
                owner=worker_id,
                now=current,
                lease_seconds=incident["policy"]["lease_seconds"],
            )
            action = incident["actions"][action_index]
            action["proposal"]["blocked_pid"] = representative
            execution_current = now()
            if _workflow_deadline_passed(
                incident,
                execution_current,
            ):
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = {
                    "type": "WORKFLOW_DEADLINE_EXPIRED",
                    "message": (
                        "The incident deadline expired before the execution "
                        "intent could be committed."
                    ),
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=execution_current,
                    event_type="WORKFLOW_DEADLINE_EXPIRED_BEFORE_INTENT",
                    payload=incident["last_error"],
                    action_id=action["action_id"],
                )

            approval_check = _approval_is_current(
                incident,
                action,
                now=execution_current,
            )
            if not approval_check["valid"]:
                incident["state"] = "AWAITING_REAPPROVAL"
                incident["last_error"] = {
                    "type": "APPROVAL_INVALID_OR_EXPIRED",
                    "errors": approval_check["errors"],
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=execution_current,
                    event_type="APPROVAL_EXPIRED_BEFORE_INTENT",
                    payload=incident["last_error"],
                    action_id=action["action_id"],
                )

            action["state"] = "EXECUTING"
            action["attempt_count"] += 1
            action["operation_id"] = action["action_id"]
            action["updated_at"] = iso_utc(execution_current)
            incident = _checkpoint(
                incident_store,
                incident,
                worker_id=worker_id,
                timestamp=execution_current,
                event_type="STEP_EXECUTION_INTENT",
                payload={
                    "operation_id": action["operation_id"],
                    "target_digest": action["target_digest"],
                },
                action_id=action["action_id"],
            )
            action = incident["actions"][action_index]
            action_id = action["action_id"]
            execution_context = _execution_context_for_action(
                store=incident_store,
                incident=incident,
                action=action,
                worker_id=worker_id,
            )

            try:
                result = execute_action(
                    deepcopy(action["proposal"]),
                    operation_id=action["operation_id"],
                    approval_context=_approval_context_for_action(
                        incident,
                        action,
                    ),
                    execution_context=execution_context,
                )
            except Exception as exc:
                incident = incident_store.load_incident(
                    incident_id
                )
                action = _action_by_id(
                    incident,
                    action_id,
                )
                if action["state"] not in {
                    "EXECUTING",
                    "APPLYING",
                }:
                    raise IncidentWorkflowError(
                        "Executor raised after moving the action to an "
                        f"unexpected state: {action['state']}"
                    ) from exc
                action["state"] = "IN_DOUBT"
                action["last_error"] = {
                    "type": type(exc).__name__,
                    "message": (
                        "Executor raised after a durable action intent; "
                        "the outcome requires reconciliation."
                    ),
                }
                action["updated_at"] = iso_utc(now())
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = action["last_error"]
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="STEP_EXECUTION_IN_DOUBT",
                    payload=action["last_error"],
                    action_id=action["action_id"],
                )

            incident = incident_store.load_incident(
                incident_id
            )
            action = _action_by_id(
                incident,
                action_id,
            )
            if not isinstance(result, dict):
                if action["state"] not in {
                    "EXECUTING",
                    "APPLYING",
                }:
                    raise IncidentWorkflowError(
                        "Malformed executor output followed an unexpected "
                        f"action state: {action['state']}"
                    )
                action["state"] = "IN_DOUBT"
                action["last_error"] = {
                    "type": "MALFORMED_EXECUTOR_RESULT",
                    "message": (
                        "Executor returned a non-object after a durable "
                        "execution intent; automatic retry is disabled."
                    ),
                }
                action["updated_at"] = iso_utc(now())
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = action["last_error"]
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="MALFORMED_EXECUTOR_RESULT",
                    payload=action["last_error"],
                    action_id=action["action_id"],
                )

            if (
                result.get("status")
                == "BLOCKED_INCIDENT_APPROVAL"
                and action["state"] == "EXECUTING"
            ):
                # The executor's atomic claim is the last authorization gate.
                # A refusal before EXECUTING -> APPLYING proves that this
                # attempt performed no incident-authorized side effect, so it
                # is safe to return the action to the approval queue.
                action["state"] = "PLANNED"
                action["last_error"] = {
                    "type": "EXECUTION_AUTHORIZATION_BLOCKED",
                    "approval_error_type": result.get(
                        "approval_error_type"
                    ),
                    "message": (
                        "The persisted execution claim was refused before "
                        "any authorized side effect. Fresh approval is "
                        "required."
                    ),
                }
                action["updated_at"] = iso_utc(now())
                incident["state"] = "AWAITING_REAPPROVAL"
                incident["last_error"] = action["last_error"]
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="EXECUTION_AUTHORIZATION_BLOCKED",
                    payload=action["last_error"],
                    action_id=action["action_id"],
                )

            if action["state"] != "APPLYING":
                if action["state"] != "EXECUTING":
                    raise IncidentWorkflowError(
                        "Executor returned after moving the action to an "
                        f"unexpected state: {action['state']}"
                    )
                action["state"] = "IN_DOUBT"
                action["last_error"] = {
                    "type": "EXECUTION_CLAIM_MISSING",
                    "message": (
                        "Executor returned without atomically claiming the "
                        "persisted action; its outcome cannot be trusted."
                    ),
                }
                action["updated_at"] = iso_utc(now())
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = action["last_error"]
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="EXECUTION_CLAIM_MISSING",
                    payload=action["last_error"],
                    action_id=action["action_id"],
                )

            action["result"] = result
            action["state"] = "RESULT_RECORDED"
            action["updated_at"] = iso_utc(now())
            audit = result.get("audit")
            incident = _checkpoint(
                incident_store,
                incident,
                worker_id=worker_id,
                timestamp=now(),
                event_type="STEP_RESULT_RECORDED",
                payload={
                    "operation_id": action["operation_id"],
                    "status": result.get("status"),
                    "audit_status": (
                        audit.get("status")
                        if isinstance(audit, dict)
                        else None
                    ),
                },
                action_id=action["action_id"],
            )
            action = incident["actions"][action_index]

            success = _executor_result_confirmed(result)
            if not success:
                action["state"] = "FAILED"
                action["last_error"] = {
                    "type": "EXECUTOR_RESULT_NOT_CONFIRMED",
                    "status": result.get("status"),
                    "audit_status": (
                        audit.get("status")
                        if isinstance(audit, dict)
                        else None
                    ),
                }
                action["updated_at"] = iso_utc(now())
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = action["last_error"]
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="STEP_FAILED_STOPPING_BATCH",
                    payload=action["last_error"],
                    action_id=action["action_id"],
                )

            post_snapshot = observe_locks()
            if post_snapshot.get("truncated") is not False:
                action["state"] = "INCONCLUSIVE"
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = {
                    "type": "TRUNCATED_POST_ACTION_SNAPSHOT",
                    "message": "The post-action lock graph is incomplete.",
                }
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="POST_ACTION_VERIFICATION_INCONCLUSIVE",
                    payload=incident["last_error"],
                    action_id=action["action_id"],
                )
            if _target_rows(_snapshot_rows(post_snapshot), action):
                action["state"] = "INCONCLUSIVE"
                action["last_error"] = {
                    "type": "BLOCKER_IDENTITY_STILL_PRESENT",
                    "message": (
                        "The exact blocker still participates in the lock graph "
                        "after the executor reported success."
                    ),
                }
                action["updated_at"] = iso_utc(now())
                incident["state"] = "REVIEW_REQUIRED"
                incident["last_error"] = action["last_error"]
                return _checkpoint(
                    incident_store,
                    incident,
                    worker_id=worker_id,
                    timestamp=now(),
                    event_type="HOLISTIC_STEP_VERIFICATION_FAILED",
                    payload=action["last_error"],
                    action_id=action["action_id"],
                )

            action["state"] = "SUCCEEDED"
            action["last_error"] = None
            action["updated_at"] = iso_utc(now())
            incident = _checkpoint(
                incident_store,
                incident,
                worker_id=worker_id,
                timestamp=now(),
                event_type="STEP_SUCCEEDED",
                payload={
                    "operation_id": action["operation_id"],
                    "resolved_blocked_pids": action[
                        "allowed_blocked_pids"
                    ],
                },
                action_id=action["action_id"],
            )

        final_snapshot = observe_locks()
        if final_snapshot.get("truncated") is not False:
            incident["state"] = "REVIEW_REQUIRED"
            incident["last_error"] = {
                "type": "TRUNCATED_FINAL_SNAPSHOT",
                "message": "The incident cannot be completed without a full final lock graph.",
            }
            return _checkpoint(
                incident_store,
                incident,
                worker_id=worker_id,
                timestamp=now(),
                event_type="FINAL_VERIFICATION_INCONCLUSIVE",
                payload=incident["last_error"],
            )

        final_rows = _snapshot_rows(final_snapshot)
        remaining_approved = [
            {
                "action_id": action["action_id"],
                "relationships": _target_rows(final_rows, action),
            }
            for action in incident["actions"]
            if _target_rows(final_rows, action)
        ]
        if remaining_approved:
            incident["state"] = "REVIEW_REQUIRED"
            incident["last_error"] = {
                "type": "APPROVED_TARGETS_REMAIN",
                "action_ids": [
                    item["action_id"]
                    for item in remaining_approved
                ],
            }
            incident["final_summary"] = summarize_snapshot(final_snapshot)
            return _checkpoint(
                incident_store,
                incident,
                worker_id=worker_id,
                timestamp=now(),
                event_type="FINAL_APPROVED_TARGETS_REMAIN",
                payload=incident["last_error"],
            )

        approved_keys = {
            target_key(action["target"])
            for action in incident["actions"]
        }
        unapproved_remainder = [
            row
            for row in final_rows
            if target_key(_relationship_target(row)) not in approved_keys
        ]
        incident["final_summary"] = summarize_snapshot(final_snapshot)
        incident["last_error"] = None
        if unapproved_remainder:
            incident["state"] = "COMPLETED_WITH_UNAPPROVED_REMAINDER"
            incident["terminal_reason"] = (
                "APPROVED_SCOPE_RESOLVED_NEW_OR_UNAPPROVED_LOCKS_REMAIN"
            )
        else:
            incident["state"] = "COMPLETED"
            incident["terminal_reason"] = "APPROVED_SCOPE_RESOLVED"
        incident = _checkpoint(
            incident_store,
            incident,
            worker_id=worker_id,
            timestamp=now(),
            event_type="INCIDENT_COMPLETED",
            payload={
                "state": incident["state"],
                "remaining_relationship_count": len(unapproved_remainder),
            },
        )
        return incident

    finally:
        try:
            incident_store.release_lease(
                incident_id,
                owner=worker_id,
                now=now(),
            )
        except IncidentLeaseUnavailable:
            pass


def run_lock_incident(
    incident_id: str,
    *,
    store: SQLiteIncidentStore | None = None,
    observe_locks: Callable[[], dict] = get_lock_graph_snapshot,
    execute_action: Callable[..., dict] = execute_action_proposal,
    approval_decider: Callable[[dict], object] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> dict:
    """Run one workflow pass and return the latest durable view.

    Lease release is itself a durable versioned transition.  Reloading after
    the internal pass prevents callers from receiving the pre-release version
    or a stale lease owner.
    """
    incident_store = store or SQLiteIncidentStore()
    _run_lock_incident_once(
        incident_id,
        store=incident_store,
        observe_locks=observe_locks,
        execute_action=execute_action,
        approval_decider=approval_decider,
        now=now,
    )
    return incident_store.load_incident(
        incident_id
    )


def incident_public_view(
    incident: dict,
) -> dict:
    return {
        "incident_id": incident["incident_id"],
        "workflow_type": incident["workflow_type"],
        "state": incident["state"],
        "plan_revision": incident["plan_revision"],
        "version": incident["version"],
        "created_at": incident["created_at"],
        "updated_at": incident["updated_at"],
        "approval": (
            {
                "approval_id": incident["approval"]["approval_id"],
                "approved_at": incident["approval"]["approved_at"],
                "expires_at": incident["approval"]["expires_at"],
                "actor": incident["approval"]["actor"],
                "scope_digest": incident["approval"]["scope_digest"],
            }
            if incident.get("approval")
            else None
        ),
        "actions": [
            {
                "action_id": action["action_id"],
                "ordinal": action["ordinal"],
                "type": action["type"],
                "risk": action["risk"],
                "state": action["state"],
                "target": action["target"],
                "blocked_pids": action["allowed_blocked_pids"],
                "approved_waiters": action.get(
                    "approved_waiters",
                    [],
                ),
                "attempt_count": action["attempt_count"],
                "operation_id": action["operation_id"],
                "result_status": (
                    action["result"].get("status")
                    if isinstance(action.get("result"), dict)
                    else None
                ),
                "last_error": action.get("last_error"),
            }
            for action in incident["actions"]
        ],
        "final_summary": incident.get("final_summary"),
        "last_error": incident.get("last_error"),
        "terminal_reason": incident.get("terminal_reason"),
    }
