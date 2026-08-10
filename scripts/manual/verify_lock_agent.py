import json

from agent import run_agent


question = """
Investigate whether PostgreSQL currently has any lock
contention or blocked sessions.

Use real database runtime evidence.

Identify:

- which session is blocked
- which session is blocking it
- what the blocked session is waiting for
- the relevant SQL statements
- the most likely database-level root cause

Do not assume that the SQL statement itself is slow.
Do not claim that any backend was cancelled or terminated.
"""


result = run_agent(
    question
)


print()
print(
    "=== SafeDBA Lock RCA ==="
)
print()

print(
    result["answer"]
)


print()
print(
    "=== Action Proposals ==="
)

print(
    json.dumps(
        result["proposals"],
        indent=2,
        ensure_ascii=False,
    )
)


print()
print(
    "=== Tool Trace ==="
)

print(
    json.dumps(
        result["tool_trace"],
        indent=2,
        ensure_ascii=False,
    )
)