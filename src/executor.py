#This part is separated from Agent

import uuid

from datetime import (
    datetime,
    timezone,
)

from actions import (
    validate_action_proposal,
)

from audit import (
    write_audit_log,
)

from config import (
    MIN_IMPROVEMENT_PCT,
    MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO,
)

from db_tools import (
    analyze_table,
    benchmark_query,
    compare_query_results,
    create_index,
    drop_index,
    get_column_stats,
    get_lock_waits,
    get_query_plan,
    terminate_blocking_backend,
)

from diagnostics import (
    analyze_query_plan,
)

from safety import (
    assess_risk,
    is_operation_allowed,
    requires_approval,
)


def now_utc() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def configured_incident_store():
    """Build the only store trusted to authorize production execution."""
    # Lazy imports keep the non-incident executor path dependency-light and
    # make the store factory replaceable by deterministic tests.
    from config import INCIDENT_STATE_DB_PATH
    from workflow_store import SQLiteIncidentStore

    return SQLiteIncidentStore(
        INCIDENT_STATE_DB_PATH
    )


incident_store_factory = configured_incident_store

def write_action_audit(
    result: dict,
    proposal: object,
) -> bool:

    record = {
        "operation_id": (
            result.get(
                "operation_id"
            )
        ),

        "timestamp": now_utc(),

        "action_type": (
            proposal.get(
                "type"
            )
            if isinstance(proposal, dict)
            else None
        ),

        "status": (
            result.get(
                "status"
            )
        ),

        "decision": (
            result.get(
                "decision"
            )
        ),

        "risk": (
            result.get(
                "risk"
            )
        ),

        "approved": (
            result.get(
                "approved"
            )
        ),

        "proposal": proposal,
    }

    # Preserve all action-specific
    # deterministic evidence.
    for key, value in result.items():

        if key not in record:
            record[key] = value

    try:
        write_audit_log(
            record
        )
    except Exception as exc:
        result["audit"] = {
            "status": "WRITE_FAILED",
            "error_type": type(exc).__name__,
        }
        if result.get("status") == "SUCCESS":
            result["status"] = "SUCCESS_AUDIT_FAILED"
            result["decision"] = "REVIEW"
        return False

    result["audit"] = {
        "status": "WRITTEN",
    }
    return True


def find_table_scan(
    analysis: dict,
    table: str,
) -> dict | None:

    for scan in analysis.get(
        "scan_nodes",
        [],
    ):
        if scan.get("table") == table:
            return scan

    return None


def capture_query_state(
    query: str,
    table: str,
) -> dict:

    plan = get_query_plan(
        query
    )

    analysis = analyze_query_plan(
        plan
    )

    scan = find_table_scan(
        analysis,
        table,
    )

    return {
        "execution_time_ms": (
            analysis.get(
                "execution_time_ms"
            )
        ),
        "estimated_output_rows": (
            analysis.get(
                "estimated_output_rows"
            )
        ),
        "actual_output_rows": (
            analysis.get(
                "actual_output_rows"
            )
        ),
        "cardinality_error_ratio": (
            analysis.get(
                "cardinality_error_ratio"
            )
        ),
        "scan": scan,
        "scan_nodes": analysis.get(
            "scan_nodes",
            [],
        ),
    }

def capture_query_analysis(
    query: str,
) -> dict:

    plan = get_query_plan(
        query
    )

    return analyze_query_plan(
        plan
    )


def get_scan_types(
    analysis: dict,
) -> list[str]:

    return [
        scan.get("scan_type")
        for scan in analysis.get(
            "scan_nodes",
            [],
        )
        if scan.get("scan_type")
    ]

def capture_statistics_state(
    table: str,
    columns: list[str],
) -> dict:

    result = {}

    for column in columns:

        result[column] = (
            get_column_stats(
                table_name=table,
                column_name=column,
            )
        )

    return result


