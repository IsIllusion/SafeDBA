import statistics

import hashlib
import json
from contextlib import contextmanager
from datetime import (
    datetime,
    timezone,
)

import psycopg
from psycopg import sql
from runtime_policy import require_operation
import config as runtime_config

from config import (
    DB_CONFIG,
    DB_INCLUDE_OBSERVED_QUERY_TEXT,
    DB_LOCK_TIMEOUT_MS,
    DB_MAX_EXPLAIN_TOTAL_COST,
    DB_MAX_OBSERVATION_ROWS,
    DB_MAX_OBSERVED_QUERY_CHARS,
    DB_MAX_QUERY_LENGTH,
    DB_STATEMENT_TIMEOUT_MS,
    EXECUTOR_DB_CONFIG,
    HEALTH_LONG_QUERY_SECONDS,
    HEALTH_LONG_TRANSACTION_SECONDS,
    TERMINATOR_DB_CONFIG,
)

from query_guard import (
    ensure_read_only_query as enforce_query_policy,
)

import db_catalog
import db_operational
import db_sessions
from db_observation_context import ObservationDependencies

def ensure_read_only_query(
    query: str,
) -> str:
    return enforce_query_policy(
        query,
        max_length=DB_MAX_QUERY_LENGTH,
    )


@contextmanager
def readonly_connection():
    """Open a bounded, transaction-level read-only connection."""
    require_operation("OBSERVE")

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SET TRANSACTION READ ONLY"
            )
            cur.execute(
                "SET LOCAL standard_conforming_strings = on"
            )
            cur.execute(
                "SELECT set_config("
                "'statement_timeout', %s, true)",
                (str(DB_STATEMENT_TIMEOUT_MS),),
            )
            cur.execute(
                "SELECT set_config("
                "'lock_timeout', %s, true)",
                (str(DB_LOCK_TIMEOUT_MS),),
            )
            cur.execute(
                "SELECT set_config("
                "'idle_in_transaction_session_timeout', "
                "%s, true)",
                (str(DB_STATEMENT_TIMEOUT_MS),),
            )

        yield conn


@contextmanager
def executor_connection():
    """Open a bounded connection for deterministic mutations."""
    if getattr(runtime_config, "PROCESS_ROLE", "combined") != "combined":
        raise RuntimeError("Isolated processes cannot open maintenance connections.")

    with psycopg.connect(
        **EXECUTOR_DB_CONFIG
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config("
                "'statement_timeout', %s, true)",
                (str(DB_STATEMENT_TIMEOUT_MS),),
            )
            cur.execute(
                "SELECT set_config("
                "'lock_timeout', %s, true)",
                (str(DB_LOCK_TIMEOUT_MS),),
            )
            cur.execute(
                "SELECT set_config("
                "'idle_in_transaction_session_timeout', "
                "%s, true)",
                (str(DB_STATEMENT_TIMEOUT_MS),),
            )

        yield conn


@contextmanager
def terminator_connection():
    """Open a bounded connection that can signal, but not mutate, data."""
    if getattr(runtime_config, "PROCESS_ROLE", "combined") == "agent":
        raise RuntimeError("Agent-only process cannot open privileged connections.")

    with psycopg.connect(
        **TERMINATOR_DB_CONFIG
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SET TRANSACTION READ ONLY"
            )
            cur.execute(
                "SELECT set_config("
                "'statement_timeout', %s, true)",
                (str(DB_STATEMENT_TIMEOUT_MS),),
            )
            cur.execute(
                "SELECT set_config("
                "'lock_timeout', %s, true)",
                (str(DB_LOCK_TIMEOUT_MS),),
            )
            cur.execute(
                "SELECT set_config("
                "'idle_in_transaction_session_timeout', "
                "%s, true)",
                (str(DB_STATEMENT_TIMEOUT_MS),),
            )

        yield conn


def _inspect_runtime_identity(
    config: dict,
) -> dict:
    statement = """
    SELECT
        current_user,
        current_database(),
        role.rolsuper,
        role.rolcreaterole,
        role.rolcreatedb,
        role.rolreplication,
        role.rolbypassrls,
        current_setting(
            'default_transaction_read_only'
        )::boolean,
        has_database_privilege(
            current_user,
            current_database(),
            'TEMPORARY'
        ),
        has_schema_privilege(
            current_user,
            'public',
            'CREATE'
        ),
        pg_has_role(
            current_user,
            'pg_signal_backend',
            'MEMBER'
        ),
        EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND (
                  has_table_privilege(
                      current_user, relation.oid, 'INSERT'
                  )
                  OR has_table_privilege(
                      current_user, relation.oid, 'UPDATE'
                  )
                  OR has_table_privilege(
                      current_user, relation.oid, 'DELETE'
                  )
                  OR has_table_privilege(
                      current_user, relation.oid, 'TRUNCATE'
                  )
                  OR has_table_privilege(
                      current_user, relation.oid, 'TRIGGER'
                  )
              )
        )
    FROM pg_roles AS role
    WHERE role.rolname = current_user;
    """

    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                statement,
                prepare=True,
            )
            row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            "Could not inspect the configured PostgreSQL identity."
        )

    return {
        "user": row[0],
        "database": row[1],
        "superuser": row[2],
        "create_role": row[3],
        "create_database": row[4],
        "replication": row[5],
        "bypass_rls": row[6],
        "default_read_only": row[7],
        "temporary": row[8],
        "schema_create": row[9],
        "signal_backend": row[10],
        "table_write_privilege": row[11],
    }


