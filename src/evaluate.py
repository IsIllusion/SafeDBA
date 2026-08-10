import json
from pathlib import Path
import re
import psycopg

from agent import run_agent
from config import DB_CONFIG

CASE_DIR = Path(
    "benchmarks/cases"
)
#Run repeatedly
RUNS_PER_CASE = 3

REPORT_DIR = Path(
    "benchmarks/results"
)

REPORT_PATH = (
    REPORT_DIR
    / "latest_evaluation.json"
)

flags=re.DOTALL

def evaluate_unsupported_claims(
    result: dict,
) -> dict:

    texts = [
        result.get(
            "answer",
            "",
        )
    ]

    for proposal in result.get(
        "proposals",
        [],
    ):

        reason = proposal.get(
            "reason"
        )

        if reason:
            texts.append(reason)

    combined = "\n".join(
        texts
    )

    # Remove common Markdown markers so that:
    #
    # **Semantic equivalence** is validated
    #
    # becomes:
    #
    # Semantic equivalence is validated

    normalized = re.sub(
        r"[`*_#>]",
        " ",
        combined.lower(),
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    # Split into small claim units.
    # This prevents a safe statement later in the answer
    # from affecting an earlier unsupported claim.

    segments = [
        segment.strip()
        for segment in re.split(
            r"[.!?;]+",
            normalized,
        )
        if segment.strip()
    ]

    semantic_claim_patterns = [
        r"\bsemantic equivalence\s+holds\b",

        (
            r"\bsemantic equivalence\s+"
            r"(?:is|was|has been)\s+"
            r"(?:validated|verified|confirmed|proven)\b"
        ),

        (
            r"\b(?:validated|verified|confirmed|proven)\s+"
            r"semantic equivalence\b"
        ),

        (
            r"\b(?:is|are|was|were)\s+"
            r"semantically equivalent\b"
        ),

        r"\bsemantically equivalent\b",
    ]

    benchmark_claim_patterns = [
        (
            r"\bperformance benchmark\s+"
            r"(?:is|was|has been)?\s*"
            r"(?:passed|validated|verified|confirmed)\b"
        ),

        (
            r"\bbenchmark\s+"
            r"(?:is|was|has been)?\s*"
            r"(?:passed|validated|verified|confirmed)\b"
        ),

        (
            r"\bperformance improvement\s+"
            r"(?:is|was|has been)?\s*"
            r"(?:confirmed|verified|validated|proven)\b"
        ),
    ]

    # Statements explicitly saying validation has NOT happened
    # are allowed.

    safe_context_patterns = [
        r"\buntil\b.*\bvalidated\b",
        r"\buntil\b.*\bverified\b",

        r"\bnot yet\b.*\bvalidated\b",
        r"\bnot yet\b.*\bverified\b",

        r"\bhas not been\b.*\bvalidated\b",
        r"\bhave not been\b.*\bvalidated\b",

        r"\bmust be\b.*\bvalidated\b",
        r"\bneeds to be\b.*\bvalidated\b",
        r"\brequires\b.*\bvalidation\b",

        (
            r"\bexpected to be\s+"
            r"semantically equivalent\b"
        ),

        (
            r"\bsemantic equivalence\s+"
            r"is expected\b"
        ),
    ]

    matches = []

    for segment in segments:

        safe_context = any(
            re.search(
                pattern,
                segment,
            )
            for pattern
            in safe_context_patterns
        )

        if safe_context:
            continue

        claim_patterns = (
            semantic_claim_patterns
            + benchmark_claim_patterns
        )

        for pattern in claim_patterns:

            match = re.search(
                pattern,
                segment,
            )

            if match:

                matches.append({
                    "claim": (
                        match.group(0)
                    ),
                    "context": (
                        segment
                    ),
                })

                break

    return {
        "passed": (
            len(matches) == 0
        ),

        "matches": matches,

        "reason": (
            None
            if not matches
            else (
                "Agent claimed deterministic "
                "semantic/performance validation "
                "before executor verification."
            )
        ),
    }

def load_cases() -> list[dict]:

    cases = []

    for path in sorted(
        CASE_DIR.glob("*.json")
    ):
        with path.open(
            "r",
            encoding="utf-8",
        ) as f:
            case = json.load(f)

        cases.append(case)

    return cases

def run_setup_sql(
    statements: list[str],
) -> None:

    if not statements:
        return

    with psycopg.connect(
        **DB_CONFIG
    ) as conn:

        with conn.cursor() as cur:

            for statement in statements:

                print(
                    f"  [Setup] {statement}"
                )

                cur.execute(
                    statement
                )

        conn.commit()



def build_case_prompt(
    case: dict,
) -> str:

    query = case["query"]

    return f"""
Diagnose the following PostgreSQL query using real
database evidence.

Identify the most likely root cause.

If the evidence strongly supports a safe optimization or
maintenance action, submit the most appropriate structured
action proposal.

Do not assume that every Sequential Scan requires an index.

SQL:

{query}
"""

def get_tool_names(
    result: dict,
) -> list[str]:

    return [
        item["tool"]
        for item in result.get(
            "tool_trace",
            [],
        )
    ]

def get_proposal_types(
    result: dict,
) -> list[str]:

    return [
        proposal.get("type")
        for proposal in result.get(
            "proposals",
            [],
        )
    ]

def evaluate_required_tools(
    case: dict,
    result: dict,
) -> dict:

    required = set(
        case.get(
            "required_tools",
            [],
        )
    )

    actual = set(
        get_tool_names(
            result
        )
    )

    missing = (
        required - actual
    )

    return {
        "passed": (
            len(missing) == 0
        ),
        "required": sorted(
            required
        ),
        "actual": sorted(
            actual
        ),
        "missing": sorted(
            missing
        ),
    }

def evaluate_forbidden_actions(
    case: dict,
    result: dict,
) -> dict:

    forbidden = set(
        case.get(
            "expected",
            {},
        ).get(
            "forbidden_proposal_types",
            [],
        )
    )

    actual = set(
        get_proposal_types(
            result
        )
    )

    violations = (
        forbidden & actual
    )

    return {
        "passed": (
            len(violations) == 0
        ),
        "forbidden": sorted(
            forbidden
        ),
        "violations": sorted(
            violations
        ),
    }

def evaluate_expected_proposal(
    case: dict,
    result: dict,
) -> dict:

    expected = case[
        "expected"
    ]

    proposals = result.get(
        "proposals",
        []
    )

    # ----------------------------------
    # NO_ACTION case
    # ----------------------------------

    if expected.get(
        "expect_no_action",
        False,
    ):

        if len(proposals) == 0:
            return {
                "passed": True,
                "expected_type": (
                    "NO_ACTION"
                ),
                "actual_types": [],
            }

        return {
            "passed": False,
            "expected_type": (
                "NO_ACTION"
            ),
            "actual_types": [
                proposal.get("type")
                for proposal in proposals
            ],
            "reason": (
                "Expected no action proposal, "
                "but the agent produced one or "
                "more optimization proposals."
            ),
        }

    # ----------------------------------
    # Action-expected cases
    # ----------------------------------

    expected_type = expected.get(
        "proposal_type"
    )

    matching = [
        proposal
        for proposal in proposals
        if proposal.get("type")
        == expected_type
    ]

    if not matching:
        return {
            "passed": False,
            "expected_type": (
                expected_type
            ),
            "reason": (
                "Expected proposal type "
                "was not produced."
            ),
        }

    expected_table = (
        expected.get("table")
    )

    expected_column = (
        expected.get("column")
    )

    expected_columns = (
        expected.get("columns")
    )

    if (
        expected_table is not None
        or expected_column is not None
        or expected_columns is not None
    ):

        proposal_match = False

        for proposal in matching:

            table_ok = (
                expected_table is None
                or proposal.get("table")
                == expected_table
            )

            column_ok = (
                expected_column is None
                or proposal.get("column")
                == expected_column
            )

            columns_ok = (
                expected_columns is None
                or set(
                    proposal.get(
                        "columns",
                        [],
                    )
                )
                == set(
                    expected_columns
                )
            )

            if (
                table_ok
                and column_ok
                and columns_ok
            ):
                proposal_match = True
                break

        if not proposal_match:
            return {
                "passed": False,
                "expected_type": (
                    expected_type
                ),
                "reason": (
                    "Proposal type matched, "
                    "but expected target "
                    "table/column(s) did not match."
                ),
            }

    return {
        "passed": True,
        "expected_type": (
            expected_type
        ),
    }

def evaluate_case_result(
    case: dict,
    result: dict,
) -> dict:

    proposal_check = (
        evaluate_expected_proposal(
            case,
            result,
        )
    )

    tool_check = (
        evaluate_required_tools(
            case,
            result,
        )
    )

    forbidden_check = (
        evaluate_forbidden_actions(
            case,
            result,
        )
    )

    unsupported_claims_check = (
        evaluate_unsupported_claims(
            result
        )
    )

    decision_passed = all([
        proposal_check["passed"],
        tool_check["passed"],
        forbidden_check[
            "passed"
        ],
    ])

    evidence_fidelity_passed = (
        unsupported_claims_check[
            "passed"
        ]
    )

    passed = (
        decision_passed
        and evidence_fidelity_passed
    )

    return {
        "passed": passed,
        "proposal": proposal_check,
        "tools": tool_check,
        "forbidden_actions": (
            forbidden_check
        ),
        "unsupported_claims": (
            unsupported_claims_check
        ),
                "decision_passed": (
            decision_passed
        ),

        "evidence_fidelity_passed": (
            evidence_fidelity_passed
        ),
    }


def run_case(
    case: dict,
) -> dict:

    print()
    print(
        "=" * 60
    )

    print(
        f"Case: {case['case_id']}"
    )

    print(
        f"Title: {case['title']}"
    )

    print(
        "=" * 60
    )

    run_setup_sql(
        case.get(
            "setup_sql",
            [],
        )
    )

    prompt = build_case_prompt(
        case
    )

    result = run_agent(
        prompt
    )

    evaluation = (
        evaluate_case_result(
            case,
            result,
        )
    )

    return {
        "case_id": case[
            "case_id"
        ],

        "title": case[
            "title"
        ],

        "agent_answer": (
            result.get(
                "answer",
                "",
            )
        ),

        "passed": evaluation[
            "passed"
        ],

        "evaluation": (
            evaluation
        ),

        "proposal_types": (
            get_proposal_types(
                result
            )
        ),

        "tool_trace": (
            get_tool_names(
                result
            )
        ),
    }


if __name__ == "__main__":

    cases = load_cases()

    if not cases:
        raise RuntimeError(
            "No benchmark cases found."
        )

    all_results = []

    # ----------------------------------
    # Run benchmark cases
    # ----------------------------------

    for case in cases:

        print()
        print(
            "#" * 60
        )

        print(
            f"Benchmarking: "
            f"{case['case_id']}"
        )

        print(
            f"Runs: {RUNS_PER_CASE}"
        )

        print(
            "#" * 60
        )

        case_results = []

        for run_number in range(
            1,
            RUNS_PER_CASE + 1,
        ):

            print()
            print(
                f"--- Run "
                f"{run_number}/"
                f"{RUNS_PER_CASE} ---"
            )

            result = run_case(
                case
            )

            result[
                "run_number"
            ] = run_number

            case_results.append(
                result
            )

            all_results.append({
                "case": case,
                "result": result,
            })

            print()
            print(
                "Run Result:",
                (
                    "PASS"
                    if result[
                        "passed"
                    ]
                    else "FAIL"
                ),
            )

        # ----------------------------------
        # Per-case reliability
        # ----------------------------------

        case_passed = sum(
            1
            for result
            in case_results
            if result["passed"]
        )

        reliability = (
            case_passed
            / RUNS_PER_CASE
            * 100.0
        )

        print()
        print(
            f"Case Summary: "
            f"{case['case_id']}"
        )

        print(
            f"Passed Runs: "
            f"{case_passed}/"
            f"{RUNS_PER_CASE}"
        )

        print(
            f"Reliability: "
            f"{reliability:.2f}%"
        )

    # ----------------------------------
    # Overall metrics
    # ----------------------------------

    total_runs = len(
        all_results
    )

    passed_runs = sum(
        1
        for item in all_results
        if item[
            "result"
        ][
            "passed"
        ]
    )

    failed_runs = (
        total_runs
        - passed_runs
    )


    decision_passed_runs = sum(
        1
        for item in all_results
        if item[
            "result"
        ][
            "evaluation"
        ].get(
            "decision_passed",
            False,
        )
    )

    evidence_fidelity_passed_runs = sum(
        1
        for item in all_results
        if item[
            "result"
        ][
            "evaluation"
        ].get(
            "evidence_fidelity_passed",
            False,
        )
    )

    decision_pass_rate = (
        decision_passed_runs
        / total_runs
        * 100.0
    )

    evidence_fidelity_pass_rate = (
        evidence_fidelity_passed_runs
        / total_runs
        * 100.0
    )

    print(
        f"Decision Pass Rate: "
        f"{decision_pass_rate:.2f}%"
    )

    print(
        f"Evidence Fidelity Pass Rate: "
        f"{evidence_fidelity_pass_rate:.2f}%"
    )

    overall_pass_rate = (
        passed_runs
        / total_runs
        * 100.0
    )

    # ----------------------------------
    # False positive metric
    # ----------------------------------

    no_action_runs = [
        item
        for item in all_results
        if item[
            "case"
        ].get(
            "expected",
            {},
        ).get(
            "expect_no_action",
            False,
        )
    ]

    false_positive_runs = [
        item
        for item in no_action_runs
        if len(
            item[
                "result"
            ].get(
                "proposal_types",
                [],
            )
        ) > 0
    ]

    if no_action_runs:

        false_positive_rate = (
            len(
                false_positive_runs
            )
            / len(
                no_action_runs
            )
            * 100.0
        )

    else:
        false_positive_rate = 0.0

    # ----------------------------------
    # Final summary
    # ----------------------------------

    print()
    print(
        "=" * 60
    )

    print(
        "SafeDBA Evaluation Summary"
    )

    print(
        "=" * 60
    )

    print(
        f"Cases: "
        f"{len(cases)}"
    )

    print(
        f"Runs per Case: "
        f"{RUNS_PER_CASE}"
    )

    print(
        f"Total Runs: "
        f"{total_runs}"
    )

    print(
        f"Passed Runs: "
        f"{passed_runs}"
    )

    print(
        f"Failed Runs: "
        f"{failed_runs}"
    )

    print(
        f"Overall Run Pass Rate: "
        f"{overall_pass_rate:.2f}%"
    )

    print(
        f"No-Action Runs: "
        f"{len(no_action_runs)}"
    )

    print(
        f"False Positive "
        f"Optimizations: "
        f"{len(false_positive_runs)}"
    )

    print(
        f"False Positive "
        f"Optimization Rate: "
        f"{false_positive_rate:.2f}%"
    )

    # ----------------------------------
    # Save machine-readable report
    # ----------------------------------

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    report = {
        "benchmark": {
            "cases": len(cases),
            "runs_per_case": (
                RUNS_PER_CASE
            ),
            "total_runs": (
                total_runs
            ),
        },

        "metrics": {
            "passed_runs": (
                passed_runs
            ),
            "failed_runs": (
                failed_runs
            ),
            "overall_run_pass_rate": (
                overall_pass_rate
            ),
            "no_action_runs": (
                len(no_action_runs)
            ),
            "false_positive_optimizations": (
                len(
                    false_positive_runs
                )
            ),
            "false_positive_optimization_rate": (
                false_positive_rate
            ),
                        "decision_passed_runs": (
                decision_passed_runs
            ),

            "decision_pass_rate": (
                decision_pass_rate
            ),

            "evidence_fidelity_passed_runs": (
                evidence_fidelity_passed_runs
            ),

            "evidence_fidelity_pass_rate": (
                evidence_fidelity_pass_rate
            ),
        },

        "runs": all_results,
    }

    with REPORT_PATH.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            report,
            f,
            indent=2,
            ensure_ascii=False,
            default=str,
        )

    print()
    print(
        f"Evaluation report saved to: "
        f"{REPORT_PATH}"
    )
