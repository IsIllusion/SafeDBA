import json
import sys
import uuid

from agent import (
    review_execution_result,
    run_agent,
)

from executor import (
    execute_action_proposal,
)

from db_tools import (
    verify_runtime_security,
)

from incident_workflow import (
    create_lock_incident,
    incident_public_view,
    run_lock_incident,
)

from workflow_store import (
    SQLiteIncidentStore,
)

def print_json(
    value,
) -> None:

    print(
        json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


def get_agent_memory_store():
    from agent_memory import SQLiteAgentMemory
    from config import AGENT_STATE_DB_PATH

    return SQLiteAgentMemory(
        AGENT_STATE_DB_PATH
    )


def get_experience_store():
    from experience_store import SQLiteExperienceStore
    from config import EXPERIENCE_DB_PATH

    return SQLiteExperienceStore(
        EXPERIENCE_DB_PATH
    )


def run_incident(
    user_message: str,
    *,
    mode: str = "diagnose",
    lock_workflow: bool = False,
    thread_id: str | None = None,
    session_id: str | None = None,
) -> dict:

    # ----------------------------------------
    # 1. Agent diagnosis
    # ----------------------------------------

    run_kwargs = {"mode": mode}
    if session_id is not None:
        run_kwargs.update({
            "thread_id": thread_id or "safedba-cli",
            "session_id": session_id,
        })
    result = run_agent(
        user_message,
        **run_kwargs,
    )

    print()
    print(
        "=== SafeDBA Diagnosis ==="
    )
    print()

    answer = result.get(
        "answer",
        "",
    )

    print(
        answer
    )

    # ----------------------------------------
    # 2. Tool trace
    # ----------------------------------------

    tool_trace = result.get(
        "tool_trace",
        [],
    )

    print()
    print(
        "=== Evidence Tool Trace ==="
    )

    if tool_trace:

        print_json(
            tool_trace
        )

    else:

        print(
            "No database tools were called."
        )

    # ----------------------------------------
    # 3. Action proposals
    # ----------------------------------------

    proposals = result.get(
        "proposals",
        [],
    )

    if proposals:

        print()
        print(
            "=== Agent Proposal ==="
        )

        print_json(
            proposals
        )

    if result.get("status") != "completed":

        print()
        print(
            "The Agent run did not complete successfully."
        )
        print(
            "Controlled execution routing is disabled for "
            "partial or failed runs."
        )
        print_json({
            "status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
        })
        return result

    # An explicit lock-resolution request is a strict workflow boundary.
    # It must never fall through to the ordinary single-action executor when
    # the Agent returns no proposal, malformed proposal output, or any action
    # other than TERMINATE_BACKEND.
    if lock_workflow:
        if not isinstance(proposals, list) or not proposals:
            print()
            print(
                "Explicit lock resolution failed closed: the Agent "
                "did not return a TERMINATE_BACKEND proposal."
            )
            print(
                "No IncidentWorkflow was created and no database action "
                "was routed to the ordinary executor."
            )
            return result

        if not all(
            isinstance(proposal, dict)
            and proposal.get("type") == "TERMINATE_BACKEND"
            for proposal in proposals
        ):
            print()
            print(
                "Explicit lock resolution failed closed: every proposal "
                "must be TERMINATE_BACKEND."
            )
            print(
                "Mixed, malformed, and non-lock proposals are not executed "
                "by this workflow or by the ordinary executor."
            )
            return result

        run_termination_incident(
            proposals=proposals,
            user_message=user_message,
            expand_all_actionable=True,
        )
        return result

    if not proposals:

        print()
        print(
            "No controlled database "
            "action proposed."
        )

        return result

    if not isinstance(proposals, list):
        print()
        print(
            "Controlled execution failed closed because the Agent "
            "proposal payload is not a list."
        )
        return result

    termination_only = all(
        isinstance(proposal, dict)
        and proposal.get("type") == "TERMINATE_BACKEND"
        for proposal in proposals
    )

    if (
        len(proposals) > 1
        and termination_only
    ):
        run_termination_incident(
            proposals=proposals,
            user_message=user_message,
            expand_all_actionable=False,
        )
        return result

    # ----------------------------------------
    # 4. Fail closed on unsupported batches
    # ----------------------------------------

    if len(proposals) > 1:

        print()
        print(
            "SafeDBA produced multiple "
            "action proposals."
        )

        print(
            "Automatic execution routing "
            "is paused because applying "
            "one action could make the "
            "remaining proposals stale."
        )

        print(
            "Only homogeneous TERMINATE_BACKEND batches are "
            "supported by IncidentWorkflow. Submit a more "
            "specific request for mixed or non-lock actions."
        )

        return result

    proposal = proposals[0]

    # ----------------------------------------
    # 5. Deterministic execution authority
    # ----------------------------------------

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

    print_json(
        execution_result
    )

    # ----------------------------------------
    # 6. Post-action Agent review
    # ----------------------------------------
    #
    # Do not maintain a status whitelist here.
    # The deterministic executor owns status
    # semantics. The review Agent receives the
    # actual executor result as evidence.

    print()
    print(
        "=== SafeDBA "
        "Post-Action Review ==="
    )
    print()

    try:

        review = (
            review_execution_result(
                proposal=proposal,
                execution_result=(
                    execution_result
                ),
            )
        )

        print(
            review
        )

    except Exception as exc:

        # The deterministic execution result
        # is already authoritative.
        # Failure of the optional LLM review
        # must not alter execution status.

        print(
            "Post-action review "
            "was unavailable."
        )

        print(
            f"Review error: {exc}"
        )

        print(
            "The deterministic execution "
            "result above remains "
            "authoritative."
        )

    return result


def print_incident(
    incident: dict,
) -> None:
    print_json(
        incident_public_view(incident)
    )


def incident_approval_decider(
    incident: dict,
) -> dict:
    incident_id = incident["incident_id"]
    short_id = incident_id.split("-")[0]
    action_count = len(incident["actions"])

    print()
    print(
        "=== IncidentWorkflow Batch Approval ==="
    )
    print(
        f"Incident: {incident_id}"
    )
    print(
        f"Risk: HIGH"
    )
    print(
        f"Unique blocker backends: {action_count}"
    )
    print(
        "Actions will run serially. Every action is checkpointed, "
        "revalidated against a fresh lock graph, and followed by "
        "whole-target verification. New blocker identities are not "
        "included in this approval."
    )

    for action in incident["actions"]:
        target = action["target"]
        print(
            "- blocker PID "
            f"{target['blocker_pid']} "
            f"(backend_start={target['blocker_backend_start']}, "
            f"xact_start={target['blocker_xact_start']})"
        )
        for waiter in action.get("approved_waiters", []):
            print(
                "  approved waiter PID "
                f"{waiter['blocked_pid']} "
                f"(backend_start={waiter['blocked_backend_start']}, "
                f"xact_start={waiter['blocked_xact_start']})"
            )

    print()
    expected = f"approve {short_id}"
    answer = input(
        f"Type '{expected}' to approve this exact batch: "
    )
    return {
        "approved": answer.strip().lower() == expected,
        "actor": "interactive-user",
    }


def run_termination_incident(
    *,
    proposals: list[dict],
    user_message: str,
    expand_all_actionable: bool,
) -> dict:
    store = SQLiteIncidentStore()
    incident = create_lock_incident(
        proposals=proposals,
        user_request=user_message,
        expand_all_actionable=expand_all_actionable,
        store=store,
    )

    print()
    print(
        "=== Durable IncidentWorkflow Plan ==="
    )
    print_incident(incident)

    result = run_lock_incident(
        incident["incident_id"],
        store=store,
        approval_decider=incident_approval_decider,
    )

    print()
    print(
        "=== IncidentWorkflow Result ==="
    )
    print_incident(result)
    return result


def resume_termination_incident(
    incident_id: str,
) -> dict:
    # A persisted approval is not permission to resume under a changed or
    # unsafe runtime role configuration. Re-attest every database identity
    # before loading or advancing durable workflow state.
    verify_runtime_security()

    print()
    print(
        "Runtime security attestation passed before resume."
    )

    store = SQLiteIncidentStore()
    existing = store.load_incident(
        incident_id
    )

    print()
    print(
        "=== Resumable IncidentWorkflow ==="
    )
    print_incident(existing)

    result = run_lock_incident(
        incident_id,
        store=store,
        approval_decider=incident_approval_decider,
    )

    print()
    print(
        "=== IncidentWorkflow Result ==="
    )
    print_incident(result)
    return result


def list_incidents() -> None:
    store = SQLiteIncidentStore()
    print_json(
        store.list_incidents()
    )


def interactive_mode() -> None:

    thread_id = "safedba-cli"
    session_id = str(uuid.uuid4())
    last_result: dict | None = None

    print()
    print(
        "========================================"
    )

    print(
        "SafeDBA - Agentic PostgreSQL Operations"
    )

    print(
        "========================================"
    )

    print()
    print(
        "Describe a PostgreSQL performance "
        "or operational problem."
    )

    print(
        "Type 'exit' or 'quit' to stop."
    )

    print(
        "Diagnosis is the default. Prefix a request with "
        "'/propose ' to explicitly allow action proposals."
    )

    print(
        "Use '/resolve-locks [context]' for one durable, "
        "exact-scope multi-blocker workflow; '/resume <id>' "
        "resumes it and '/incidents' lists recent workflows."
    )

    print(
        "Session memory is enabled. Use '/session', '/session new', "
        "'/session <id>', or '/forget'."
    )
    print(
        "Use '/feedback good|bad [note]' for evaluation feedback; "
        "'train-good|train-bad' explicitly also permits offline "
        "training-dataset export."
    )
    print(
        f"Current session: {session_id}"
    )

    while True:

        print()

        try:

            user_message = input(
                "SafeDBA> "
            ).strip()

        except EOFError:

            print()
            print(
                "SafeDBA stopped."
            )

            return

        except KeyboardInterrupt:

            print()
            print(
                "SafeDBA stopped."
            )

            return

        if user_message.lower() in {
            "exit",
            "quit",
            ":q",
        }:

            print(
                "SafeDBA stopped."
            )

            return

        if not user_message:
            continue

        try:

            normalized = user_message.lower()

            if normalized == "/session":
                print(
                    f"Current session: {session_id}"
                )
                continue

            if normalized.startswith("/session "):
                requested_session = user_message[
                    len("/session ") :
                ].strip()
                if requested_session.lower() == "new":
                    session_id = str(uuid.uuid4())
                elif requested_session:
                    session_id = requested_session
                else:
                    print("Provide a session ID or 'new'.")
                    continue
                last_result = None
                print(f"Current session: {session_id}")
                continue

            if normalized == "/forget":
                deleted = get_agent_memory_store().delete_session(
                    thread_id=thread_id,
                    session_id=session_id,
                )
                last_result = None
                print(
                    f"Deleted {deleted} memory records for "
                    f"session {session_id}."
                )
                continue

            if normalized.startswith("/feedback "):
                if not last_result or not last_result.get("run_id"):
                    print("No Agent run is available for feedback.")
                    continue
                payload = user_message[
                    len("/feedback ") :
                ].strip()
                parts = payload.split(maxsplit=1)
                label_token = parts[0].lower() if parts else ""
                note = (
                    parts[1].strip()
                    if len(parts) > 1
                    else "Operator feedback from the SafeDBA CLI."
                )
                feedback_policy = {
                    "good": ("good", 5, ("evaluation",)),
                    "bad": ("bad", 1, ("evaluation",)),
                    "train-good": (
                        "good",
                        5,
                        ("evaluation", "training"),
                    ),
                    "train-bad": (
                        "bad",
                        1,
                        ("evaluation", "training"),
                    ),
                }
                if label_token not in feedback_policy:
                    print(
                        "Feedback label must be good, bad, "
                        "train-good, or train-bad."
                    )
                    continue
                label, rating, approved_uses = feedback_policy[
                    label_token
                ]
                feedback = get_experience_store().add_human_feedback(
                    feedback_id=str(uuid.uuid4()),
                    run_id=last_result["run_id"],
                    actor="cli_operator",
                    label=label,
                    rationale={"note": note},
                    rating=rating,
                    approved_uses=approved_uses,
                )
                print_json(feedback)
                continue

            if normalized == "/incidents":
                list_incidents()
                continue

            if normalized.startswith("/resume "):
                incident_id = user_message[
                    len("/resume ") :
                ].strip()
                if not incident_id:
                    print(
                        "Provide an incident ID after /resume."
                    )
                    continue
                resume_termination_incident(
                    incident_id
                )
                continue

            mode = "diagnose"
            lock_workflow = False

            if normalized == "/resolve-locks":
                mode = "propose"
                lock_workflow = True
                user_message = (
                    "Investigate and propose remediation for all "
                    "currently actionable lock blockers."
                )
            elif normalized.startswith(
                "/resolve-locks "
            ):
                mode = "propose"
                lock_workflow = True
                user_message = user_message[
                    len("/resolve-locks ") :
                ].strip()
                if not user_message:
                    user_message = (
                        "Investigate and propose remediation for all "
                        "currently actionable lock blockers."
                    )
            elif normalized.startswith(
                "/propose "
            ):
                mode = "propose"
                user_message = user_message[
                    len("/propose ") :
                ].strip()
                if not user_message:
                    print(
                        "Provide a request after /propose."
                    )
                    continue

            last_result = run_incident(
                user_message,
                mode=mode,
                lock_workflow=lock_workflow,
                thread_id=thread_id,
                session_id=session_id,
            )

        except KeyboardInterrupt:

            print()
            print(
                "Current operation interrupted."
            )

        except Exception as exc:

            print()
            print(
                "=== SafeDBA Error ==="
            )

            print(
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            print(
                "No successful database "
                "action should be inferred "
                "from this failed turn."
            )


def main() -> None:

    # One-shot mode:
    #
    # python src/main.py "Investigate ..."
    #
    # Useful for scripts, demos, and README
    # examples.

    if len(
        sys.argv
    ) > 1:

        arguments = list(sys.argv[1:])

        if arguments[0] == "--resume":
            if len(arguments) != 2:
                raise SystemExit(
                    "Usage: python src/main.py --resume <incident-id>"
                )
            resume_termination_incident(
                arguments[1]
            )
            return

        if arguments[0] == "--incidents":
            if len(arguments) != 1:
                raise SystemExit(
                    "--incidents does not accept additional arguments."
                )
            list_incidents()
            return

        session_id = None
        thread_id = "safedba-cli"
        if "--session" in arguments:
            session_index = arguments.index("--session")
            if session_index + 1 >= len(arguments):
                raise SystemExit(
                    "Usage: --session <session-id>"
                )
            session_id = arguments[session_index + 1]
            del arguments[
                session_index : session_index + 2
            ]
        if "--thread" in arguments:
            thread_index = arguments.index("--thread")
            if thread_index + 1 >= len(arguments):
                raise SystemExit(
                    "Usage: --thread <thread-id>"
                )
            thread_id = arguments[thread_index + 1]
            del arguments[
                thread_index : thread_index + 2
            ]

        mode = "diagnose"
        lock_workflow = False
        if arguments and arguments[0] == "--resolve-locks":
            mode = "propose"
            lock_workflow = True
            arguments = arguments[1:]
        if arguments and arguments[0] == "--propose":
            mode = "propose"
            arguments = arguments[1:]

        user_message = " ".join(arguments).strip()

        if not user_message and lock_workflow:
            user_message = (
                "Investigate and propose remediation for all "
                "currently actionable lock blockers."
            )
        elif not user_message:

            raise SystemExit(
                "Empty SafeDBA request."
            )

        run_incident(
            user_message,
            mode=mode,
            lock_workflow=lock_workflow,
            thread_id=thread_id,
            session_id=session_id,
        )

        return

    # Otherwise launch interactive REPL.

    interactive_mode()


if __name__ == "__main__":

    main()