def verify_runtime_security() -> dict:
    """Fail before LLM use when configured DB roles violate policy."""
    if getattr(runtime_config, "PROCESS_ROLE", "combined") == "agent":
        observer = _inspect_runtime_identity(DB_CONFIG)
        if (
            observer["user"] != DB_CONFIG["user"]
            or not observer["default_read_only"]
            or any(observer[field] for field in (
                "superuser", "create_role", "create_database", "replication",
                "bypass_rls", "temporary", "schema_create", "signal_backend",
                "table_write_privilege",
            ))
        ):
            raise RuntimeError("Agent observer role violates the read-only contract.")
        return {"observer": observer}

    inspected_configs = {"observer": DB_CONFIG}
    if getattr(runtime_config, "PROCESS_ROLE", "combined") == "combined":
        inspected_configs["executor"] = EXECUTOR_DB_CONFIG
    inspected_configs["terminator"] = TERMINATOR_DB_CONFIG
    identities = {label: _inspect_runtime_identity(value) for label, value in inspected_configs.items()}
    actual_users = {
        identity["user"]
        for identity in identities.values()
    }
    errors = []

    if len(actual_users) != len(inspected_configs):
        errors.append(
            "Observer, executor, and terminator must be distinct users."
        )

    if len({
        identity["database"]
        for identity in identities.values()
    }) != 1:
        errors.append(
            "All runtime identities must target the same database."
        )

    for label, identity in identities.items():
        if identity["user"] != {
            "observer": DB_CONFIG["user"],
            "executor": EXECUTOR_DB_CONFIG["user"],
            "terminator": TERMINATOR_DB_CONFIG["user"],
        }[label]:
            errors.append(
                f"{label} current_user does not match its configuration."
            )
        if any(
            identity[capability]
            for capability in (
                "superuser",
                "create_role",
                "create_database",
                "replication",
                "bypass_rls",
            )
        ):
            errors.append(
                f"{label} has a forbidden cluster-level capability."
            )

    observer = identities["observer"]
    if not observer["default_read_only"]:
        errors.append("Observer default transactions are not read-only.")
    if any(
        observer[capability]
        for capability in (
            "temporary",
            "schema_create",
            "signal_backend",
            "table_write_privilege",
        )
    ):
        errors.append(
            "Observer has temporary, create, signal, or table-write rights."
        )

    if "executor" in identities:
        executor = identities["executor"]
        if executor["signal_backend"]:
            errors.append("Maintenance executor must not have pg_signal_backend.")
        if not executor["schema_create"]:
            errors.append("Maintenance executor lacks CREATE on the managed schema.")

    terminator = identities["terminator"]
    if not terminator["default_read_only"]:
        errors.append("Terminator default transactions are not read-only.")
    if (
        not terminator["signal_backend"]
        or terminator["temporary"]
        or terminator["schema_create"]
        or terminator["table_write_privilege"]
    ):
        errors.append(
            "Terminator must have only read visibility and backend signal "
            "authority."
        )

    if errors:
        raise RuntimeError(
            "Unsafe PostgreSQL runtime identity configuration: "
            + " ".join(errors)
            + " Recreate/migrate the demo roles before running SafeDBA."
        )

    return identities


