import statistics

import psycopg
from psycopg import sql

from config import (
    DB_CONFIG,
    HEALTH_LONG_QUERY_SECONDS,
    HEALTH_LONG_TRANSACTION_SECONDS,
)

def ensure_read_only_query(
    query: str,
) -> str:

    normalized = query.strip()

    # Allow one trailing semicolon.
    if normalized.endswith(";"):
        normalized = (
            normalized[:-1].strip()
        )

    # Reject multiple SQL statements.
    if ";" in normalized:
        raise ValueError(
            "Multiple SQL statements "
            "are not allowed."
        )

    if not normalized.upper().startswith(
        "SELECT"
    ):
        raise ValueError(
            "SafeDBA currently allows "
            "only read-only SELECT queries."
        )

    return normalized


def get_query_plan(query: str) -> dict:
    query = ensure_read_only_query(
                query
                )

    explain_sql = f"""
    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
    {query}
    """

    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(explain_sql)
            result = cur.fetchone()

    return result[0][0]


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

    with psycopg.connect(**DB_CONFIG) as conn:
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

    with psycopg.connect(**DB_CONFIG) as conn:
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

    with psycopg.connect(**DB_CONFIG) as conn:
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

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

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

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

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
        pid ASC;
    """

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

        with conn.cursor() as cur:

            cur.execute(
                query
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
            "query": row[6],

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
        pid ASC;
    """

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (
                    HEALTH_LONG_TRANSACTION_SECONDS,
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
            "query": row[6],

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

def get_lock_waits() -> list[dict]:

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

        blocker.state AS blocker_state,
        blocker.wait_event_type AS blocker_wait_event_type,
        blocker.wait_event AS blocker_wait_event,

        blocker.query AS blocker_query,
        blocker.query_start AS blocker_query_start,
        blocker.xact_start AS blocker_xact_start,

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

    ORDER BY
        blocked.query_start ASC,
        blocker.pid ASC;
    """

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

        with conn.cursor() as cur:

            cur.execute(query)

            rows = cur.fetchall()

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
                row[7]
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

            "blocked_query_duration_seconds": (
                float(row[10])
                if row[10] is not None
                else None
            ),

            "blocked_transaction_age_seconds": (
                float(row[11])
                if row[11] is not None
                else None
            ),

            "blocker_pid": (
                row[12]
            ),

            "blocker_user": (
                row[13]
            ),

            "blocker_application": (
                row[14]
            ),

            "blocker_state": (
                row[15]
            ),

            "blocker_wait_event_type": (
                row[16]
            ),

            "blocker_wait_event": (
                row[17]
            ),

            "blocker_query": (
                row[18]
            ),

            "blocker_query_start": (
                row[19].isoformat()
                if row[19] is not None
                else None
            ),

            "blocker_xact_start": (
                row[20].isoformat()
                if row[20] is not None
                else None
            ),

            "blocker_transaction_age_seconds": (
                float(row[21])
                if row[21] is not None
                else None
            ),

            "waiting_locks": (
                row[22]
                if row[22] is not None
                else []
            ),
        })

    return results


def terminate_blocking_backend(
    blocked_pid: int,
    blocker_pid: int,
    timeout_ms: int = 5000,
) -> dict:

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

        blocker_pid,
        blocker_database,
        blocker_user,
        blocker_backend_type,
        blocker_state,
        blocker_query,
        blocker_xact_start,

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

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

        with conn.cursor() as cur:

            cur.execute(
                statement,
                (
                    blocker_pid,
                    blocked_pid,
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
            row[7]
        ),

        "blocker_pid": row[8],

        "blocker_database": row[9],

        "blocker_user": row[10],

        "blocker_backend_type": (
            row[11]
        ),

        "blocker_state": row[12],

        "blocker_query": row[13],

        "blocker_xact_start": (
            row[14].isoformat()
            if row[14] is not None
            else None
        ),

        "still_blocking": (
            row[15]
        ),

        "final_validation_passed": (
            row[16]
        ),

        "terminated": (
            row[17]
        ),
    }


def create_index(
    table: str,
    column: str,
    index_name: str,
) -> None:

    statement = sql.SQL(
        "CREATE INDEX {} ON {} ({})"
    ).format(
        sql.Identifier(index_name),
        sql.Identifier(table),
        sql.Identifier(column),
    )

    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(statement)



def analyze_table(
    table_name: str,
    columns: list[str] | None = None,
) -> None:

    columns = columns or []

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

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
                        table_name
                    ),
                    column_sql,
                )

            else:

                statement = sql.SQL(
                    "ANALYZE {}"
                ).format(
                    sql.Identifier(
                        table_name
                    )
                )

            cur.execute(
                statement
            )



def drop_index(index_name: str) -> None:
    statement = sql.SQL(
        "DROP INDEX {}"
    ).format(
        sql.Identifier(index_name)
    )

    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(statement)

def compare_query_results(
    original_query: str,
    rewritten_query: str,
) -> dict:

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

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

        with conn.cursor() as cur:

            cur.execute(
                comparison_sql
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

    for _ in range(warmups):
        get_query_plan(query)

    times = []

    for _ in range(runs):
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