import re

from db_tools import (
    get_indexes,
)


def find_nodes(
    plan_node: dict,
) -> list[dict]:

    nodes = [
        plan_node
    ]

    for child in plan_node.get(
        "Plans",
        [],
    ):

        nodes.extend(
            find_nodes(
                child
            )
        )

    return nodes


def analyze_query_plan(
    plan: dict,
) -> dict:
    """
    Convert a raw PostgreSQL EXPLAIN ANALYZE
    JSON plan into deterministic,
    structured evidence.

    PostgreSQL Actual Rows and
    Rows Removed by Filter may be
    reported per loop, so row counts
    account for Actual Loops.
    """

    root = plan[
        "Plan"
    ]

    estimated_output_rows = (
        root.get(
            "Plan Rows",
            0,
        )
    )

    root_loops = (
        root.get(
            "Actual Loops",
            1,
        )
    )

    actual_output_rows = (
        root.get(
            "Actual Rows",
            0,
        )
        * root_loops
    )

    # ----------------------------------------
    # Cardinality estimation accuracy
    # ----------------------------------------

    cardinality_error_ratio = None

    if (
        estimated_output_rows > 0
        and actual_output_rows > 0
    ):

        cardinality_error_ratio = max(
            estimated_output_rows
            / actual_output_rows,

            actual_output_rows
            / estimated_output_rows,
        )

    result = {
        "planning_time_ms": (
            plan.get(
                "Planning Time"
            )
        ),

        "execution_time_ms": (
            plan.get(
                "Execution Time"
            )
        ),

        "estimated_output_rows": (
            estimated_output_rows
        ),

        "actual_output_rows": (
            actual_output_rows
        ),

        "cardinality_error_ratio": (
            cardinality_error_ratio
        ),

        "scan_nodes": [],
    }

    # ----------------------------------------
    # Analyze every scan node
    # ----------------------------------------

    scan_types = {
        "Seq Scan",
        "Index Scan",
        "Index Only Scan",
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
    }

    nodes = find_nodes(
        root
    )

    for node in nodes:

        node_type = (
            node.get(
                "Node Type"
            )
        )

        if node_type not in scan_types:
            continue

        loops = (
            node.get(
                "Actual Loops",
                1,
            )
        )

        actual_rows = (
            node.get(
                "Actual Rows",
                0,
            )
        )

        removed_rows = (
            node.get(
                "Rows Removed by Filter",
                0,
            )
        )

        # PostgreSQL may report these
        # values as per-loop averages.
        total_returned = (
            actual_rows
            * loops
        )

        total_removed = (
            removed_rows
            * loops
        )

        total_examined = (
            total_returned
            + total_removed
        )

        selectivity = None

        if total_examined > 0:

            selectivity = (
                total_returned
                / total_examined
            )

        parallel = (
            node.get(
                "Parallel Aware",
                False,
            )
        )

        scan_name = (
            node_type
        )

        if (
            node_type == "Seq Scan"
            and parallel
        ):

            scan_name = (
                "Parallel Sequential Scan"
            )

        result[
            "scan_nodes"
        ].append({
            "scan_type": (
                scan_name
            ),

            "table": (
                node.get(
                    "Relation Name"
                )
            ),

            "filter": (
                node.get(
                    "Filter"
                )
            ),

            "index_name": (
                node.get(
                    "Index Name"
                )
            ),

            "index_cond": (
                node.get(
                    "Index Cond"
                )
            ),

            "recheck_cond": (
                node.get(
                    "Recheck Cond"
                )
            ),

            "loops": loops,

            "rows_returned": (
                total_returned
            ),

            "rows_removed_by_filter": (
                total_removed
            ),

            "rows_examined": (
                total_examined
            ),

            "row_counts_approximate": (
                loops > 1
            ),

            "selectivity": (
                selectivity
            ),

            # Buffer counts are taken
            # directly from PostgreSQL.
            "shared_hit_blocks": (
                node.get(
                    "Shared Hit Blocks",
                    0,
                )
            ),

            "shared_read_blocks": (
                node.get(
                    "Shared Read Blocks",
                    0,
                )
            ),
        })

    return result