def truncate_observed_query(
    value: str | None,
) -> str | None:
    if value is None:
        return value

    if not DB_INCLUDE_OBSERVED_QUERY_TEXT:
        return (
            "[redacted query sha256="
            + hashlib.sha256(
                value.encode("utf-8")
            ).hexdigest()
            + f" characters={len(value)}]"
        )

    if len(value) <= DB_MAX_OBSERVED_QUERY_CHARS:
        return value

    return (
        value[:DB_MAX_OBSERVED_QUERY_CHARS]
        + "...[truncated]"
    )


def _run_explain(
    query: str,
    *,
    analyze: bool,
) -> dict:
    require_operation("EXPLAIN_ANALYZE" if analyze else "OBSERVE")
    query = ensure_read_only_query(
        query
    )

    options = (
        "ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON"
        if analyze
        else "FORMAT JSON"
    )
    explain_sql = (
        f"EXPLAIN ({options})\n{query}"
    )

    with readonly_connection() as conn:
        with conn.cursor() as cur:
            # prepare=True forces PostgreSQL's extended protocol, whose
            # Parse step accepts exactly one statement.  This remains a
            # separate safety boundary from the client-side SQL policy.
            require_operation("EXPLAIN_ANALYZE" if analyze else "OBSERVE")
            cur.execute(
                explain_sql,
                prepare=True,
            )
            result = cur.fetchone()

    if not result or not result[0]:
        raise RuntimeError(
            "PostgreSQL returned an empty EXPLAIN result."
        )

    return result[0][0]


def get_estimated_query_plan(
    query: str,
) -> dict:
    """Plan a diagnostic query without executing it."""

    return _run_explain(
        query,
        analyze=False,
    )


def get_query_plan(query: str) -> dict:
    """Run bounded EXPLAIN ANALYZE after a cost-only preflight."""
    require_operation("EXPLAIN_ANALYZE")

    estimated_plan = get_estimated_query_plan(
        query
    )
    total_cost = (
        estimated_plan.get("Plan", {})
        .get("Total Cost")
    )

    if total_cost is None:
        raise RuntimeError(
            "Estimated plan did not contain Total Cost."
        )

    if total_cost > DB_MAX_EXPLAIN_TOTAL_COST:
        raise ValueError(
            "EXPLAIN ANALYZE blocked by the cost policy: "
            f"estimated total cost {total_cost} exceeds "
            f"{DB_MAX_EXPLAIN_TOTAL_COST}. Use the estimated "
            "plan or an isolated environment instead."
        )

    analyzed_plan = _run_explain(
        query,
        analyze=True,
    )
    analyzed_plan["SafeDBA Preflight"] = {
        "estimated_total_cost": total_cost,
        "maximum_total_cost": (
            DB_MAX_EXPLAIN_TOTAL_COST
        ),
    }

    return analyzed_plan


def _observation_dependencies() -> ObservationDependencies:
    """Compose per call so overrides and existing public test seams stay live."""
    return ObservationDependencies(
        connect=readonly_connection,
        redact_query=truncate_observed_query,
        health=get_database_health,
        now=lambda: datetime.now(timezone.utc),
        database_name=DB_CONFIG.get("dbname"),
        max_rows=DB_MAX_OBSERVATION_ROWS,
        long_query_seconds=HEALTH_LONG_QUERY_SECONDS,
        long_transaction_seconds=HEALTH_LONG_TRANSACTION_SECONDS,
    )


def get_indexes(table_name: str) -> list[dict]:
    return db_catalog.get_indexes(table_name, context=_observation_dependencies())


def get_table_columns(
    table_name: str,
) -> list[str]:
    return db_catalog.get_table_columns(table_name, context=_observation_dependencies())

def get_column_info(
    table_name: str,
    column_name: str,
) -> dict | None:
    return db_catalog.get_column_info(
        table_name, column_name, context=_observation_dependencies()
    )

def get_column_stats(
    table_name: str,
    column_name: str,
) -> dict | None:
    return db_catalog.get_column_stats(
        table_name, column_name, context=_observation_dependencies()
    )

