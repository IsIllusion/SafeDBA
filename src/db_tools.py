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


def get_indexes(table_name: str) -> list[dict]:
    query = """
    SELECT
        indexname,
        indexdef
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = %s
    ORDER BY indexname;
    """

    with readonly_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (table_name,),
            )

            rows = cur.fetchall()

    return [
        {
            "index_name": row[0],
            "index_definition": row[1],
        }
        for row in rows
    ]


def get_table_columns(
    table_name: str,
) -> list[str]:

    query = """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = %s
    ORDER BY ordinal_position;
    """

    with readonly_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (table_name,),
            )

            rows = cur.fetchall()

    return [
        row[0]
        for row in rows
    ]

def get_column_info(
    table_name: str,
    column_name: str,
) -> dict | None:

    query = """
    SELECT
        column_name,
        data_type,
        udt_name,
        is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = %s
      AND column_name = %s;
    """

    with readonly_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (
                    table_name,
                    column_name,
                ),
            )

            row = cur.fetchone()

    if row is None:
        return None

    return {
        "column_name": row[0],
        "data_type": row[1],
        "udt_name": row[2],
        "is_nullable": (
            row[3] == "YES"
        ),
    }

def get_column_stats(
    table_name: str,
    column_name: str,
) -> dict | None:

    stats_query = """
    SELECT
        s.attname,
        s.null_frac,
        s.n_distinct,
        s.most_common_vals::text,
        s.most_common_freqs,
        s.histogram_bounds::text
    FROM pg_stats AS s
    WHERE s.schemaname = 'public'
      AND s.tablename = %s
      AND s.attname = %s;
    """

    table_stats_query = """
    SELECT
        n_live_tup,
        n_mod_since_analyze,
        last_analyze,
        last_autoanalyze
    FROM pg_stat_user_tables
    WHERE schemaname = 'public'
      AND relname = %s;
    """

    with readonly_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                stats_query,
                (
                    table_name,
                    column_name,
                ),
            )

            column_row = (
                cur.fetchone()
            )

            cur.execute(
                table_stats_query,
                (
                    table_name,
                ),
            )

            table_row = (
                cur.fetchone()
            )

    if (
        column_row is None
        and table_row is None
    ):
        return None

    result = {
        "table_name": table_name,
        "column_name": column_name,
    }

    if column_row is not None:

        result.update({
            "null_frac": (
                column_row[1]
            ),
            "n_distinct": (
                column_row[2]
            ),
            "most_common_vals": (
                column_row[3]
            ),
            "most_common_freqs": (
                column_row[4]
            ),
            "histogram_bounds": (
                column_row[5]
            ),
        })

    if table_row is not None:

        result.update({
            "n_live_tup": (
                table_row[0]
            ),

            "n_mod_since_analyze": (
                table_row[1]
            ),

            "last_analyze": (
                table_row[2].isoformat()
                if table_row[2] is not None
                else None
            ),

            "last_autoanalyze": (
                table_row[3].isoformat()
                if table_row[3] is not None
                else None
            ),
        })

    return result

