from db_tools import (
    compare_query_results,
)


original_query = """
SELECT *
FROM orders
WHERE DATE(created_at)
    = DATE '2026-05-18';
"""

rewritten_query = """
SELECT *
FROM orders
WHERE created_at
        >= DATE '2026-05-18'
  AND created_at
        < DATE '2026-05-18'
          + INTERVAL '12 hours';
"""

result = compare_query_results(
    original_query,
    rewritten_query,
)


print(result)