def get_database_health() -> dict:
    """
    Capture a lightweight runtime health
    snapshot for the current PostgreSQL
    database.

    This is a triage tool, not a complete
    database health assessment.
    """
    return db_operational.get_database_health(context=_observation_dependencies())

def get_operational_snapshot() -> dict:
    """Collect broad, read-only PostgreSQL operational evidence.

    The snapshot combines low-cost observations so a general incident can be
    routed from one Agent tool call. It reports PostgreSQL-visible sizes, not
    filesystem capacity.
    """
    return db_operational.get_operational_snapshot(context=_observation_dependencies())


def get_active_sessions() -> list[dict]:
    """
    Return current active PostgreSQL client
    sessions for the configured database.

    This is an observational triage tool.
    It does not cancel or terminate sessions.
    """
    return db_sessions.get_active_sessions(context=_observation_dependencies())

def get_transaction_sessions() -> list[dict]:
    """
    Return client sessions with operationally
    relevant open transactions.

    Includes idle-in-transaction sessions and
    transactions exceeding the configured
    long-transaction threshold.

    This tool is read-only.
    """
    return db_sessions.get_transaction_sessions(context=_observation_dependencies())

def get_lock_graph_snapshot() -> dict:
    return db_sessions.get_lock_graph_snapshot(context=_observation_dependencies())


def get_lock_waits() -> list[dict]:
    """Compatibility wrapper for the Agent observation tool."""
    return get_lock_graph_snapshot()["rows"]


def terminate_blocking_backend(
    blocked_pid: int,
    blocker_pid: int,
    blocker_backend_start: str,
    blocker_xact_start: str,
    timeout_ms: int = 5000,
    *,
    blocked_backend_start: str,
    blocked_xact_start: str,
) -> dict:
    require_operation("TERMINATE_BACKEND")

    statement = """
    WITH evidence AS MATERIALIZED (
        SELECT
            pg_backend_pid()
                AS executor_pid,

            current_database()
                AS executor_database,

            blocked.pid
                AS blocked_pid,

            blocked.datname
                AS blocked_database,

            blocked.state
                AS blocked_state,

            blocked.wait_event_type
                AS blocked_wait_event_type,

            blocked.wait_event
                AS blocked_wait_event,

            blocked.query
                AS blocked_query,

            blocked.xact_start
                AS blocked_xact_start,

            blocked.backend_start
                AS blocked_backend_start,

            blocker.pid
                AS blocker_pid,

            blocker.datname
                AS blocker_database,

            blocker.usename
                AS blocker_user,

            blocker.backend_type
                AS blocker_backend_type,

            blocker.state
                AS blocker_state,

            blocker.query
                AS blocker_query,

            blocker.xact_start
                AS blocker_xact_start,

            blocker.backend_start
                AS blocker_backend_start,

            blocker.pid = ANY(
                pg_blocking_pids(
                    blocked.pid
                )
            ) AS still_blocking

        FROM pg_stat_activity
            AS blocked

        JOIN pg_stat_activity
            AS blocker
            ON blocker.pid = %s

        WHERE blocked.pid = %s
          AND blocked.backend_start = %s::timestamptz
          AND blocked.xact_start = %s::timestamptz
          AND blocker.backend_start = %s::timestamptz
          AND blocker.xact_start = %s::timestamptz
    )

    SELECT
        executor_pid,
        executor_database,

        blocked_pid,
        blocked_database,
        blocked_state,
        blocked_wait_event_type,
        blocked_wait_event,
        blocked_query,
        blocked_xact_start,
        blocked_backend_start,

        blocker_pid,
        blocker_database,
        blocker_user,
        blocker_backend_type,
        blocker_state,
        blocker_query,
        blocker_xact_start,
        blocker_backend_start,

        still_blocking,

        (
            still_blocking

            AND blocked_wait_event_type
                = 'Lock'

            AND blocker_pid
                <> executor_pid

            AND blocker_backend_type
                = 'client backend'

            AND blocked_database
                = executor_database

            AND blocker_database
                = executor_database

            AND blocker_state IN (
                'idle in transaction',
                'idle in transaction (aborted)'
            )
        ) AS final_validation_passed,

        CASE
            WHEN (
                still_blocking

                AND blocked_wait_event_type
                    = 'Lock'

                AND blocker_pid
                    <> executor_pid

                AND blocker_backend_type
                    = 'client backend'

                AND blocked_database
                    = executor_database

                AND blocker_database
                    = executor_database

                AND blocker_state IN (
                    'idle in transaction',
                    'idle in transaction (aborted)'
                )
            )

            THEN pg_terminate_backend(
                blocker_pid,
                %s
            )

            ELSE false
        END AS terminated

    FROM evidence;
    """

    with terminator_connection() as conn:

        with conn.cursor() as cur:

            require_operation("TERMINATE_BACKEND")
            cur.execute(
                statement,
                (
                    blocker_pid,
                    blocked_pid,
                    blocked_backend_start,
                    blocked_xact_start,
                    blocker_backend_start,
                    blocker_xact_start,
                    timeout_ms,
                ),
            )

            row = cur.fetchone()

    if row is None:

        return {
            "final_validation_passed": False,
            "terminated": False,
            "reason": (
                "The blocked or blocking "
                "backend is no longer present."
            ),
        }

    return {
        "executor_pid": row[0],

        "executor_database": row[1],

        "blocked_pid": row[2],

        "blocked_database": row[3],

        "blocked_state": row[4],

        "blocked_wait_event_type": (
            row[5]
        ),

        "blocked_wait_event": (
            row[6]
        ),

        "blocked_query": (
            truncate_observed_query(
                row[7]
            )
        ),

        "blocked_xact_start": (
            row[8].isoformat()
            if row[8] is not None
            else None
        ),

        "blocked_backend_start": (
            row[9].isoformat()
            if row[9] is not None
            else None
        ),

        "blocker_pid": row[10],

        "blocker_database": row[11],

        "blocker_user": row[12],

        "blocker_backend_type": (
            row[13]
        ),

        "blocker_state": row[14],

        "blocker_query": (
            truncate_observed_query(
                row[15]
            )
        ),

        "blocker_xact_start": (
            row[16].isoformat()
            if row[16] is not None
            else None
        ),

        "blocker_backend_start": (
            row[17].isoformat()
            if row[17] is not None
            else None
        ),

        "still_blocking": (
            row[18]
        ),

        "final_validation_passed": (
            row[19]
        ),

        "terminated": (
            row[20]
        ),
    }


