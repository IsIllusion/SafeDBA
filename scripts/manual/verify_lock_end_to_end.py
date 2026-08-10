import json

from agent import (
    review_execution_result,
    run_agent,
)

from executor import (
    execute_action_proposal,
)


question = """
Investigate the current PostgreSQL lock contention using real
database runtime evidence.

Identify the blocked session, blocker, wait event, relevant SQL,
and the most likely database-level root cause.

I also want remediation.

If the CURRENT database evidence shows that a PostgreSQL client
session is blocking another session through an open
idle-in-transaction transaction, and the evidence is strong
enough to justify evaluation of a controlled remediation action,
submit exactly one structured backend-termination proposal.

Do not claim that any backend has already been terminated.

Do not reuse or guess PIDs. Use the current runtime evidence.
"""


agent_result = run_agent(
    question
)


print()
print(
    "=== SafeDBA Diagnosis ==="
)
print()

print(
    agent_result["answer"]
)


print()
print(
    "=== Tool Trace ==="
)

print(
    json.dumps(
        agent_result["tool_trace"],
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)


print()
print(
    "=== Action Proposals ==="
)

print(
    json.dumps(
        agent_result["proposals"],
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)


terminate_proposals = [
    proposal
    for proposal in agent_result[
        "proposals"
    ]
    if proposal.get("type")
    == "TERMINATE_BACKEND"
]


if not terminate_proposals:

    print()
    print(
        "No TERMINATE_BACKEND "
        "proposal was produced."
    )

    raise SystemExit(0)


if len(terminate_proposals) > 1:

    print()
    print(
        "More than one termination "
        "proposal was produced. "
        "Stopping for manual review."
    )

    raise SystemExit(1)


proposal = terminate_proposals[0]


print()
print(
    "=== Controlled Execution ==="
)


execution_result = (
    execute_action_proposal(
        proposal
    )
)


print()
print(
    "=== Deterministic "
    "Execution Result ==="
)

print(
    json.dumps(
        execution_result,
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)


print()
print(
    "=== Agent Review ==="
)


review = (
    review_execution_result(
        proposal,
        execution_result,
    )
)


print(
    review
)