def get_database_health() -> dict:
    """
    Capture a lightweight runtime health
    snapshot for the current PostgreSQL
    database.

    This is a triage tool, not a complete
    database health assessment.
    """

    query = """
    WITH activity AS (
        SELECT
            pid,
            usename,
            application_name,
            state,
            wait_event_type,
            wait_event,
            query_start,
            xact_start,

            CASE
                WHEN query_start IS NOT NULL
                THEN EXTRACT(
                    EPOCH FROM (
                        clock_timestamp()
                        - query_start
                    )
                )
                ELSE NULL
            END AS query_age_seconds,

            CASE
                WHEN xact_start IS NOT NULL
                THEN EXTRACT(
                    EPOCH FROM (
                        clock_timestamp()
                        - xact_start
                    )
                )
                ELSE NULL
            END AS transaction_age_seconds,

            cardinality(
                pg_blocking_pids(
                    pid
                )
            ) AS blocker_count

        FROM pg_stat_activity

        WHERE datname = current_database()

          AND pid <> pg_backend_pid()

          AND backend_type
              = 'client backend'
    )

    SELECT
        current_database(),

        COUNT(*),

        COUNT(*) FILTER (
            WHERE state = 'active'
        ),

        COUNT(*) FILTER (
            WHERE blocker_count > 0
        ),

        COUNT(*) FILTER (
            WHERE state IN (
                'idle in transaction',
                'idle in transaction (aborted)'
            )
        ),

        COUNT(*) FILTER (
            WHERE state = 'active'
              AND query_age_seconds >= %s
        ),

        COUNT(*) FILTER (
            WHERE xact_start IS NOT NULL
              AND transaction_age_seconds >= %s
        ),

        MAX(
            query_age_seconds
        ) FILTER (
            WHERE state = 'active'
        ),

        MAX(
            transaction_age_seconds
        ) FILTER (
            WHERE xact_start IS NOT NULL
        )

    FROM activity;
    """

    with readonly_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (
                    HEALTH_LONG_QUERY_SECONDS,
                    HEALTH_LONG_TRANSACTION_SECONDS,
                ),
            )

            row = cur.fetchone()

    if row is None:

        raise RuntimeError(
            "Database health query "
            "returned no result."
        )

    return {
        "database": row[0],

        "total_client_sessions": (
            row[1]
        ),

        "active_sessions": (
            row[2]
        ),

        "blocked_sessions": (
            row[3]
        ),

        "idle_in_transaction_sessions": (
            row[4]
        ),

        "long_running_queries": (
            row[5]
        ),

        "long_running_transactions": (
            row[6]
        ),

        "oldest_active_query_seconds": (
            float(row[7])
            if row[7] is not None
            else None
        ),

        "oldest_transaction_seconds": (
            float(row[8])
            if row[8] is not None
            else None
        ),

        "thresholds": {
            "long_query_seconds": (
                HEALTH_LONG_QUERY_SECONDS
            ),

            "long_transaction_seconds": (
                HEALTH_LONG_TRANSACTION_SECONDS
            ),
        },
    }