def create_index(
    table: str,
    column: str,
    index_name: str,
) -> dict:
    require_operation("CREATE_INDEX")

    statement = sql.SQL(
        "CREATE INDEX {} ON {} ({})"
    ).format(
        # PostgreSQL places an index in its table's schema and doesn't
        # allow a schema-qualified index name in CREATE INDEX.
        sql.Identifier(index_name),
        sql.Identifier("public", table),
        sql.Identifier(column),
    )

    with executor_connection() as conn:
        with conn.cursor() as cur:
            require_operation("CREATE_INDEX")
            cur.execute(statement)
            cur.execute(
                """
                SELECT
                    index_relation.oid,
                    table_relation.oid,
                    namespace.nspname,
                    index_relation.relname
                FROM pg_class AS index_relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = index_relation.relnamespace
                JOIN pg_index AS index_metadata
                  ON index_metadata.indexrelid = index_relation.oid
                JOIN pg_class AS table_relation
                  ON table_relation.oid = index_metadata.indrelid
                WHERE namespace.nspname = 'public'
                  AND index_relation.relname = %s
                  AND table_relation.relname = %s;
                """,
                (index_name, table),
            )
            identity = cur.fetchone()

    if identity is None:
        raise RuntimeError(
            "Created index could not be rebound to its catalog identity."
        )

    return {
        "index_oid": identity[0],
        "table_oid": identity[1],
        "schema": identity[2],
        "index_name": identity[3],
    }



def analyze_table(
    table_name: str,
    columns: list[str] | None = None,
) -> None:
    require_operation("ANALYZE_TABLE")

    columns = columns or []

    with executor_connection() as conn:

        with conn.cursor() as cur:

            if columns:

                column_sql = sql.SQL(
                    ", "
                ).join(
                    sql.Identifier(
                        column
                    )
                    for column in columns
                )

                statement = sql.SQL(
                    "ANALYZE {} ({})"
                ).format(
                    sql.Identifier(
                        "public",
                        table_name,
                    ),
                    column_sql,
                )

            else:

                statement = sql.SQL(
                    "ANALYZE {}"
                ).format(
                    sql.Identifier(
                        "public",
                        table_name,
                    )
                )

            require_operation("ANALYZE_TABLE")
            cur.execute(statement)



