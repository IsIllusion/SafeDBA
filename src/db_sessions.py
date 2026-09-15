"""Active sessions, transaction sessions and identity-bound lock snapshots."""

import hashlib
import json

from db_observation_context import ObservationDependencies


def get_active_sessions(*, context: ObservationDependencies) -> list[dict]:
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

    with context.connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (context.max_rows,),
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
            "query": context.redact_query(row[6]),
            "query_start": (row[7].isoformat() if row[7] is not None else None),
            "transaction_start": (row[8].isoformat() if row[8] is not None else None),
            "query_age_seconds": (float(row[9]) if row[9] is not None else None),
            "transaction_age_seconds": (
                float(row[10]) if row[10] is not None else None
            ),
            "blocker_count": (row[11]),
        }
        for row in rows
    ]


def get_transaction_sessions(*, context: ObservationDependencies) -> list[dict]:
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

    with context.connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (
                    context.long_transaction_seconds,
                    context.max_rows,
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
            "query": context.redact_query(row[6]),
            "query_start": (row[7].isoformat() if row[7] is not None else None),
            "transaction_start": (row[8].isoformat() if row[8] is not None else None),
            "transaction_age_seconds": (float(row[9]) if row[9] is not None else None),
            "query_age_seconds": (float(row[10]) if row[10] is not None else None),
            "blocking_pids": (row[11]),
        }
        for row in rows
    ]


def get_lock_graph_snapshot(*, context: ObservationDependencies) -> dict:

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

    with context.connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                query,
                (context.max_rows + 1,),
            )

            rows = cur.fetchall()

    truncated = len(rows) > context.max_rows
    rows = rows[: context.max_rows]

    results = []

    for row in rows:

        results.append(
            {
                "blocked_pid": (row[0]),
                "blocked_user": (row[1]),
                "database_name": (row[2]),
                "blocked_application": (row[3]),
                "blocked_state": (row[4]),
                "blocked_wait_event_type": (row[5]),
                "blocked_wait_event": (row[6]),
                "blocked_query": (context.redact_query(row[7])),
                "blocked_query_start": (
                    row[8].isoformat() if row[8] is not None else None
                ),
                "blocked_xact_start": (
                    row[9].isoformat() if row[9] is not None else None
                ),
                "blocked_backend_start": (
                    row[10].isoformat() if row[10] is not None else None
                ),
                "blocked_query_duration_seconds": (
                    float(row[11]) if row[11] is not None else None
                ),
                "blocked_transaction_age_seconds": (
                    float(row[12]) if row[12] is not None else None
                ),
                "blocker_pid": (row[13]),
                "blocker_user": (row[14]),
                "blocker_application": (row[15]),
                "blocker_database_name": (row[16]),
                "blocker_backend_type": (row[17]),
                "blocker_state": (row[18]),
                "blocker_wait_event_type": (row[19]),
                "blocker_wait_event": (row[20]),
                "blocker_query": (context.redact_query(row[21])),
                "blocker_query_start": (
                    row[22].isoformat() if row[22] is not None else None
                ),
                "blocker_xact_start": (
                    row[23].isoformat() if row[23] is not None else None
                ),
                "blocker_transaction_age_seconds": (
                    float(row[25]) if row[25] is not None else None
                ),
                "blocker_backend_start": (
                    row[24].isoformat() if row[24] is not None else None
                ),
                "waiting_locks": (row[26] if row[26] is not None else []),
            }
        )

    captured_at = context.now().isoformat()
    digest_rows = [
        {
            "database_name": row.get("database_name"),
            "blocked_pid": row.get("blocked_pid"),
            "blocked_backend_start": row.get("blocked_backend_start"),
            "blocked_xact_start": row.get("blocked_xact_start"),
            "blocker_pid": row.get("blocker_pid"),
            "blocker_backend_start": row.get("blocker_backend_start"),
            "blocker_xact_start": row.get("blocker_xact_start"),
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
            results[0].get("database_name") if results else context.database_name
        ),
        "rows": results,
        "row_count": len(results),
        "truncated": truncated,
        "snapshot_digest": snapshot_digest,
    }
