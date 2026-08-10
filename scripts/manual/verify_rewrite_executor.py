from actions import (
    build_query_rewrite_proposal,
)

from executor import (
    execute_action_proposal,
)

import json


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
          + INTERVAL '1 day';
"""


proposal = (
    build_query_rewrite_proposal(
        original_query=original_query,
        rewritten_query=rewritten_query,
        reason=(
            "Test non-sargable "
            "predicate rewrite."
        ),
        confidence=0.9,
    )
)


result = (
    execute_action_proposal(
        proposal
    )
)


print()
print(
    "=== Rewrite Execution Result ==="
)

print(
    json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
    )
)