def get_operational_snapshot() -> dict:
    """Collect broad, read-only PostgreSQL operational evidence.

    The snapshot combines low-cost observations so a general incident can be
    routed from one Agent tool call. It reports PostgreSQL-visible sizes, not
    filesystem capacity.
    """

    runtime_health = get_database_health()

    connection_query = """
    WITH connection_settings AS (
        SELECT
            current_setting('max_connections')::integer
                AS max_connections,
            current_setting(
                'superuser_reserved_connections'
            )::integer AS superuser_reserved_connections,
            COALESCE(
                NULLIF(
                    current_setting('reserved_connections', true),
                    ''
                ),
                '0'
            )::integer AS reserved_connections
    )
    SELECT
        current_database(),
        settings.max_connections,
        settings.superuser_reserved_connections,
        settings.reserved_connections,
        COUNT(*) FILTER (
            WHERE activity.backend_type = 'client backend'
        ),
        COUNT(*) FILTER (
            WHERE activity.backend_type = 'client backend'
              AND activity.datname = current_database()
        ),
        COUNT(*) FILTER (
            WHERE activity.backend_type = 'client backend'
              AND activity.datname = current_database()
              AND activity.state = 'active'
        ),
        COUNT(*) FILTER (
            WHERE activity.backend_type = 'client backend'
              AND activity.datname = current_database()
              AND activity.state IN (
                  'idle in transaction',
                  'idle in transaction (aborted)'
              )
        )
    FROM connection_settings AS settings
    CROSS JOIN pg_stat_activity AS activity
    GROUP BY
        settings.max_connections,
        settings.superuser_reserved_connections,
        settings.reserved_connections;
    """

    vacuum_query = """
    WITH settings AS (
        SELECT current_setting(
            'autovacuum_freeze_max_age'
        )::bigint AS freeze_max_age
    )
    SELECT
        namespace.nspname,
        relation.relname,
        stats.n_live_tup,
        stats.n_dead_tup,
        ROUND(
            100.0 * stats.n_dead_tup
            / GREATEST(
                stats.n_live_tup + stats.n_dead_tup,
                1
            ),
            3
        ) AS dead_tuple_ratio_pct,
        stats.last_vacuum,
        stats.last_autovacuum,
        stats.vacuum_count,
        stats.autovacuum_count,
        stats.last_analyze,
        stats.last_autoanalyze,
        age(relation.relfrozenxid)::bigint AS xid_age,
        settings.freeze_max_age,
        ROUND(
            100.0 * age(relation.relfrozenxid)::numeric
            / GREATEST(settings.freeze_max_age, 1),
            3
        ) AS freeze_age_pct
    FROM pg_stat_user_tables AS stats
    JOIN pg_class AS relation
      ON relation.oid = stats.relid
    JOIN pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    CROSS JOIN settings
    ORDER BY
        dead_tuple_ratio_pct DESC,
        xid_age DESC,
        namespace.nspname,
        relation.relname
    LIMIT %s;
    """

    primary_replication_query = """
    SELECT
        application_name,
        state,
        sync_state,
        CASE
            WHEN replay_lsn IS NOT NULL
            THEN pg_wal_lsn_diff(
                pg_current_wal_lsn(),
                replay_lsn
            )
            ELSE NULL
        END AS wal_bytes_behind,
        EXTRACT(EPOCH FROM write_lag),
        EXTRACT(EPOCH FROM flush_lag),
        EXTRACT(EPOCH FROM replay_lag),
        backend_start,
        reply_time
    FROM pg_stat_replication
    ORDER BY application_name, pid
    LIMIT %s;
    """

    standby_replication_query = """
    SELECT
        status,
        slot_name,
        written_lsn::text,
        flushed_lsn::text,
        latest_end_lsn::text,
        last_msg_send_time,
        last_msg_receipt_time,
        latest_end_time
    FROM pg_stat_wal_receiver
    ORDER BY pid
    LIMIT %s;
    """

    storage_summary_query = """
    SELECT
        pg_database_size(current_database()),
        stats.temp_files,
        stats.temp_bytes,
        stats.deadlocks,
        stats.stats_reset
    FROM pg_stat_database AS stats
    WHERE stats.datname = current_database();
    """

    largest_relations_query = """
    SELECT
        namespace.nspname,
        relation.relname,
        relation.relkind,
        pg_relation_size(relation.oid),
        pg_indexes_size(relation.oid),
        pg_total_relation_size(relation.oid)
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE relation.relkind IN ('r', 'm')
      AND namespace.nspname NOT IN (
          'pg_catalog',
          'information_schema'
      )
      AND namespace.nspname !~ '^pg_toast'
    ORDER BY
        pg_total_relation_size(relation.oid) DESC,
        namespace.nspname,
        relation.relname
    LIMIT %s;
    """

    observation_limit = min(DB_MAX_OBSERVATION_ROWS, 10)

    with readonly_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(connection_query)
            connection_row = cur.fetchone()
            if connection_row is None:
                raise RuntimeError(
                    "Connection capacity query returned no result."
                )

            cur.execute(vacuum_query, (observation_limit,))
            vacuum_rows = cur.fetchall()

            cur.execute("SELECT pg_is_in_recovery();")
            recovery_row = cur.fetchone()
            if recovery_row is None:
                raise RuntimeError(
                    "PostgreSQL recovery-state query returned no result."
                )
            in_recovery = bool(recovery_row[0])

            if in_recovery:
                cur.execute(
                    standby_replication_query,
                    (observation_limit,),
                )
            else:
                cur.execute(
                    primary_replication_query,
                    (observation_limit,),
                )
            replication_rows = cur.fetchall()

            cur.execute(storage_summary_query)
            storage_row = cur.fetchone()
            if storage_row is None:
                raise RuntimeError(
                    "Database storage query returned no result."
                )

            cur.execute(
                largest_relations_query,
                (observation_limit,),
            )
            relation_rows = cur.fetchall()

    max_connections = int(connection_row[1])
    superuser_reserved = int(connection_row[2])
    reserved = int(connection_row[3])
    client_connections = int(connection_row[4])
    regular_capacity = max(
        max_connections - superuser_reserved - reserved,
        0,
    )

    def timestamp(value):
        return value.isoformat() if value is not None else None

    connection_capacity = {
        "max_connections": max_connections,
        "superuser_reserved_connections": superuser_reserved,
        "reserved_connections": reserved,
        "regular_connection_capacity": regular_capacity,
        "current_client_connections": client_connections,
        "current_database_connections": int(connection_row[5]),
        "active_current_database_connections": int(connection_row[6]),
        "idle_in_transaction_connections": int(connection_row[7]),
        "max_connection_utilization_pct": round(
            100.0 * client_connections / max(max_connections, 1),
            3,
        ),
        "estimated_available_regular_slots": max(
            regular_capacity - client_connections,
            0,
        ),
    }

    vacuum_tables = [
        {
            "schema": row[0],
            "table": row[1],
            "estimated_live_tuples": int(row[2]),
            "estimated_dead_tuples": int(row[3]),
            "dead_tuple_ratio_pct": float(row[4]),
            "last_manual_vacuum": timestamp(row[5]),
            "last_autovacuum": timestamp(row[6]),
            "manual_vacuum_count": int(row[7]),
            "autovacuum_count": int(row[8]),
            "last_manual_analyze": timestamp(row[9]),
            "last_autoanalyze": timestamp(row[10]),
            "xid_age": int(row[11]),
            "autovacuum_freeze_max_age": int(row[12]),
            "freeze_age_pct": float(row[13]),
        }
        for row in vacuum_rows
    ]

    if in_recovery:
        replication = {
            "server_role": "standby",
            "receivers": [
                {
                    "status": row[0],
                    "slot_name": row[1],
                    "written_lsn": row[2],
                    "flushed_lsn": row[3],
                    "latest_end_lsn": row[4],
                    "last_message_sent_at": timestamp(row[5]),
                    "last_message_received_at": timestamp(row[6]),
                    "latest_wal_end_at": timestamp(row[7]),
                }
                for row in replication_rows
            ],
        }
    else:
        replication = {
            "server_role": "primary",
            "standbys": [
                {
                    "application": row[0],
                    "state": row[1],
                    "sync_state": row[2],
                    "wal_bytes_behind": (
                        int(row[3]) if row[3] is not None else None
                    ),
                    "write_lag_seconds": (
                        float(row[4]) if row[4] is not None else None
                    ),
                    "flush_lag_seconds": (
                        float(row[5]) if row[5] is not None else None
                    ),
                    "replay_lag_seconds": (
                        float(row[6]) if row[6] is not None else None
                    ),
                    "backend_started_at": timestamp(row[7]),
                    "last_reply_at": timestamp(row[8]),
                }
                for row in replication_rows
            ],
        }

    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "database": connection_row[0],
        "runtime_health": runtime_health,
        "connection_capacity": connection_capacity,
        "vacuum": {
            "ordering": "dead_tuple_ratio_then_xid_age",
            "table_limit": observation_limit,
            "tables": vacuum_tables,
        },
        "replication": replication,
        "storage_usage": {
            "database_bytes": int(storage_row[0]),
            "temporary_files_since_stats_reset": int(storage_row[1]),
            "temporary_bytes_since_stats_reset": int(storage_row[2]),
            "deadlocks_since_stats_reset": int(storage_row[3]),
            "statistics_reset_at": timestamp(storage_row[4]),
            "largest_relations": [
                {
                    "schema": row[0],
                    "relation": row[1],
                    "relation_kind": row[2],
                    "heap_bytes": int(row[3]),
                    "index_bytes": int(row[4]),
                    "total_bytes": int(row[5]),
                }
                for row in relation_rows
            ],
            "filesystem_free_space_available": False,
        },
        "limitations": [
            "Vacuum tuple counts are PostgreSQL statistics estimates.",
            "Replication lag fields may be null on idle or unavailable links.",
            "Primary replication rows cover directly connected standbys only.",
            "Replication lag is not a prediction of catch-up time.",
            "Database size does not reveal filesystem free space.",
            "All values are point-in-time observations.",
        ],
    }


