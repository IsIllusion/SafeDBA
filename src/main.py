import json
import sys

from agent import (
    review_execution_result,
    run_agent,
)

from executor import (
    execute_action_proposal,
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


def run_incident(
    user_message: str,
) -> None:

    # ----------------------------------------
    # 1. Agent diagnosis
    # ----------------------------------------

    result = run_agent(
        user_message
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

    if not proposals:

        print()
        print(
            "No controlled database "
            "action proposed."
        )

        return

    print()
    print(
        "=== Agent Proposal ==="
    )

    print_json(
        proposals
    )

    # ----------------------------------------
    # 4. Fail closed on multiple proposals
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
            "Submit a more specific request "
            "or evaluate one action in a "
            "fresh diagnostic turn."
        )

        return

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


def interactive_mode() -> None:

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

            run_incident(
                user_message
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

        user_message = (
            " ".join(
                sys.argv[1:]
            ).strip()
        )

        if not user_message:

            raise SystemExit(
                "Empty SafeDBA request."
            )

        run_incident(
            user_message
        )

        return

    # Otherwise launch interactive REPL.

    interactive_mode()


if __name__ == "__main__":

    main()