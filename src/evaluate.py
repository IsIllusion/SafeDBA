"""Benchmark CLI and database setup; grading is provided by evaluation_policy."""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import psycopg

from agent import run_agent
from config import EXECUTOR_DB_CONFIG, LLM_MODEL, LLM_PROVIDER, SAFEDBA_ENV

# Keep the existing evaluate.* grading API available to callers.
from evaluation_policy import (
    evaluate_unsupported_claims,
    get_tool_names,
    _query_summary_matches,
    _trace_arguments_match,
    get_proposal_types,
    evaluate_required_tools,
    evaluate_forbidden_actions,
    validate_proposal_shape,
    evaluate_expected_proposal,
    evaluate_proposal_evidence_binding,
    evaluate_diagnosis_expectation,
    evaluate_evidence_references,
    evaluate_case_result,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = PROJECT_ROOT / "benchmarks" / "cases"
# Run repeatedly
RUNS_PER_CASE = 3

REPORT_DIR = PROJECT_ROOT / "benchmarks" / "results"

REPORT_PATH = REPORT_DIR / "latest_evaluation.json"


def load_cases() -> list[dict]:

    cases = []

    for path in sorted(CASE_DIR.glob("*.json")):
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

    if SAFEDBA_ENV != "benchmark":
        raise RuntimeError(
            "Destructive benchmark setup is disabled. Set "
            "SAFEDBA_ENV=benchmark only for the disposable "
            "local regression database."
        )

    host = str(EXECUTOR_DB_CONFIG.get("host", "")).strip().lower()
    database = str(EXECUTOR_DB_CONFIG.get("dbname", "")).strip().lower()

    if (
        host
        not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        or database != "benchmark"
    ):
        raise RuntimeError(
            "Benchmark setup is allowed only against the local "
            "database named 'benchmark'."
        )

    with psycopg.connect(**EXECUTOR_DB_CONFIG) as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    current_database(),
                    current_user,
                    role.rolsuper,
                    EXISTS (
                        SELECT 1
                        FROM public.safedba_benchmark_marker
                        WHERE marker =
                            'SAFE_TO_RUN_DESTRUCTIVE_BENCHMARKS'
                    )
                FROM pg_roles AS role
                WHERE role.rolname = current_user;
            """)
            target = cur.fetchone()

            if (
                target is None
                or target[0] != "benchmark"
                or target[1] != EXECUTOR_DB_CONFIG.get("user")
                or target[2]
                or not target[3]
            ):
                raise RuntimeError(
                    "Benchmark target verification failed: the "
                    "database must contain the SafeDBA marker and "
                    "the executor must not be a superuser."
                )

            for statement in statements:

                print(f"  [Setup] {statement}")

                cur.execute(statement)

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


def run_case(
    case: dict,
) -> dict:

    print()
    print("=" * 60)

    print(f"Case: {case['case_id']}")

    print(f"Title: {case['title']}")

    print("=" * 60)

    run_setup_sql(
        case.get(
            "setup_sql",
            [],
        )
    )

    prompt = build_case_prompt(case)

    result = run_agent(
        prompt,
        mode="propose",
    )

    evaluation = evaluate_case_result(
        case,
        result,
    )

    return {
        "case_id": case["case_id"],
        "title": case["title"],
        "agent_answer": (
            result.get(
                "answer",
                "",
            )
        ),
        "passed": evaluation["passed"],
        "evaluation": (evaluation),
        "proposal_types": (get_proposal_types(result)),
        "proposals": result.get(
            "proposals",
            [],
        ),
        "agent_status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
        "tool_trace": (
            result.get(
                "tool_trace",
                [],
            )
        ),
    }


if __name__ == "__main__":

    cases = load_cases()

    if not cases:
        raise RuntimeError("No benchmark cases found.")

    all_results = []

    # ----------------------------------
    # Run benchmark cases
    # ----------------------------------

    for case in cases:

        print()
        print("#" * 60)

        print(f"Benchmarking: " f"{case['case_id']}")

        print(f"Runs: {RUNS_PER_CASE}")

        print("#" * 60)

        case_results = []

        for run_number in range(
            1,
            RUNS_PER_CASE + 1,
        ):

            print()
            print(f"--- Run " f"{run_number}/" f"{RUNS_PER_CASE} ---")

            result = run_case(case)

            result["run_number"] = run_number

            case_results.append(result)

            all_results.append(
                {
                    "case": case,
                    "result": result,
                }
            )

            print()
            print(
                "Run Result:",
                ("PASS" if result["passed"] else "FAIL"),
            )

        # ----------------------------------
        # Per-case reliability
        # ----------------------------------

        case_passed = sum(1 for result in case_results if result["passed"])

        reliability = case_passed / RUNS_PER_CASE * 100.0

        print()
        print(f"Case Summary: " f"{case['case_id']}")

        print(f"Passed Runs: " f"{case_passed}/" f"{RUNS_PER_CASE}")

        print(f"Reliability: " f"{reliability:.2f}%")

    # ----------------------------------
    # Overall metrics
    # ----------------------------------

    total_runs = len(all_results)

    passed_runs = sum(1 for item in all_results if item["result"]["passed"])

    failed_runs = total_runs - passed_runs

    decision_passed_runs = sum(
        1
        for item in all_results
        if item["result"]["evaluation"].get(
            "decision_passed",
            False,
        )
    )

    evidence_fidelity_passed_runs = sum(
        1
        for item in all_results
        if item["result"]["evaluation"].get(
            "evidence_fidelity_passed",
            False,
        )
    )

    decision_pass_rate = decision_passed_runs / total_runs * 100.0

    evidence_fidelity_pass_rate = evidence_fidelity_passed_runs / total_runs * 100.0

    print(f"Decision Pass Rate: " f"{decision_pass_rate:.2f}%")

    print(f"Evidence Fidelity Pass Rate: " f"{evidence_fidelity_pass_rate:.2f}%")

    overall_pass_rate = passed_runs / total_runs * 100.0

    # ----------------------------------
    # False positive metric
    # ----------------------------------

    no_action_runs = [
        item
        for item in all_results
        if item["case"]
        .get(
            "expected",
            {},
        )
        .get(
            "expect_no_action",
            False,
        )
    ]

    false_positive_runs = [
        item
        for item in no_action_runs
        if len(
            item["result"].get(
                "proposal_types",
                [],
            )
        )
        > 0
    ]

    if no_action_runs:

        false_positive_rate = len(false_positive_runs) / len(no_action_runs) * 100.0

    else:
        false_positive_rate = 0.0

    # ----------------------------------
    # Final summary
    # ----------------------------------

    print()
    print("=" * 60)

    print("SafeDBA Evaluation Summary")

    print("=" * 60)

    print(f"Cases: " f"{len(cases)}")

    print(f"Runs per Case: " f"{RUNS_PER_CASE}")

    print(f"Total Runs: " f"{total_runs}")

    print(f"Passed Runs: " f"{passed_runs}")

    print(f"Failed Runs: " f"{failed_runs}")

    print(f"Overall Run Pass Rate: " f"{overall_pass_rate:.2f}%")

    print(f"No-Action Runs: " f"{len(no_action_runs)}")

    print(f"False Positive " f"Optimizations: " f"{len(false_positive_runs)}")

    print(f"False Positive " f"Optimization Rate: " f"{false_positive_rate:.2f}%")

    # ----------------------------------
    # Save machine-readable report
    # ----------------------------------

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": LLM_PROVIDER,
        "model": LLM_MODEL,
        "case_suite_sha256": hashlib.sha256(
            json.dumps(
                cases,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "benchmark": {
            "cases": len(cases),
            "runs_per_case": (RUNS_PER_CASE),
            "total_runs": (total_runs),
        },
        "metrics": {
            "passed_runs": (passed_runs),
            "failed_runs": (failed_runs),
            "overall_run_pass_rate": (overall_pass_rate),
            "no_action_runs": (len(no_action_runs)),
            "false_positive_optimizations": (len(false_positive_runs)),
            "false_positive_optimization_rate": (false_positive_rate),
            "decision_passed_runs": (decision_passed_runs),
            "decision_pass_rate": (decision_pass_rate),
            "evidence_fidelity_passed_runs": (evidence_fidelity_passed_runs),
            "evidence_fidelity_pass_rate": (evidence_fidelity_pass_rate),
            "metric_note": (
                "The legacy evidence_fidelity name currently measures "
                "reference/argument/proposal integrity plus scenario "
                "root-cause assertions, not field-path proof for every "
                "natural-language claim."
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
    print(f"Evaluation report saved to: " f"{REPORT_PATH}")
