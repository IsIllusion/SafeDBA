import json

from actions import (
    build_analyze_table_proposal,
)

from executor import (
    execute_action_proposal,
)


query = """
SELECT *
FROM cardinality_test
WHERE status = 'hot';
"""


proposal = (
    build_analyze_table_proposal(
        query=query,
        table="cardinality_test",
        columns=[
            "status"
        ],
        reason=(
            "Severe cardinality under-estimate "
            "with stale status statistics."
        ),
        confidence=0.9,
    )
)


print()
print(
    "=== Analyze Proposal ==="
)

print(
    json.dumps(
        proposal,
        indent=2,
        ensure_ascii=False,
    )
)


result = (
    execute_action_proposal(
        proposal
    )
)


print()
print(
    "=== Analyze Execution Result ==="
)

print(
    json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
    )
)