def get_active_sessions() -> list[dict]:
    """
    Return current active PostgreSQL client
    sessions for the configured database.

    This is an observational triage tool.
    It does not cancel or terminate sessions.
    """

    query = """
    SELECT
        pid,
        usename,
        application_name,
        state,
        wait_event_type,
        wait_event,
        query,
        query_start,
        xact_start,

        CASE
            WHEN query_start IS NOT NULL
            THEN EXTRACT(
                EPOCH FROM (
                    clock_timestamp()
                    - query_start
                )
            )
            ELSE NULL
        END AS query_age_seconds,

        CASE
            WHEN xact_start IS NOT NULL
            THEN EXTRACT(
                EPOCH FROM (
                    clock_timestamp()
                    - xact_start
                )
            )
            ELSE NULL
        END AS transaction_age_seconds,

        cardinality(
            pg_blocking_pids(
                pid
            )
        ) AS blocker_count

    FROM pg_stat_activity

    WHERE datname = current_database()

      AND pid <> pg_backend_pid()

      AND backend_type = 'client backend'

      AND state = 'active'

    ORDER BY
        query_start ASC NULLS LAST,
        pid ASC
    LIMIT %s;
    """

    with readonly_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (DB_MAX_OBSERVATION_ROWS,),
            )

            rows = cur.fetchall()

    return [
        {
            "pid": row[0],
            "user": row[1],
            "application": row[2],
            "state": row[3],
            "wait_event_type": row[4],
            "wait_event": row[5],
            "query": truncate_observed_query(
                row[6]
            ),

            "query_start": (
                row[7].isoformat()
                if row[7] is not None
                else None
            ),

            "transaction_start": (
                row[8].isoformat()
                if row[8] is not None
                else None
            ),

            "query_age_seconds": (
                float(row[9])
                if row[9] is not None
                else None
            ),

            "transaction_age_seconds": (
                float(row[10])
                if row[10] is not None
                else None
            ),

            "blocker_count": (
                row[11]
            ),
        }

        for row in rows
    ]

