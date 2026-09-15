"""Health, capacity, VACUUM, replication and storage observations."""

from db_observation_context import ObservationDependencies


def get_database_health(*, context: ObservationDependencies) -> dict:
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

    with context.connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (
                    context.long_query_seconds,
                    context.long_transaction_seconds,
                ),
            )

            row = cur.fetchone()

    if row is None:

        raise RuntimeError("Database health query " "returned no result.")

    return {
        "database": row[0],
        "total_client_sessions": (row[1]),
        "active_sessions": (row[2]),
        "blocked_sessions": (row[3]),
        "idle_in_transaction_sessions": (row[4]),
        "long_running_queries": (row[5]),
        "long_running_transactions": (row[6]),
        "oldest_active_query_seconds": (float(row[7]) if row[7] is not None else None),
        "oldest_transaction_seconds": (float(row[8]) if row[8] is not None else None),
        "thresholds": {
            "long_query_seconds": (context.long_query_seconds),
            "long_transaction_seconds": (context.long_transaction_seconds),
        },
    }


def get_operational_snapshot(*, context: ObservationDependencies) -> dict:
    """Collect broad, read-only PostgreSQL operational evidence.

    The snapshot combines low-cost observations so a general incident can be
    routed from one Agent tool call. It reports PostgreSQL-visible sizes, not
    filesystem capacity.
    """

    runtime_health = context.health()

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

    observation_limit = min(context.max_rows, 10)

    with context.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(connection_query)
            connection_row = cur.fetchone()
            if connection_row is None:
                raise RuntimeError("Connection capacity query returned no result.")

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
                raise RuntimeError("Database storage query returned no result.")

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
                    "wal_bytes_behind": (int(row[3]) if row[3] is not None else None),
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
        "captured_at": context.now().isoformat(),
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