def drop_index(
    index_name: str,
    *,
    expected_index_oid: int,
    expected_table_oid: int,
) -> None:
    require_operation("DROP_INDEX")
    statement = sql.SQL(
        "DROP INDEX {}"
    ).format(
        sql.Identifier("public", index_name)
    )

    with executor_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    index_relation.oid,
                    table_relation.oid
                FROM pg_class AS index_relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = index_relation.relnamespace
                JOIN pg_index AS index_metadata
                  ON index_metadata.indexrelid = index_relation.oid
                JOIN pg_class AS table_relation
                  ON table_relation.oid = index_metadata.indrelid
                WHERE namespace.nspname = 'public'
                  AND index_relation.relname = %s;
                """,
                (index_name,),
            )
            identity = cur.fetchone()
            if identity != (
                expected_index_oid,
                expected_table_oid,
            ):
                raise RuntimeError(
                    "Rollback target no longer matches the index/table "
                    "catalog identity created by this operation."
                )
            require_operation("DROP_INDEX")
            cur.execute(statement)

def compare_query_results(
    original_query: str,
    rewritten_query: str,
) -> dict:
    require_operation("COMPARE_QUERY_RESULTS")

    original = ensure_read_only_query(
        original_query
    )

    rewritten = ensure_read_only_query(
        rewritten_query
    )

    comparison_sql = f"""
    WITH original_result AS (
        {original}
    ),
    rewritten_result AS (
        {rewritten}
    ),
    only_original AS (
        SELECT *
        FROM original_result

        EXCEPT ALL

        SELECT *
        FROM rewritten_result
    ),
    only_rewritten AS (
        SELECT *
        FROM rewritten_result

        EXCEPT ALL

        SELECT *
        FROM original_result
    )
    SELECT
        (
            SELECT COUNT(*)
            FROM original_result
        ) AS original_count,

        (
            SELECT COUNT(*)
            FROM rewritten_result
        ) AS rewritten_count,

        (
            SELECT COUNT(*)
            FROM only_original
        ) AS only_original_count,

        (
            SELECT COUNT(*)
            FROM only_rewritten
        ) AS only_rewritten_count;
    """

    with readonly_connection() as conn:

        with conn.cursor() as cur:

            require_operation("COMPARE_QUERY_RESULTS")
            cur.execute(
                comparison_sql,
                prepare=True,
            )

            row = cur.fetchone()

    original_count = row[0]
    rewritten_count = row[1]
    only_original_count = row[2]
    only_rewritten_count = row[3]

    equivalent = (
        original_count
        == rewritten_count
        and only_original_count == 0
        and only_rewritten_count == 0
    )

    return {
        "equivalent": equivalent,

        "comparison_scope": (
            "CURRENT_SNAPSHOT_UNORDERED_ROW_MULTISET"
        ),

        "semantic_equivalence_proven": False,

        "original_count": (
            original_count
        ),

        "rewritten_count": (
            rewritten_count
        ),

        "only_original_count": (
            only_original_count
        ),

        "only_rewritten_count": (
            only_rewritten_count
        ),
    }

def benchmark_query(
    query: str,
    warmups: int = 2,
    runs: int = 5,
) -> dict:
    require_operation("BENCHMARK")
    if (
        type(warmups) is not int or type(runs) is not int
        or warmups < 0 or runs < 1 or warmups + runs > 20
    ):
        raise ValueError("Benchmark requires integer warmups >= 0, runs >= 1, and at most 20 total executions.")

    for _ in range(warmups):
        require_operation("BENCHMARK")
        get_query_plan(query)

    times = []

    for _ in range(runs):
        require_operation("BENCHMARK")
        plan = get_query_plan(query)

        times.append(
            plan["Execution Time"]
        )

    return {
        "samples": times,
        "median_ms": statistics.median(times),
        "min_ms": min(times),
        "max_ms": max(times),
    }
