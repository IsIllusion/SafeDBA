from db_tools import (
    ensure_read_only_query,
    get_indexes,
    get_lock_waits,
    get_table_columns,
)

import hashlib
import math

from diagnostics import (
    column_has_index,
)

from safety import (
    assess_risk,
)


def build_index_name(
    table: str,
    column: str,
) -> str:
    """Build a stable PostgreSQL identifier no longer than 63 bytes."""

    base = f"idx_{table}_{column}"

    if len(base.encode("utf-8")) <= 63:
        return base

    suffix = (
        "_"
        + hashlib.sha256(
            base.encode("utf-8")
        ).hexdigest()[:10]
    )
    byte_budget = 63 - len(
        suffix.encode("utf-8")
    )
    prefix_bytes = base.encode("utf-8")[
        :byte_budget
    ]

    while True:
        try:
            prefix = prefix_bytes.decode("utf-8")
            break
        except UnicodeDecodeError:
            prefix_bytes = prefix_bytes[:-1]

    return prefix + suffix


def build_create_index_proposal(
    query: str,
    table: str,
    column: str,
    reason: str,
    confidence: float,
) -> dict:

    index_name = build_index_name(
        table,
        column,
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
    blocker_backend_start: str,
    blocker_xact_start: str,
    reason: str,
    confidence: float,
) -> dict:

    return {
        "type": "TERMINATE_BACKEND",
        "blocked_pid": blocked_pid,
        "blocker_pid": blocker_pid,
        "blocker_backend_start": blocker_backend_start,
        "blocker_xact_start": blocker_xact_start,
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

    if (
        isinstance(confidence, bool)
        or not isinstance(
            confidence,
            (int, float),
        )
    ):
        errors.append(
            "Confidence must be numeric."
        )

    elif not math.isfinite(
        float(confidence)
    ):
        errors.append(
            "Confidence must be finite."
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

    query = proposal.get(
        "query"
    )

    reason = proposal.get(
        "reason"
    )

    index_name = proposal.get(
        "index_name"
    )

    if not isinstance(table, str) or not table:
        errors.append(
            "Missing table name."
        )

    if not isinstance(column, str) or not column:
        errors.append(
            "Missing column name."
        )

    if not isinstance(query, str) or not query.strip():
        errors.append(
            "Missing diagnostic query."
        )
    else:
        try:
            ensure_read_only_query(query)
        except ValueError as exc:
            errors.append(str(exc))

    if not isinstance(reason, str) or not reason.strip():
        errors.append(
            "Missing proposal reason."
        )

    if not isinstance(index_name, str) or not index_name:
        errors.append(
            "Missing index name."
        )
    elif (
        isinstance(table, str)
        and isinstance(column, str)
        and index_name
        != build_index_name(table, column)
    ):
        errors.append(
            "Index name does not match the deterministic name."
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

    if not isinstance(original_query, str) or not original_query.strip():
        errors.append(
            "Missing original query."
        )
    else:
        try:
            ensure_read_only_query(
                original_query
            )
        except ValueError as exc:
            errors.append(str(exc))

    if not isinstance(rewritten_query, str) or not rewritten_query.strip():
        errors.append(
            "Missing rewritten query."
        )
    else:
        try:
            ensure_read_only_query(
                rewritten_query
            )
        except ValueError as exc:
            errors.append(str(exc))

    if (
        isinstance(original_query, str)
        and isinstance(rewritten_query, str)
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

    if not isinstance(query, str) or not query.strip():
        errors.append(
            "Missing diagnostic query."
        )
    else:
        try:
            ensure_read_only_query(query)
        except ValueError as exc:
            errors.append(str(exc))

    if not isinstance(table, str) or not table:
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

    elif any(
        not isinstance(column, str)
        or not column
        for column in columns
    ):
        errors.append(
            "Every column must be a non-empty string."
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

    blocker_backend_start = proposal.get(
        "blocker_backend_start"
    )

    blocker_xact_start = proposal.get(
        "blocker_xact_start"
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

    for field, value in (
        ("blocker_backend_start", blocker_backend_start),
        ("blocker_xact_start", blocker_xact_start),
    ):
        if not isinstance(value, str) or not value.strip():
            errors.append(
                f"{field} must be a non-empty timestamp string."
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
            and lock_wait.get(
                "blocker_backend_start"
            )
            == blocker_backend_start
            and lock_wait.get(
                "blocker_xact_start"
            )
            == blocker_xact_start
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


def validate_proposal_shape(
    proposal: dict,
) -> dict:
    """Validate a built proposal without issuing any database query."""

    if not isinstance(proposal, dict):
        return {
            "valid": False,
            "errors": ["Action proposal must be an object."],
        }

    errors: list[str] = []
    action_type = proposal.get("type")
    supported = {
        "CREATE_INDEX",
        "REWRITE_QUERY",
        "ANALYZE_TABLE",
        "TERMINATE_BACKEND",
    }
    if action_type not in supported:
        errors.append(
            f"Unsupported action type: {action_type}"
        )

    reason = proposal.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(
            "Proposal reason must be a non-empty string."
        )

    validate_confidence(proposal, errors)

    if action_type in supported:
        expected_risk = assess_risk(action_type)
        if proposal.get("risk") != expected_risk:
            errors.append(
                "Proposal risk does not match the deterministic policy."
            )

    def require_read_only(name: str) -> None:
        value = proposal.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{name} must be a non-empty query string.")
            return
        try:
            ensure_read_only_query(value)
        except ValueError as exc:
            errors.append(str(exc))

    if action_type == "CREATE_INDEX":
        table = proposal.get("table")
        column = proposal.get("column")
        require_read_only("query")
        if not isinstance(table, str) or not table.strip():
            errors.append("Missing table name.")
        if not isinstance(column, str) or not column.strip():
            errors.append("Missing column name.")

        index_name = proposal.get("index_name")
        if not isinstance(index_name, str) or not index_name.strip():
            errors.append("Missing index name.")
        elif (
            isinstance(table, str)
            and table.strip()
            and isinstance(column, str)
            and column.strip()
            and index_name != build_index_name(table, column)
        ):
            errors.append(
                "Index name does not match the deterministic name."
            )

    elif action_type == "REWRITE_QUERY":
        require_read_only("original_query")
        require_read_only("rewritten_query")
        original = proposal.get("original_query")
        rewritten = proposal.get("rewritten_query")
        if (
            isinstance(original, str)
            and isinstance(rewritten, str)
            and original.strip() == rewritten.strip()
        ):
            errors.append(
                "Rewritten query is identical to original query."
            )

    elif action_type == "ANALYZE_TABLE":
        require_read_only("query")
        table = proposal.get("table")
        columns = proposal.get("columns")
        if not isinstance(table, str) or not table.strip():
            errors.append("Missing table name.")
        if (
            not isinstance(columns, list)
            or not columns
            or any(
                not isinstance(column, str) or not column.strip()
                for column in columns
            )
        ):
            errors.append(
                "Columns must be a non-empty list of non-empty strings."
            )

    elif action_type == "TERMINATE_BACKEND":
        blocked_pid = proposal.get("blocked_pid")
        blocker_pid = proposal.get("blocker_pid")
        for name, pid in (
            ("blocked_pid", blocked_pid),
            ("blocker_pid", blocker_pid),
        ):
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid <= 0
            ):
                errors.append(
                    f"{name} must be a positive integer."
                )
        if (
            isinstance(blocked_pid, int)
            and not isinstance(blocked_pid, bool)
            and isinstance(blocker_pid, int)
            and not isinstance(blocker_pid, bool)
            and blocked_pid == blocker_pid
        ):
            errors.append(
                "blocked_pid and blocker_pid must be different."
            )
        for name in (
            "blocker_backend_start",
            "blocker_xact_start",
        ):
            value = proposal.get(name)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"{name} must be a non-empty timestamp string."
                )

    return {
        "valid": not errors,
        "errors": errors,
    }


def validate_action_proposal(
    proposal: dict,
) -> dict:

    shape_result = validate_proposal_shape(proposal)
    if not shape_result["valid"]:
        return shape_result

    action_type = proposal.get(
        "type"
    )

    validator = None

    if action_type == "CREATE_INDEX":
        validator = (
            validate_create_index_proposal
        )
    elif action_type == "REWRITE_QUERY":
        validator = (
            validate_query_rewrite_proposal
        )
    elif action_type == "ANALYZE_TABLE":
        validator = (
            validate_analyze_table_proposal
        )
    elif action_type == "TERMINATE_BACKEND":
        validator = (
            validate_terminate_backend_proposal
        )

    if validator is None:
        return {
            "valid": False,
            "errors": [
                f"Unsupported action type: "
                f"{action_type}"
            ],
        }

    result = validator(
        proposal
    )
    errors = list(
        result.get("errors", [])
    )

    result["valid"] = len(errors) == 0
    result["errors"] = errors
    return result
