import json

from actions import (
    build_terminate_backend_proposal,
)

from db_tools import (
    get_lock_waits,
)

from executor import (
    execute_action_proposal,
)


lock_waits = get_lock_waits()


if not lock_waits:

    print(
        "No active blocking "
        "relationship found."
    )

    raise SystemExit(0)


# Test-only behavior:
# choose the first idle-in-transaction
# blocker automatically.

lock_wait = next(
    (
        item
        for item in lock_waits

        if item.get(
            "blocker_state"
        )
        in {
            "idle in transaction",
            "idle in transaction (aborted)",
        }
    ),
    None,
)


if lock_wait is None:

    print(
        "No idle-in-transaction "
        "blocker found."
    )

    raise SystemExit(0)


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

        blocker_backend_start=lock_wait[
            "blocker_backend_start"
        ],

        blocker_xact_start=lock_wait[
            "blocker_xact_start"
        ],

        reason=(
            f"PID {blocked_pid} is "
            f"currently waiting on a "
            f"PostgreSQL Lock event, "
            f"and PID {blocker_pid} is "
            f"its current "
            f"idle-in-transaction "
            f"blocking backend."
        ),

        confidence=0.95,
    )
)


print()
print(
    "=== Proposal ==="
)

print(
    json.dumps(
        proposal,
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)


result = (
    execute_action_proposal(
        proposal
    )
)


print()
print(
    "=== Execution Result ==="
)

print(
    json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)
