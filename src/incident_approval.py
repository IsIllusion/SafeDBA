from identifiers import is_valid_uuid as _valid_uuid
from serialization import json_digest
import hmac
import math

from datetime import (
    datetime,
    timezone,
)


APPROVAL_KIND = "LOCK_INCIDENT_APPROVAL"


def utc_now() -> datetime:
    return datetime.now(
        timezone.utc
    )


def parse_utc_timestamp(
    value: object,
) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return None

    return parsed.astimezone(
        timezone.utc
    )


def canonical_target(
    value: dict,
) -> dict:
    return {
        "database_name": value.get(
            "database_name"
        ),
        "blocker_pid": value.get(
            "blocker_pid"
        ),
        "blocker_backend_start": value.get(
            "blocker_backend_start"
        ),
        "blocker_xact_start": value.get(
            "blocker_xact_start"
        ),
    }


def target_key(
    value: dict,
) -> tuple:
    canonical = canonical_target(
        value
    )
    return (
        canonical["database_name"],
        canonical["blocker_pid"],
        canonical["blocker_backend_start"],
        canonical["blocker_xact_start"],
    )


def canonical_approved_actions(
    actions: list[dict],
) -> list[dict]:
    canonical = []

    for action in actions:
        blocked_pids = action.get(
            "allowed_blocked_pids",
            [],
        )
        normalized_pids = sorted({
            pid
            for pid in blocked_pids
            if (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
            )
        })
        approved_waiters = []
        for waiter in action.get("approved_waiters", []):
            if not isinstance(waiter, dict):
                continue
            approved_waiters.append({
                "blocked_pid": waiter.get("blocked_pid"),
                "blocked_backend_start": waiter.get(
                    "blocked_backend_start"
                ),
                "blocked_xact_start": waiter.get(
                    "blocked_xact_start"
                ),
            })
        approved_waiters = sorted(
            approved_waiters,
            key=lambda waiter: (
                waiter["blocked_pid"] or 0,
                waiter["blocked_backend_start"] or "",
                waiter["blocked_xact_start"] or "",
            ),
        )

        action_id = action.get("action_id")
        ordinal = action.get("ordinal")
        if not isinstance(action_id, str):
            action_id = ""
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
        ):
            ordinal = 0

        canonical.append({
            "action_id": action_id,
            "ordinal": ordinal,
            "target": canonical_target(
                action.get("target", {})
            ),
            "allowed_blocked_pids": (
                normalized_pids
            ),
            "approved_waiters": approved_waiters,
        })

    return sorted(
        canonical,
        key=lambda item: (
            item["ordinal"],
            item["action_id"],
        ),
    )


def compute_scope_digest(
    *,
    incident_id: str,
    plan_revision: int,
    approved_actions: list[dict],
    max_actions: int,
    max_risk: str,
) -> str:
    payload = {
        "incident_id": incident_id,
        "plan_revision": plan_revision,
        "approved_actions": (
            canonical_approved_actions(
                approved_actions
            )
        ),
        "max_actions": max_actions,
        "max_risk": max_risk,
    }
    return json_digest(payload)


def validate_approval_context(
    proposal: dict,
    context: object,
    *,
    now: datetime | None = None,
) -> dict:
    errors: list[str] = []

    if not isinstance(context, dict):
        return {
            "valid": False,
            "errors": [
                "Incident approval context must be an object."
            ],
        }

    if context.get("kind") != APPROVAL_KIND:
        errors.append(
            "Unsupported incident approval kind."
        )

    if context.get("revoked_at") is not None:
        errors.append(
            "Incident approval has been revoked."
        )

    incident_id = context.get(
        "incident_id"
    )
    approval_id = context.get(
        "approval_id"
    )
    if not _valid_uuid(incident_id):
        errors.append(
            "Incident approval has an invalid incident_id."
        )
    if not _valid_uuid(approval_id):
        errors.append(
            "Incident approval has an invalid approval_id."
        )

    plan_revision = context.get(
        "plan_revision"
    )
    max_actions = context.get(
        "max_actions"
    )
    if (
        isinstance(plan_revision, bool)
        or not isinstance(plan_revision, int)
        or plan_revision <= 0
    ):
        errors.append(
            "Incident approval plan_revision must be positive."
        )
    if (
        isinstance(max_actions, bool)
        or not isinstance(max_actions, int)
        or max_actions <= 0
        or max_actions > 100
    ):
        errors.append(
            "Incident approval max_actions is invalid."
        )

    approved_actions = context.get(
        "approved_actions"
    )
    if not isinstance(approved_actions, list) or not approved_actions:
        errors.append(
            "Incident approval has no approved actions."
        )
        approved_actions = []
    elif (
        isinstance(max_actions, int)
        and len(approved_actions) > max_actions
    ):
        errors.append(
            "Incident approval exceeds its action limit."
        )

    approved_at = parse_utc_timestamp(
        context.get("approved_at")
    )
    expires_at = parse_utc_timestamp(
        context.get("expires_at")
    )
    current = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime)
        and now.tzinfo is not None
        else utc_now()
    )
    if approved_at is None:
        errors.append(
            "Incident approval has an invalid approved_at."
        )
    elif approved_at > current:
        errors.append(
            "Incident approval is not active yet."
        )
    if expires_at is None:
        errors.append(
            "Incident approval has an invalid expires_at."
        )
    elif expires_at <= current:
        errors.append(
            "Incident approval has expired."
        )
    if (
        approved_at is not None
        and expires_at is not None
        and expires_at <= approved_at
    ):
        errors.append(
            "Incident approval expiry must follow approval time."
        )

    max_risk = context.get(
        "max_risk"
    )
    if max_risk != "HIGH":
        errors.append(
            "Incident approval does not authorize HIGH risk."
        )

    scope_digest = context.get(
        "scope_digest"
    )
    if not isinstance(scope_digest, str):
        errors.append(
            "Incident approval scope digest is missing."
        )
    elif (
        isinstance(incident_id, str)
        and isinstance(plan_revision, int)
        and isinstance(max_actions, int)
    ):
        try:
            expected_digest = compute_scope_digest(
                incident_id=incident_id,
                plan_revision=plan_revision,
                approved_actions=(
                    approved_actions
                ),
                max_actions=max_actions,
                max_risk=max_risk,
            )
        except (TypeError, ValueError):
            errors.append(
                "Incident approval scope is not canonicalizable."
            )
        else:
            if not hmac.compare_digest(
                scope_digest,
                expected_digest,
            ):
                errors.append(
                    "Incident approval scope digest does not match."
                )

    proposal_target = canonical_target(
        proposal
    )
    blocked_pid = proposal.get(
        "blocked_pid"
    )
    matched_action = None

    for action in approved_actions:
        if not isinstance(action, dict):
            continue
        if target_key(
            action.get("target", {})
        ) != target_key(proposal_target):
            continue
        if blocked_pid not in action.get(
            "allowed_blocked_pids",
            [],
        ):
            continue
        matched_action = action
        break

    if matched_action is None:
        errors.append(
            "Proposal is outside the approved incident scope."
        )
    elif context.get("current_action_id") != matched_action.get(
        "action_id"
    ):
        errors.append(
            "Incident approval current_action_id does not match the proposal."
        )

    confidence = proposal.get(
        "confidence"
    )
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
    ):
        errors.append(
            "Proposal confidence is invalid."
        )

    return {
        "valid": not errors,
        "errors": errors,
        "incident_id": incident_id,
        "approval_id": approval_id,
        "action_id": (
            matched_action.get("action_id")
            if matched_action
            else None
        ),
    }
