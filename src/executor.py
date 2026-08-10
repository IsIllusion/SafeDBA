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

def write_action_audit(
    result: dict,
    proposal: dict,
) -> None:

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

    write_audit_log(
        record
    )


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
) -> dict:

    operation_id = str(
        uuid.uuid4()
    )

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
    # 3. Semantic validation
    # ----------------------------------

    print()
    print(
        "Running semantic validation..."
    )

    semantic_result = (
        compare_query_results(
            original_query,
            rewritten_query,
        )
    )

    print(
        "Equivalent:",
        semantic_result[
            "equivalent"
        ],
    )

    if not semantic_result[
        "equivalent"
    ]:

        result = {
            "operation_id": operation_id,
            "status": (
                "BLOCKED_SEMANTICS"
            ),
            "decision": "BLOCK",
            "risk": risk,
            "semantic_validation": (
                semantic_result
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

        "semantic_validation": (
            semantic_result
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
) -> dict:

    operation_id = str(
        uuid.uuid4()
    )

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
) -> dict:

    operation_id = str(
        uuid.uuid4()
    )

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
                timeout_ms=5000,
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
    proposal: dict,
) -> dict:

    action_type = proposal.get(
        "type"
    )

    if action_type == "REWRITE_QUERY":
        return (
            execute_query_rewrite_proposal(
                proposal
            )
        )

    if action_type == "ANALYZE_TABLE":
        return (
            execute_analyze_table_proposal(
                proposal
            )
        )

    if action_type == "TERMINATE_BACKEND":
        return (
            execute_terminate_backend_proposal(
                proposal
            )
        )

    operation_id = str(
        uuid.uuid4()
    )

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

        create_index(
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
    ):

        try:

            drop_index(
                proposal[
                    "index_name"
                ]
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
                "before_ms": before_ms,
                "after_ms": after_ms,
                "improvement_pct": (
                    improvement
                ),
                "rollback_error": (
                    str(exc)
                ),
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

            "before_ms": before_ms,
            "after_ms": after_ms,
            "improvement_pct": improvement,

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

        "before_ms": before_ms,
        "after_ms": after_ms,
        "improvement_pct": improvement,

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