def get_transaction_sessions() -> list[dict]:
    """
    Return client sessions with operationally
    relevant open transactions.

    Includes idle-in-transaction sessions and
    transactions exceeding the configured
    long-transaction threshold.

    This tool is read-only.
    """

    query = """
    SELECT
        pid,
        usename,
        application_name,
        state,
        wait_event_type,
        wait_event,
        query,
        query_start,
        xact_start,

        EXTRACT(
            EPOCH FROM (
                clock_timestamp()
                - xact_start
            )
        ) AS transaction_age_seconds,

        CASE
            WHEN query_start IS NOT NULL
            THEN EXTRACT(
                EPOCH FROM (
                    clock_timestamp()
                    - query_start
                )
            )
            ELSE NULL
        END AS query_age_seconds,

        pg_blocking_pids(
            pid
        ) AS blocking_pids

    FROM pg_stat_activity

    WHERE datname = current_database()

      AND pid <> pg_backend_pid()

      AND backend_type = 'client backend'

      AND xact_start IS NOT NULL

      AND (
          state IN (
              'idle in transaction',
              'idle in transaction (aborted)'
          )

          OR EXTRACT(
              EPOCH FROM (
                  clock_timestamp()
                  - xact_start
              )
          ) >= %s
      )

    ORDER BY
        xact_start ASC,
        pid ASC
    LIMIT %s;
    """

    with readonly_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (
                    HEALTH_LONG_TRANSACTION_SECONDS,
                    DB_MAX_OBSERVATION_ROWS,
                ),
            )

            rows = cur.fetchall()

    return [
        {
            "pid": row[0],
            "user": row[1],
            "application": row[2],
            "state": row[3],
            "wait_event_type": row[4],
            "wait_event": row[5],
            "query": truncate_observed_query(
                row[6]
            ),

            "query_start": (
                row[7].isoformat()
                if row[7] is not None
                else None
            ),

            "transaction_start": (
                row[8].isoformat()
                if row[8] is not None
                else None
            ),

            "transaction_age_seconds": (
                float(row[9])
                if row[9] is not None
                else None
            ),

            "query_age_seconds": (
                float(row[10])
                if row[10] is not None
                else None
            ),

            "blocking_pids": (
                row[11]
            ),
        }

        for row in rows
    ]