def detect_cardinality_anomalies(
    analysis: dict,
    threshold: float = 10.0,
) -> list[dict]:

    findings = []

    estimated_rows = (
        analysis.get(
            "estimated_output_rows"
        )
    )

    actual_rows = (
        analysis.get(
            "actual_output_rows"
        )
    )

    error_ratio = (
        analysis.get(
            "cardinality_error_ratio"
        )
    )

    if (
        estimated_rows is None
        or actual_rows is None
        or error_ratio is None
    ):
        return findings

    if error_ratio < threshold:
        return findings

    if actual_rows > estimated_rows:

        direction = (
            "UNDER_ESTIMATE"
        )

    elif estimated_rows > actual_rows:

        direction = (
            "OVER_ESTIMATE"
        )

    else:

        direction = (
            "MATCH"
        )

    findings.append({
        "type": (
            "SEVERE_CARDINALITY_ERROR"
        ),

        "estimated_rows": (
            estimated_rows
        ),

        "actual_rows": (
            actual_rows
        ),

        "error_ratio": (
            error_ratio
        ),

        "direction": (
            direction
        ),

        "threshold": (
            threshold
        ),
    })

    return findings


def column_has_index(
    column_name: str,
    indexes: list[dict],
) -> bool:

    pattern = (
        rf"\(\s*"
        rf"{re.escape(column_name)}\b"
    )

    for index in indexes:

        index_definition = (
            index[
                "index_definition"
            ]
        )

        if re.search(
            pattern,
            index_definition,
            re.IGNORECASE,
        ):
            return True

    return False


def detect_non_sargable_predicates(
    analysis: dict,
) -> list[dict]:
    """
    Detect simple cases where an
    indexed column is wrapped in a
    function inside a filter predicate.

    Example:

        DATE(created_at)
        = DATE '2026-05-18'

    while created_at itself already
    has an ordinary index.
    """

    findings = []

    function_pattern = re.compile(
        r"\b"
        r"(?P<function>"
        r"[a-zA-Z_][a-zA-Z0-9_]*"
        r")"
        r"\s*\(\s*"
        r"(?P<column>"
        r"[a-zA-Z_][a-zA-Z0-9_]*"
        r")"
        r"\s*\)",
        re.IGNORECASE,
    )

    for scan in analysis.get(
        "scan_nodes",
        [],
    ):

        scan_type = (
            scan.get(
                "scan_type"
            )
        )

        # This detector focuses on
        # predicates for which PostgreSQL
        # still chose sequential access.
        if scan_type not in {
            "Sequential Scan",
            "Parallel Sequential Scan",
        }:
            continue

        table = (
            scan.get(
                "table"
            )
        )

        filter_text = (
            scan.get(
                "filter"
            )
        )

        if (
            not table
            or not filter_text
        ):
            continue

        indexes = get_indexes(
            table
        )

        for match in (
            function_pattern.finditer(
                filter_text
            )
        ):

            function_name = (
                match.group(
                    "function"
                )
            )

            column_name = (
                match.group(
                    "column"
                )
            )

            # Only flag the expression
            # when the underlying column
            # already has an index.
            if not column_has_index(
                column_name,
                indexes,
            ):
                continue

            findings.append({
                "type": (
                    "NON_SARGABLE_FUNCTION_"
                    "ON_INDEXED_COLUMN"
                ),

                "table": table,

                "column": (
                    column_name
                ),

                "function": (
                    function_name.upper()
                ),

                "filter": (
                    filter_text
                ),

                "scan_type": (
                    scan_type
                ),

                "reason": (
                    f"Column "
                    f"'{table}.{column_name}' "
                    f"has an index, but the "
                    f"predicate wraps it in "
                    f"{function_name.upper()}"
                    f"(...). This may prevent "
                    f"PostgreSQL from using "
                    f"the ordinary index "
                    f"directly."
                ),
            })

    return findings