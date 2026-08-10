import json

from actions import (
    build_terminate_backend_proposal,
    validate_action_proposal,
)

from db_tools import (
    get_lock_waits,
)


lock_waits = get_lock_waits()


print()
print(
    "=== Current Lock Waits ==="
)

print(
    json.dumps(
        lock_waits,
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)


if not lock_waits:

    print()
    print(
        "No active blocking "
        "relationship found."
    )

    raise SystemExit(0)


# For this isolated test, use the first
# currently observed blocking relationship.
#
# Later the Agent will choose the relevant
# relationship based on the incident being
# diagnosed.

lock_wait = lock_waits[0]


blocked_pid = lock_wait[
    "blocked_pid"
]

blocker_pid = lock_wait[
    "blocker_pid"
]


proposal = (
    build_terminate_backend_proposal(
        blocked_pid=blocked_pid,
        blocker_pid=blocker_pid,
        reason=(
            f"PID {blocked_pid} is "
            f"currently waiting on a "
            f"PostgreSQL Lock event and "
            f"PID {blocker_pid} is "
            f"currently reported as its "
            f"blocking backend."
        ),
        confidence=0.95,
    )
)


print()
print(
    "=== Terminate Backend Proposal ==="
)

print(
    json.dumps(
        proposal,
        indent=2,
        ensure_ascii=False,
    )
)


validation = (
    validate_action_proposal(
        proposal
    )
)


print()
print(
    "=== Proposal Validation ==="
)

print(
    json.dumps(
        validation,
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)