def get_lock_graph_snapshot() -> dict:

    query = """
    SELECT
        blocked.pid AS blocked_pid,
        blocked.usename AS blocked_user,
        blocked.datname AS database_name,
        blocked.application_name AS blocked_application,

        blocked.state AS blocked_state,
        blocked.wait_event_type AS blocked_wait_event_type,
        blocked.wait_event AS blocked_wait_event,

        blocked.query AS blocked_query,
        blocked.query_start AS blocked_query_start,
        blocked.xact_start AS blocked_xact_start,
        blocked.backend_start AS blocked_backend_start,

        EXTRACT(
            EPOCH FROM (
                clock_timestamp()
                - blocked.query_start
            )
        ) AS blocked_query_duration_seconds,

        CASE
            WHEN blocked.xact_start IS NOT NULL
            THEN EXTRACT(
                EPOCH FROM (
                    clock_timestamp()
                    - blocked.xact_start
                )
            )
            ELSE NULL
        END AS blocked_transaction_age_seconds,

        blocker.pid AS blocker_pid,
        blocker.usename AS blocker_user,
        blocker.application_name AS blocker_application,
        blocker.datname AS blocker_database_name,
        blocker.backend_type AS blocker_backend_type,

        blocker.state AS blocker_state,
        blocker.wait_event_type AS blocker_wait_event_type,
        blocker.wait_event AS blocker_wait_event,

        blocker.query AS blocker_query,
        blocker.query_start AS blocker_query_start,
        blocker.xact_start AS blocker_xact_start,

        blocker.backend_start AS blocker_backend_start,

        CASE
            WHEN blocker.xact_start IS NOT NULL
            THEN EXTRACT(
                EPOCH FROM (
                    clock_timestamp()
                    - blocker.xact_start
                )
            )
            ELSE NULL
        END AS blocker_transaction_age_seconds,

        waiting_locks.waiting_locks

    FROM pg_stat_activity AS blocked

    CROSS JOIN LATERAL
        unnest(
            pg_blocking_pids(
                blocked.pid
            )
        ) AS blocker_pid(pid)

    JOIN pg_stat_activity AS blocker
        ON blocker.pid = blocker_pid.pid

    LEFT JOIN LATERAL (
        SELECT
            jsonb_agg(
                jsonb_build_object(
                    'lock_type',
                    waiting.locktype,

                    'mode',
                    waiting.mode,

                    'relation',
                    CASE
                        WHEN waiting.relation
                            IS NOT NULL
                        THEN waiting.relation::regclass::text
                        ELSE NULL
                    END,

                    'page',
                    waiting.page,

                    'tuple',
                    waiting.tuple,

                    'transaction_id',
                    waiting.transactionid,

                    'virtual_transaction',
                    waiting.virtualxid
                )
            ) AS waiting_locks

        FROM pg_locks AS waiting

        WHERE waiting.pid = blocked.pid
          AND waiting.granted = false

    ) AS waiting_locks
        ON true

    WHERE blocked.pid <> pg_backend_pid()
      AND blocked.datname = current_database()
      AND blocker.datname = current_database()

    ORDER BY
        blocked.query_start ASC,
        blocker.pid ASC
    LIMIT %s;
    """

    with readonly_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (DB_MAX_OBSERVATION_ROWS + 1,),
            )

            rows = cur.fetchall()

    truncated = len(rows) > DB_MAX_OBSERVATION_ROWS
    rows = rows[:DB_MAX_OBSERVATION_ROWS]

    results = []

    for row in rows:

        results.append({
            "blocked_pid": (
                row[0]
            ),

            "blocked_user": (
                row[1]
            ),

            "database_name": (
                row[2]
            ),

            "blocked_application": (
                row[3]
            ),

            "blocked_state": (
                row[4]
            ),

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

            "blocked_query_start": (
                row[8].isoformat()
                if row[8] is not None
                else None
            ),

            "blocked_xact_start": (
                row[9].isoformat()
                if row[9] is not None
                else None
            ),

            "blocked_backend_start": (
                row[10].isoformat()
                if row[10] is not None
                else None
            ),

            "blocked_query_duration_seconds": (
                float(row[11])
                if row[11] is not None
                else None
            ),

            "blocked_transaction_age_seconds": (
                float(row[12])
                if row[12] is not None
                else None
            ),

            "blocker_pid": (
                row[13]
            ),

            "blocker_user": (
                row[14]
            ),

            "blocker_application": (
                row[15]
            ),

            "blocker_database_name": (
                row[16]
            ),

            "blocker_backend_type": (
                row[17]
            ),

            "blocker_state": (
                row[18]
            ),

            "blocker_wait_event_type": (
                row[19]
            ),

            "blocker_wait_event": (
                row[20]
            ),

            "blocker_query": (
                truncate_observed_query(
                    row[21]
                )
            ),

            "blocker_query_start": (
                row[22].isoformat()
                if row[22] is not None
                else None
            ),

            "blocker_xact_start": (
                row[23].isoformat()
                if row[23] is not None
                else None
            ),

            "blocker_transaction_age_seconds": (
                float(row[25])
                if row[25] is not None
                else None
            ),

            "blocker_backend_start": (
                row[24].isoformat()
                if row[24] is not None
                else None
            ),

            "waiting_locks": (
                row[26]
                if row[26] is not None
                else []
            ),
        })

    captured_at = datetime.now(
        timezone.utc
    ).isoformat()
    digest_rows = [
        {
            "database_name": row.get("database_name"),
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
        }
        for row in results
    ]
    snapshot_digest = hashlib.sha256(
        json.dumps(
            digest_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    return {
        "captured_at": captured_at,
        "database_name": (
            results[0].get("database_name")
            if results
            else DB_CONFIG.get("dbname")
        ),
        "rows": results,
        "row_count": len(results),
        "truncated": truncated,
        "snapshot_digest": snapshot_digest,
    }


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