def execute_query_rewrite_proposal(
    proposal: dict,
    *,
    operation_id: str | None = None,
) -> dict:

    operation_id = operation_id or str(uuid.uuid4())

    # ----------------------------------
    # 1. Validate proposal structure
    # ----------------------------------

    validation = (
        validate_action_proposal(
            proposal
        )
    )

    if not validation["valid"]:

        result = {
            "operation_id": operation_id,
            "status": (
                "BLOCKED_VALIDATION"
            ),
            "decision": "BLOCK",
            "errors": validation[
                "errors"
            ],
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------
    # 2. Deterministic risk assessment
    # ----------------------------------

    risk = assess_risk(
        "REWRITE_QUERY"
    )

    if not is_operation_allowed(
        risk
    ):

        result = {
            "operation_id": operation_id,
            "status": "BLOCKED",
            "decision": "BLOCK",
            "risk": risk,
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    original_query = proposal[
        "original_query"
    ]

    rewritten_query = proposal[
        "rewritten_query"
    ]

    # ----------------------------------
    # 3. Current-snapshot result comparison
    # ----------------------------------

    print()
    print(
        "Comparing current snapshot results..."
    )

    snapshot_result = (
        compare_query_results(
            original_query,
            rewritten_query,
        )
    )

    print(
        "Current row multiset matches:",
        snapshot_result[
            "equivalent"
        ],
    )

    if not snapshot_result[
        "equivalent"
    ]:

        result = {
            "operation_id": operation_id,
            "status": (
                "BLOCKED_SNAPSHOT_MISMATCH"
            ),
            "decision": "BLOCK",
            "risk": risk,
            "snapshot_result_comparison": (
                snapshot_result
            ),
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------
    # 4. Benchmark original query
    # ----------------------------------

    print()
    print(
        "Running ORIGINAL benchmark..."
    )

    before = benchmark_query(
        original_query
    )

    print(
        f"Original median: "
        f"{before['median_ms']:.3f} ms"
    )

    before_analysis = (
        capture_query_analysis(
            original_query
        )
    )

    # ----------------------------------
    # 5. Benchmark rewritten query
    # ----------------------------------

    print()
    print(
        "Running REWRITE benchmark..."
    )

    after = benchmark_query(
        rewritten_query
    )

    print(
        f"Rewrite median: "
        f"{after['median_ms']:.3f} ms"
    )

    after_analysis = (
        capture_query_analysis(
            rewritten_query
        )
    )

    before_ms = before[
        "median_ms"
    ]

    after_ms = after[
        "median_ms"
    ]

    if before_ms > 0:
        improvement = (
            (
                before_ms
                - after_ms
            )
            / before_ms
            * 100.0
        )
    else:
        improvement = 0.0

    before_scan_types = (
        get_scan_types(
            before_analysis
        )
    )

    after_scan_types = (
        get_scan_types(
            after_analysis
        )
    )

    plan_changed = (
        before_scan_types
        != after_scan_types
    )

    # ----------------------------------
    # 6. Accept or reject candidate
    # ----------------------------------

    accepted = (
        improvement
        >= MIN_IMPROVEMENT_PCT
    )

    if accepted:
        status = (
            "REWRITE_ACCEPTED"
        )
        decision = (
            "ACCEPT_REWRITE"
        )

    else:
        status = (
            "REWRITE_REJECTED"
        )
        decision = (
            "REJECT_REWRITE"
        )

    result = {
        "operation_id": operation_id,
        "status": status,
        "decision": decision,
        "risk": risk,

        "snapshot_result_comparison": (
            snapshot_result
        ),

        "before_ms": before_ms,
        "after_ms": after_ms,
        "improvement_pct": (
            improvement
        ),

        "before_scan_types": (
            before_scan_types
        ),

        "after_scan_types": (
            after_scan_types
        ),

        "plan_changed": (
            plan_changed
        ),

        "before_analysis": (
            before_analysis
        ),

        "after_analysis": (
            after_analysis
        ),
    }

    write_action_audit(
        result,
        proposal,
    )

    return result


def execute_analyze_table_proposal(
    proposal: dict,
    *,
    operation_id: str | None = None,
) -> dict:

    operation_id = operation_id or str(uuid.uuid4())

    # ----------------------------------
    # 1. Validate proposal
    # ----------------------------------

    validation = (
        validate_action_proposal(
            proposal
        )
    )

    if not validation["valid"]:

        result = {
            "operation_id": operation_id,
            "status": (
                "BLOCKED_VALIDATION"
            ),
            "decision": "BLOCK",
            "errors": validation[
                "errors"
            ],
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------
    # 2. Risk assessment
    # ----------------------------------

    risk = assess_risk(
        "ANALYZE_TABLE"
    )

    if not is_operation_allowed(
        risk
    ):

        result = {
            "operation_id": operation_id,
            "status": "BLOCKED",
            "decision": "BLOCK",
            "risk": risk,
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    query = proposal[
        "query"
    ]

    table = proposal[
        "table"
    ]

    columns = proposal.get(
        "columns",
        [],
    )

    # ----------------------------------
    # 3. Human approval
    # ----------------------------------

    if requires_approval(
        risk
    ):

        print()
        print(
            "=== Proposed Database Action ==="
        )

        print(
            "Action: ANALYZE_TABLE"
        )

        print(
            f"Table: {table}"
        )

        print(
            f"Columns: {columns}"
        )

        print(
            f"Risk: {risk}"
        )

        print(
            "Agent confidence: "
            f"{proposal.get('confidence', 0):.2f}"
        )

        print(
            f"Reason: "
            f"{proposal.get('reason', '')}"
        )

        answer = input(
            "Approve operation? [y/N]: "
        )

        approved = (
            answer.strip().lower()
            == "y"
        )

        if not approved:

            result = {
                "operation_id": (
                    operation_id
                ),
                "status": "CANCELLED",
                "decision": "CANCEL",
                "risk": risk,
                "approved": False,
            }

            write_action_audit(
                result,
                proposal,
            )

            return result

    # ----------------------------------
    # 4. Capture BEFORE evidence
    # ----------------------------------

    print()
    print(
        "Capturing BEFORE "
        "cardinality evidence..."
    )

    before_analysis = (
        capture_query_analysis(
            query
        )
    )

    before_error = (
        before_analysis.get(
            "cardinality_error_ratio"
        )
    )

    before_stats = (
        capture_statistics_state(
            table,
            columns,
        )
    )

    print(
        "Before cardinality "
        f"error ratio: {before_error}"
    )

    # ----------------------------------
    # 5. Execute ANALYZE
    # ----------------------------------

    print()
    print(
        "Running ANALYZE..."
    )

    try:

        analyze_table(
            table_name=table,
            columns=columns,
        )

    except Exception as exc:

        result = {
            "operation_id": (
                operation_id
            ),
            "status": "FAILED",
            "decision": "FAILED",
            "risk": risk,
            "error": str(exc),
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------
    # 6. Capture AFTER evidence
    # ----------------------------------

    print()
    print(
        "Capturing AFTER "
        "cardinality evidence..."
    )

    after_analysis = (
        capture_query_analysis(
            query
        )
    )

    after_error = (
        after_analysis.get(
            "cardinality_error_ratio"
        )
    )

    after_stats = (
        capture_statistics_state(
            table,
            columns,
        )
    )

    print(
        "After cardinality "
        f"error ratio: {after_error}"
    )

    # ----------------------------------
    # 7. Deterministic acceptance
    # ----------------------------------

    improved = (
        before_error is not None
        and after_error is not None
        and after_error < before_error
    )

    acceptable = (
        after_error is not None
        and after_error
        <= (
            MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO
        )
    )

    confirmed = (
        improved
        and acceptable
    )

    if confirmed:

        status = (
            "STATISTICS_REFRESH_CONFIRMED"
        )

        decision = (
            "ACCEPT_ANALYZE"
        )

    else:

        status = (
            "STATISTICS_REFRESH_INCONCLUSIVE"
        )

        decision = (
            "REVIEW"
        )

    result = {
        "operation_id": operation_id,
        "status": status,
        "decision": decision,
        "risk": risk,

        "before_cardinality_error_ratio": (
            before_error
        ),

        "after_cardinality_error_ratio": (
            after_error
        ),

        "acceptance_threshold": (
            MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO
        ),

        "before_analysis": (
            before_analysis
        ),

        "after_analysis": (
            after_analysis
        ),

        "before_stats": (
            before_stats
        ),

        "after_stats": (
            after_stats
        ),
    }

    write_action_audit(
        result,
        proposal,
    )

    return result


def execute_terminate_backend_proposal(
    proposal: dict,
    *,
    operation_id: str | None = None,
    approval_context: dict | None = None,
    execution_context: dict | None = None,
) -> dict:

    operation_id = operation_id or str(uuid.uuid4())

    # ----------------------------------
    # 1. Proposal validation
    # ----------------------------------

    validation = (
        validate_action_proposal(
            proposal
        )
    )

    if not validation["valid"]:

        result = {
            "operation_id": operation_id,

            "status": (
                "BLOCKED_VALIDATION"
            ),

            "decision": "BLOCK",

            "errors": validation[
                "errors"
            ],
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------
    # 2. Risk assessment
    # ----------------------------------

    risk = assess_risk(
        "TERMINATE_BACKEND"
    )

    if not is_operation_allowed(
        risk
    ):

        result = {
            "operation_id": operation_id,

            "status": "BLOCKED",

            "decision": "BLOCK",

            "risk": risk,
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    blocked_pid = proposal[
        "blocked_pid"
    ]

    blocker_pid = proposal[
        "blocker_pid"
    ]

    before_evidence = (
        validation.get(
            "current_blocking_evidence"
        )
    )

    blocked_backend_start = (
        before_evidence.get("blocked_backend_start")
        if isinstance(before_evidence, dict)
        else None
    )
    blocked_xact_start = (
        before_evidence.get("blocked_xact_start")
        if isinstance(before_evidence, dict)
        else None
    )
    if (
        not isinstance(blocked_backend_start, str)
        or not blocked_backend_start.strip()
        or not isinstance(blocked_xact_start, str)
        or not blocked_xact_start.strip()
    ):
        result = {
            "operation_id": operation_id,
            "status": "BLOCKED_VALIDATION",
            "decision": "BLOCK",
            "errors": [
                "Current blocked backend identity is incomplete."
            ],
            "before_evidence": before_evidence,
        }
        write_action_audit(
            result,
            proposal,
        )
        return result

    # ----------------------------------
    # 3. Human approval
    # ----------------------------------

    approval_source = None
    incident_execution = (
        approval_context is not None
        or execution_context is not None
    )
    if incident_execution:
        if (
            not isinstance(approval_context, dict)
            or not isinstance(execution_context, dict)
        ):
            result = {
                "operation_id": operation_id,
                "status": "BLOCKED_INCIDENT_APPROVAL",
                "decision": "BLOCK",
                "risk": risk,
                "approved": False,
                "errors": [
                    "Persisted incident approval and execution context "
                    "are both required."
                ],
                "before_evidence": before_evidence,
            }
            write_action_audit(
                result,
                proposal,
            )
            return result

        try:
            store = incident_store_factory()
            authorization = store.claim_action_execution(
                approval_context=approval_context,
                operation_id=operation_id,
                proposal=proposal,
                current_evidence=before_evidence,
                execution_context=execution_context,
            )
        except Exception as exc:
            result = {
                "operation_id": operation_id,
                "status": "BLOCKED_INCIDENT_APPROVAL",
                "decision": "BLOCK",
                "risk": risk,
                "approved": False,
                "errors": [
                    "The configured incident store did not authorize "
                    "this action."
                ],
                "approval_error_type": type(exc).__name__,
                "before_evidence": before_evidence,
            }
            write_action_audit(
                result,
                proposal,
            )
            return result

        approval_source = {
            "mode": "PERSISTED_INCIDENT_CLAIM",
            "incident_id": authorization["incident_id"],
            "approval_id": authorization["approval_id"],
            "action_id": authorization["action_id"],
            "operation_id": authorization["operation_id"],
            "plan_revision": authorization["plan_revision"],
            "scope_digest": authorization["scope_digest"],
            "evidence_digest": authorization["evidence_digest"],
            "claimed_at": authorization["claimed_at"],
        }

        print()
        print(
            "Using the persisted, exact-scope "
            "IncidentWorkflow approval."
        )

    if (
        requires_approval(risk)
        and approval_source is None
    ):

        print()
        print(
            "=== Proposed Database Action ==="
        )

        print(
            "Action: TERMINATE_BACKEND"
        )

        print(
            f"Blocked PID: "
            f"{blocked_pid}"
        )

        print(
            f"Blocker PID: "
            f"{blocker_pid}"
        )

        if before_evidence:

            blocker_state = (
                before_evidence.get(
                    "blocker_state"
                )
            )

            blocked_query = (
                before_evidence.get(
                    "blocked_query"
                )
            )

            blocker_query = (
                before_evidence.get(
                    "blocker_query"
                )
            )

            print(
                f"Blocker state: "
                f"{blocker_state}"
            )

            print(
                f"Blocked query: "
                f"{blocked_query}"
            )

            print(
                f"Blocker query: "
                f"{blocker_query}"
            )

        print(
            f"Risk: {risk}"
        )

        confidence = proposal.get(
            "confidence",
            0,
        )

        print(
            "Agent confidence: "
            f"{confidence:.2f}"
        )

        reason = proposal.get(
            "reason",
            "",
        )

        print(
            f"Reason: {reason}"
        )

        print()
        print(
            "WARNING: this action "
            "terminates the target "
            "PostgreSQL session."
        )

        answer = input(
            "Approve operation? [y/N]: "
        )

        approved = (
            answer.strip().lower()
            == "y"
        )

        if not approved:

            result = {
                "operation_id": (
                    operation_id
                ),

                "status": "CANCELLED",

                "decision": "CANCEL",

                "risk": risk,

                "approved": False,

                "before_evidence": (
                    before_evidence
                ),
            }

            write_action_audit(
                result,
                proposal,
            )

            return result

        approval_source = {
            "mode": "INTERACTIVE_SINGLE_ACTION",
        }

    # ----------------------------------
    # 4. FINAL revalidation + execute
    # ----------------------------------

    print()
    print(
        "Running final blocking "
        "relationship validation..."
    )

    try:

        execution_evidence = (
            terminate_blocking_backend(
                blocked_pid=blocked_pid,
                blocker_pid=blocker_pid,
                blocker_backend_start=proposal[
                    "blocker_backend_start"
                ],
                blocker_xact_start=proposal[
                    "blocker_xact_start"
                ],
                timeout_ms=5000,
                blocked_backend_start=(
                    blocked_backend_start
                ),
                blocked_xact_start=(
                    blocked_xact_start
                ),
            )
        )

    except Exception as exc:

        result = {
            "operation_id": (
                operation_id
            ),

            "status": "FAILED",

            "decision": "FAILED",

            "risk": risk,

            "approved": True,

            "approval_source": approval_source,

            "error": str(exc),

            "before_evidence": (
                before_evidence
            ),
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    if not execution_evidence.get(
        "final_validation_passed"
    ):

        result = {
            "operation_id": (
                operation_id
            ),

            "status": (
                "BLOCKED_FINAL_REVALIDATION"
            ),

            "decision": "BLOCK",

            "risk": risk,

            "approved": True,

            "approval_source": approval_source,

            "before_evidence": (
                before_evidence
            ),

            "final_execution_evidence": (
                execution_evidence
            ),
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    if not execution_evidence.get(
        "terminated"
    ):

        result = {
            "operation_id": (
                operation_id
            ),

            "status": (
                "TERMINATION_FAILED"
            ),

            "decision": "FAILED",

            "risk": risk,

            "approved": True,

            "approval_source": approval_source,

            "before_evidence": (
                before_evidence
            ),

            "final_execution_evidence": (
                execution_evidence
            ),
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------
    # 5. Post-action verification
    # ----------------------------------

    print()
    print(
        "Verifying blocking "
        "relationship removal..."
    )

    after_lock_waits = (
        get_lock_waits()
    )

    remaining_relationship = None

    for lock_wait in after_lock_waits:

        if (
            lock_wait.get(
                "blocked_pid"
            )
            == blocked_pid

            and lock_wait.get(
                "blocker_pid"
            )
            == blocker_pid
        ):

            remaining_relationship = (
                lock_wait
            )

            break

    resolved = (
        remaining_relationship
        is None
    )

    # ----------------------------------
    # 6. Final deterministic result
    # ----------------------------------

    if resolved:

        status = (
            "BACKEND_TERMINATION_CONFIRMED"
        )

        decision = (
            "RESOLVE_BLOCKING"
        )

    else:

        status = (
            "BACKEND_TERMINATION_INCONCLUSIVE"
        )

        decision = "REVIEW"

    result = {
        "operation_id": (
            operation_id
        ),

        "status": status,

        "decision": decision,

        "risk": risk,

        "approved": True,

        "approval_source": approval_source,

        "blocked_pid": (
            blocked_pid
        ),

        "blocker_pid": (
            blocker_pid
        ),

        "before_evidence": (
            before_evidence
        ),

        "final_execution_evidence": (
            execution_evidence
        ),

        "blocking_relationship_removed": (
            resolved
        ),

        "remaining_relationship": (
            remaining_relationship
        ),
    }

    write_action_audit(
        result,
        proposal,
    )

    return result



def execute_action_proposal(
    proposal: object,
    *,
    operation_id: str | None = None,
    approval_context: dict | None = None,
    execution_context: dict | None = None,
) -> dict:

    intent_id = operation_id or str(uuid.uuid4())
    try:
        uuid.UUID(intent_id)
    except (ValueError, AttributeError, TypeError):
        return {
            "operation_id": intent_id,
            "status": "BLOCKED_VALIDATION",
            "decision": "BLOCK",
            "errors": [
                "operation_id must be a valid UUID."
            ],
        }
    try:
        write_audit_log({
            "operation_id": intent_id,
            "timestamp": now_utc(),
            "action_type": (
                proposal.get("type")
                if isinstance(proposal, dict)
                else None
            ),
            "status": "INTENT_RECEIVED",
            "proposal": proposal,
            "execution_context": execution_context,
            "approval_context": (
                {
                    "kind": approval_context.get("kind"),
                    "incident_id": approval_context.get("incident_id"),
                    "approval_id": approval_context.get("approval_id"),
                    "plan_revision": approval_context.get("plan_revision"),
                    "scope_digest": approval_context.get("scope_digest"),
                    "current_action_id": approval_context.get(
                        "current_action_id"
                    ),
                }
                if isinstance(approval_context, dict)
                else None
            ),
        })
    except Exception as exc:
        return {
            "operation_id": intent_id,
            "status": "BLOCKED_AUDIT_UNAVAILABLE",
            "decision": "BLOCK",
            "error": (
                "The append-only audit sink was unavailable; "
                "no controlled action was attempted."
            ),
            "audit_error_type": type(exc).__name__,
        }

    try:
        return _execute_action_proposal_with_intent(
            proposal,
            intent_id=intent_id,
            approval_context=approval_context,
            execution_context=execution_context,
        )
    except Exception as exc:
        # The intent record proves an action may have begun.  Unexpected
        # failures therefore require review rather than a misleading clean
        # failure, and the outcome keeps the same correlation identifier.
        result = {
            "operation_id": intent_id,
            "status": "FAILED_UNHANDLED",
            "decision": "REVIEW",
            "error": (
                "Controlled action raised an unexpected exception; "
                "database state may be incomplete."
            ),
            "error_type": type(exc).__name__,
        }
        write_action_audit(result, proposal)
        return result


def _execute_action_proposal_with_intent(
    proposal: object,
    *,
    intent_id: str,
    approval_context: dict | None = None,
    execution_context: dict | None = None,
) -> dict:

    if not isinstance(proposal, dict):
        result = {
            "operation_id": intent_id,
            "status": "BLOCKED_VALIDATION",
            "decision": "BLOCK",
            "errors": [
                "Action proposal must be an object."
            ],
        }
        write_action_audit(result, proposal)
        return result

    action_type = proposal.get(
        "type"
    )

    if (
        (
            approval_context is not None
            or execution_context is not None
        )
        and action_type != "TERMINATE_BACKEND"
    ):
        result = {
            "operation_id": intent_id,
            "status": "BLOCKED_INCIDENT_APPROVAL",
            "decision": "BLOCK",
            "errors": [
                "IncidentWorkflow approval currently supports "
                "TERMINATE_BACKEND only."
            ],
        }
        write_action_audit(result, proposal)
        return result

    if action_type == "REWRITE_QUERY":
        return (
            execute_query_rewrite_proposal(
                proposal,
                operation_id=intent_id,
            )
        )

    if action_type == "ANALYZE_TABLE":
        return (
            execute_analyze_table_proposal(
                proposal,
                operation_id=intent_id,
            )
        )

    if action_type == "TERMINATE_BACKEND":
        return (
            execute_terminate_backend_proposal(
                proposal,
                operation_id=intent_id,
                approval_context=approval_context,
                execution_context=execution_context,
            )
        )

    operation_id = intent_id

    # ----------------------------------------
    # 1. Deterministic validation
    # ----------------------------------------

    validation = (
        validate_action_proposal(
            proposal
        )
    )

    if not validation["valid"]:

        result = {
            "operation_id": operation_id,
            "status": "BLOCKED_VALIDATION",
            "errors": validation[
                "errors"
            ],
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------------
    # 2. Risk engine
    # ----------------------------------------

    risk = assess_risk(
        proposal["type"]
    )

    if not is_operation_allowed(
        risk
    ):

        result = {
            "operation_id": operation_id,
            "status": "BLOCKED",
            "risk": risk,
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------------
    # 3. Human approval
    # ----------------------------------------

    approved = True

    if requires_approval(
        risk
    ):

        print()
        print(
            "=== Proposed Database Action ==="
        )

        print(
            f"Action: "
            f"{proposal['type']}"
        )

        print(
            f"Table: "
            f"{proposal['table']}"
        )

        print(
            f"Column: "
            f"{proposal['column']}"
        )

        print(
            f"Index: "
            f"{proposal['index_name']}"
        )

        print(
            f"Risk: {risk}"
        )

        print(
            f"Agent confidence: "
            f"{proposal['confidence']:.2f}"
        )

        print(
            f"Reason: "
            f"{proposal['reason']}"
        )

        answer = input(
            "Approve operation? [y/N]: "
        )

        approved = (
            answer.strip().lower()
            == "y"
        )

    if not approved:

        result = {
            "operation_id": operation_id,
            "status": "CANCELLED",
            "risk": risk,
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------------
    # 4. BEFORE benchmark
    # ----------------------------------------

    print()
    print(
        "Running BEFORE benchmark..."
    )

    before = benchmark_query(
        proposal["query"]
    )

    print(
        f"Before median: "
        f"{before['median_ms']:.3f} ms"
    )

    before_state = capture_query_state(
        query=proposal["query"],
        table=proposal["table"],
    )

    # ----------------------------------------
    # 5. Execute
    # ----------------------------------------

    try:

        created_identity = create_index(
            table=proposal["table"],
            column=proposal["column"],
            index_name=proposal[
                "index_name"
            ],
        )

    except Exception as exc:

        result = {
            "operation_id": operation_id,
            "status": "FAILED",
            "risk": risk,
            "approved": True,
            "error": str(exc),
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------------
    # 6. AFTER benchmark
    # ----------------------------------------

    print(
        "Running AFTER benchmark..."
    )

    try:
        after = benchmark_query(
            proposal["query"]
        )

        print(
            f"After median: "
            f"{after['median_ms']:.3f} ms"
        )

        after_state = capture_query_state(
            query=proposal["query"],
            table=proposal["table"],
        )

    except Exception as verification_exc:
        try:
            drop_index(
                proposal["index_name"],
                expected_index_oid=(
                    created_identity["index_oid"]
                ),
                expected_table_oid=(
                    created_identity["table_oid"]
                ),
            )
        except Exception as rollback_exc:
            result = {
                "operation_id": operation_id,
                "status": "ROLLBACK_FAILED",
                "decision": "REVIEW",
                "risk": risk,
                "approved": True,
                "verification_error": (
                    str(verification_exc)
                ),
                "rollback_error": (
                    str(rollback_exc)
                ),
                "created_index_identity": (
                    created_identity
                ),
                "before_state": before_state,
            }
            write_action_audit(
                result,
                proposal,
            )
            return result

        result = {
            "operation_id": operation_id,
            "status": (
                "ROLLED_BACK_AFTER_"
                "VERIFICATION_ERROR"
            ),
            "decision": "ROLLBACK",
            "risk": risk,
            "approved": True,
            "verification_error": (
                str(verification_exc)
            ),
            "created_index_identity": created_identity,
            "before_state": before_state,
        }
        write_action_audit(
            result,
            proposal,
        )
        return result

    before_ms = before[
        "median_ms"
    ]

    after_ms = after[
        "median_ms"
    ]

    before_scan = (
        before_state.get("scan")
        or {}
    )

    after_scan = (
        after_state.get("scan")
        or {}
    )

    before_scan_type = (
        before_scan.get(
            "scan_type"
        )
    )

    after_scan_type = (
        after_scan.get(
            "scan_type"
        )
    )

    plan_changed = (
        before_scan_type
        != after_scan_type
    )

    index_used = any(
        scan.get("index_name")
        == proposal["index_name"]
        for scan in after_state.get(
            "scan_nodes",
            [],
        )
    )


    if before_ms > 0:

        improvement = (
            (
                before_ms
                - after_ms
            )
            / before_ms
            * 100
        )

    else:
        improvement = 0.0



    # ----------------------------------------
    # 7. Rollback decision
    # ----------------------------------------

    if (
        improvement
        < MIN_IMPROVEMENT_PCT
        or not index_used
    ):

        try:

            drop_index(
                proposal[
                    "index_name"
                ],
                expected_index_oid=(
                    created_identity["index_oid"]
                ),
                expected_table_oid=(
                    created_identity["table_oid"]
                ),
            )

        except Exception as exc:

            result = {
                "operation_id": (
                    operation_id
                ),
                "status": (
                    "ROLLBACK_FAILED"
                ),
                "risk": risk,
                "approved": True,
                "before_ms": before_ms,
                "after_ms": after_ms,
                "improvement_pct": (
                    improvement
                ),
                "rollback_error": (
                    str(exc)
                ),
                "index_used": index_used,
                "created_index_identity": created_identity,
            }

            write_action_audit(
                result,
                proposal,
            )

            return result

        result = {
            "operation_id": operation_id,
            "status": "ROLLED_BACK",
            "decision": "ROLLBACK",
            "risk": risk,
            "approved": True,

            "before_ms": before_ms,
            "after_ms": after_ms,
            "improvement_pct": improvement,
            "index_used": index_used,
            "created_index_identity": created_identity,
            "rollback_reason": (
                "created_index_not_used"
                if not index_used
                else "insufficient_improvement"
            ),

            "before_scan_type": (
                before_scan_type
            ),
            "after_scan_type": (
                after_scan_type
            ),
            "plan_changed": (
                plan_changed
            ),

            "before_state": (
                before_state
            ),
            "after_state": (
                after_state
            ),
        }

        write_action_audit(
            result,
            proposal,
        )

        return result

    # ----------------------------------------
    # 8. Success
    # ----------------------------------------

    result = {
        "operation_id": operation_id,
        "status": "SUCCESS",
        "decision": "KEEP",
        "risk": risk,
        "approved": True,

        "before_ms": before_ms,
        "after_ms": after_ms,
        "improvement_pct": improvement,
        "index_used": index_used,
        "created_index_identity": created_identity,

        "before_scan_type": (
            before_scan_type
        ),
        "after_scan_type": (
            after_scan_type
        ),
        "plan_changed": (
            plan_changed
        ),

        "before_state": (
            before_state
        ),
        "after_state": (
            after_state
        ),
    }

    write_action_audit(
        result,
        proposal,
    )

    return result
