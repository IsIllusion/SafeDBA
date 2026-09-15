"""Catalog and column-statistics observations; no deployment imports."""

from db_observation_context import ObservationDependencies


def get_indexes(table_name: str, *, context: ObservationDependencies) -> list[dict]:
    query = """
    SELECT
        indexname,
        indexdef
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = %s
    ORDER BY indexname;
    """

    with context.connect() as conn:
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
    table_name: str, *, context: ObservationDependencies
) -> list[str]:

    query = """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = %s
    ORDER BY ordinal_position;
    """

    with context.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (table_name,),
            )

            rows = cur.fetchall()

    return [row[0] for row in rows]


def get_column_info(
    table_name: str, column_name: str, *, context: ObservationDependencies
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

    with context.connect() as conn:
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
        "is_nullable": (row[3] == "YES"),
    }


def get_column_stats(
    table_name: str, column_name: str, *, context: ObservationDependencies
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

    with context.connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                stats_query,
                (
                    table_name,
                    column_name,
                ),
            )

            column_row = cur.fetchone()

            cur.execute(
                table_stats_query,
                (table_name,),
            )

            table_row = cur.fetchone()

    if column_row is None and table_row is None:
        return None

    result = {
        "table_name": table_name,
        "column_name": column_name,
    }

    if column_row is not None:

        result.update(
            {
                "null_frac": (column_row[1]),
                "n_distinct": (column_row[2]),
                "most_common_vals": (column_row[3]),
                "most_common_freqs": (column_row[4]),
                "histogram_bounds": (column_row[5]),
            }
        )

    if table_row is not None:

        result.update(
            {
                "n_live_tup": (table_row[0]),
                "n_mod_since_analyze": (table_row[1]),
                "last_analyze": (
                    table_row[2].isoformat() if table_row[2] is not None else None
                ),
                "last_autoanalyze": (
                    table_row[3].isoformat() if table_row[3] is not None else None
                ),
            }
        )

    return result
