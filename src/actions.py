from db_tools import (
    get_indexes,
    get_lock_waits,
    get_table_columns,
)

from diagnostics import (
    column_has_index,
)

from safety import (
    assess_risk,
)


def build_create_index_proposal(
    query: str,
    table: str,
    column: str,
    reason: str,
    confidence: float,
) -> dict:

    index_name = (
        f"idx_{table}_{column}"
    )

    return {
        "type": "CREATE_INDEX",
        "query": query,
        "table": table,
        "column": column,
        "index_name": index_name,
        "reason": reason,
        "confidence": confidence,
        "risk": assess_risk(
            "CREATE_INDEX"
        ),
    }

def build_query_rewrite_proposal(
    original_query: str,
    rewritten_query: str,
    reason: str,
    confidence: float,
) -> dict:

    return {
        "type": "REWRITE_QUERY",
        "original_query": original_query,
        "rewritten_query": rewritten_query,
        "reason": reason,
        "confidence": confidence,
        "risk": assess_risk(
            "REWRITE_QUERY"
        ),
    }

def build_analyze_table_proposal(
    query: str,
    table: str,
    columns: list[str],
    reason: str,
    confidence: float,
) -> dict:

    return {
        "type": "ANALYZE_TABLE",
        "query": query,
        "table": table,
        "columns": columns,
        "reason": reason,
        "confidence": confidence,
        "risk": assess_risk(
            "ANALYZE_TABLE"
        ),
    }

def build_terminate_backend_proposal(
    blocked_pid: int,
    blocker_pid: int,
    reason: str,
    confidence: float,
) -> dict:

    return {
        "type": "TERMINATE_BACKEND",
        "blocked_pid": blocked_pid,
        "blocker_pid": blocker_pid,
        "reason": reason,
        "confidence": confidence,
        "risk": assess_risk(
            "TERMINATE_BACKEND"
        ),
    }


def validate_confidence(
    proposal: dict,
    errors: list[str],
) -> None:

    confidence = proposal.get(
        "confidence"
    )

    if not isinstance(
        confidence,
        (int, float),
    ):
        errors.append(
            "Confidence must be numeric."
        )

    elif not (
        0 <= confidence <= 1
    ):
        errors.append(
            "Confidence must be between 0 and 1."
        )


def validate_create_index_proposal(
    proposal: dict,
) -> dict:

    errors = []

    table = proposal.get(
        "table"
    )

    column = proposal.get(
        "column"
    )

    if not table:
        errors.append(
            "Missing table name."
        )

    if not column:
        errors.append(
            "Missing column name."
        )

    if errors:
        return {
            "valid": False,
            "errors": errors,
        }

    columns = get_table_columns(
        table
    )

    if not columns:
        errors.append(
            f"Table '{table}' does not exist "
            f"in schema public."
        )

        return {
            "valid": False,
            "errors": errors,
        }

    if column not in columns:
        errors.append(
            f"Column '{table}.{column}' "
            f"does not exist."
        )

    indexes = get_indexes(
        table
    )

    if column_has_index(
        column,
        indexes,
    ):
        errors.append(
            f"Column '{table}.{column}' "
            f"already appears to have "
            f"a matching index."
        )

    validate_confidence(
        proposal,
        errors,
    )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }


def validate_query_rewrite_proposal(
    proposal: dict,
) -> dict:

    errors = []

    original_query = proposal.get(
        "original_query"
    )

    rewritten_query = proposal.get(
        "rewritten_query"
    )

    if not original_query:
        errors.append(
            "Missing original query."
        )

    if not rewritten_query:
        errors.append(
            "Missing rewritten query."
        )

    if (
        original_query
        and rewritten_query
        and original_query.strip()
        == rewritten_query.strip()
    ):
        errors.append(
            "Rewritten query is identical "
            "to original query."
        )

    validate_confidence(
        proposal,
        errors,
    )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }


def validate_analyze_table_proposal(
    proposal: dict,
) -> dict:

    errors = []

    query = proposal.get(
        "query"
    )

    table = proposal.get(
        "table"
    )

    columns = proposal.get(
        "columns",
        [],
    )

    if not query:
        errors.append(
            "Missing diagnostic query."
        )

    if not table:
        errors.append(
            "Missing table name."
        )

    if not isinstance(
        columns,
        list,
    ):
        errors.append(
            "Columns must be a list."
        )

    if errors:
        return {
            "valid": False,
            "errors": errors,
        }

    table_columns = (
        get_table_columns(
            table
        )
    )

    if not table_columns:
        errors.append(
            f"Table '{table}' does not "
            f"exist in schema public."
        )

        return {
            "valid": False,
            "errors": errors,
        }

    invalid_columns = [
        column
        for column in columns
        if column not in table_columns
    ]

    if invalid_columns:
        errors.append(
            "Unknown columns: "
            + ", ".join(
                invalid_columns
            )
        )

    validate_confidence(
        proposal,
        errors,
    )

    return {
        "valid": (
            len(errors) == 0
        ),
        "errors": errors,
    }

def validate_terminate_backend_proposal(
    proposal: dict,
) -> dict:

    errors = []

    blocked_pid = proposal.get(
        "blocked_pid"
    )

    blocker_pid = proposal.get(
        "blocker_pid"
    )

    # bool is a subclass of int in Python,
    # so reject bool explicitly.
    if (
        not isinstance(
            blocked_pid,
            int,
        )
        or isinstance(
            blocked_pid,
            bool,
        )
        or blocked_pid <= 0
    ):
        errors.append(
            "blocked_pid must be "
            "a positive integer."
        )

    if (
        not isinstance(
            blocker_pid,
            int,
        )
        or isinstance(
            blocker_pid,
            bool,
        )
        or blocker_pid <= 0
    ):
        errors.append(
            "blocker_pid must be "
            "a positive integer."
        )

    if (
        isinstance(
            blocked_pid,
            int,
        )
        and not isinstance(
            blocked_pid,
            bool,
        )
        and isinstance(
            blocker_pid,
            int,
        )
        and not isinstance(
            blocker_pid,
            bool,
        )
        and blocked_pid == blocker_pid
    ):
        errors.append(
            "blocked_pid and blocker_pid "
            "must be different."
        )

    validate_confidence(
        proposal,
        errors,
    )

    if errors:
        return {
            "valid": False,
            "errors": errors,
        }

    lock_waits = get_lock_waits()

    matching_relationship = None

    for lock_wait in lock_waits:

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
            matching_relationship = (
                lock_wait
            )
            break

    if matching_relationship is None:

        errors.append(
            f"PID {blocker_pid} is not "
            f"currently reported as "
            f"blocking PID {blocked_pid}."
        )

        return {
            "valid": False,
            "errors": errors,
        }

    if (
        matching_relationship.get(
            "blocked_wait_event_type"
        )
        != "Lock"
    ):
        errors.append(
            f"PID {blocked_pid} is not "
            f"currently waiting on a "
            f"PostgreSQL Lock event."
        )

    blocker_state = (
        matching_relationship.get(
            "blocker_state"
        )
    )

    if blocker_state not in {
        "idle in transaction",
        "idle in transaction (aborted)",
    }:
        errors.append(
            f"PID {blocker_pid} is "
            f"currently in state "
            f"'{blocker_state}', not an "
            f"idle-in-transaction state. "
            f"Automatic termination is "
            f"outside the current "
            f"SafeDBA policy scope."
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "current_blocking_evidence": (
            matching_relationship
        ),
    }


def validate_action_proposal(
    proposal: dict,
) -> dict:

    action_type = proposal.get(
        "type"
    )

    if action_type == "CREATE_INDEX":
        return (
            validate_create_index_proposal(
                proposal
            )
        )

    if action_type == "REWRITE_QUERY":
        return (
            validate_query_rewrite_proposal(
                proposal
            )
        )

    if action_type == "ANALYZE_TABLE":
        return (
            validate_analyze_table_proposal(
                proposal
            )
        )

    if action_type == "TERMINATE_BACKEND":
        return (
            validate_terminate_backend_proposal(
                proposal
            )
        )

    return {
        "valid": False,
        "errors": [
            f"Unsupported action type: "
            f"{action_type}"
        ],
    }