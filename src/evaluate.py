import json
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
import re
import psycopg

from agent import run_agent
from config import (
    EXECUTOR_DB_CONFIG,
    LLM_MODEL,
    LLM_PROVIDER,
    SAFEDBA_ENV,
)
from agent_policy import canonical_query
from query_guard import ensure_read_only_query

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = PROJECT_ROOT / "benchmarks" / "cases"
#Run repeatedly
RUNS_PER_CASE = 3

REPORT_DIR = PROJECT_ROOT / "benchmarks" / "results"

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

    if SAFEDBA_ENV != "benchmark":
        raise RuntimeError(
            "Destructive benchmark setup is disabled. Set "
            "SAFEDBA_ENV=benchmark only for the disposable "
            "local regression database."
        )

    host = str(
        EXECUTOR_DB_CONFIG.get("host", "")
    ).strip().lower()
    database = str(
        EXECUTOR_DB_CONFIG.get("dbname", "")
    ).strip().lower()

    if host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    } or database != "benchmark":
        raise RuntimeError(
            "Benchmark setup is allowed only against the local "
            "database named 'benchmark'."
        )

    with psycopg.connect(
        **EXECUTOR_DB_CONFIG
    ) as conn:

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
                or target[1]
                != EXECUTOR_DB_CONFIG.get("user")
                or target[2]
                or not target[3]
            ):
                raise RuntimeError(
                    "Benchmark target verification failed: the "
                    "database must contain the SafeDBA marker and "
                    "the executor must not be a superuser."
                )

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
        if item.get("status") == "success"
    ]


def _query_summary_matches(
    value,
    query: str,
) -> bool:
    if not isinstance(value, dict):
        return False

    normalized = canonical_query(query)
    if normalized is None:
        return False

    if "canonical_sha256" in value:
        return (
            value.get("canonical_sha256")
            == hashlib.sha256(
                normalized.encode("utf-8")
            ).hexdigest()
            and value.get("canonical_characters")
            == len(normalized)
        )

    return (
        value.get("sha256")
        == hashlib.sha256(
            query.encode("utf-8")
        ).hexdigest()
        and value.get("characters") == len(query)
    )


def _trace_arguments_match(
    trace: dict,
    expected: dict,
) -> bool:
    actual = trace.get("arguments")
    if not isinstance(actual, dict):
        return False

    for key, value in expected.items():
        if key in {
            "query",
            "original_query",
            "rewritten_query",
        }:
            if not _query_summary_matches(
                actual.get(key),
                value,
            ):
                return False
        elif actual.get(key) != value:
            return False

    return True

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

    successful_trace = [
        item
        for item in result.get("tool_trace", [])
        if item.get("status") == "success"
    ]
    actual = {
        item.get("tool")
        for item in successful_trace
    }

    missing = (
        required - actual
    )

    argument_failures = []
    query = case.get("query")
    if (
        "analyze_query" in required
        and isinstance(query, str)
        and not any(
            item.get("tool") == "analyze_query"
            and _trace_arguments_match(
                item,
                {"query": query},
            )
            for item in successful_trace
        )
    ):
        argument_failures.append(
            "analyze_query was not bound to the case query."
        )

    expected_arguments = case.get(
        "required_tool_arguments",
        {},
    )
    for tool, specifications in expected_arguments.items():
        if isinstance(specifications, dict):
            specifications = [specifications]
        if not isinstance(specifications, list):
            argument_failures.append(
                f"Invalid required_tool_arguments for {tool}."
            )
            continue
        for specification in specifications:
            if not any(
                item.get("tool") == tool
                and _trace_arguments_match(
                    item,
                    specification,
                )
                for item in successful_trace
            ):
                argument_failures.append(
                    f"No successful {tool} call matched "
                    f"{specification}."
                )

    return {
        "passed": not missing and not argument_failures,
        "required": sorted(
            required
        ),
        "actual": sorted(
            actual
        ),
        "missing": sorted(
            missing
        ),
        "argument_failures": argument_failures,
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


def validate_proposal_shape(
    proposal: dict,
) -> list[str]:
    if not isinstance(proposal, dict):
        return ["Proposal must be a JSON object."]

    action_type = proposal.get("type")
    required_fields = {
        "CREATE_INDEX": {
            "query",
            "table",
            "column",
            "index_name",
            "reason",
            "confidence",
            "risk",
            "evidence_refs",
        },
        "REWRITE_QUERY": {
            "original_query",
            "rewritten_query",
            "reason",
            "confidence",
            "risk",
            "evidence_refs",
        },
        "ANALYZE_TABLE": {
            "query",
            "table",
            "columns",
            "reason",
            "confidence",
            "risk",
            "evidence_refs",
        },
        "TERMINATE_BACKEND": {
            "blocked_pid",
            "blocker_pid",
            "blocker_backend_start",
            "blocker_xact_start",
            "reason",
            "confidence",
            "risk",
            "evidence_refs",
        },
    }
    expected_risk = {
        "CREATE_INDEX": "MEDIUM",
        "REWRITE_QUERY": "LOW",
        "ANALYZE_TABLE": "MEDIUM",
        "TERMINATE_BACKEND": "HIGH",
    }
    errors = []

    if action_type not in required_fields:
        return [
            f"Unsupported proposal type: {action_type}"
        ]

    missing = (
        required_fields[action_type]
        - set(proposal)
    )
    if missing:
        errors.append(
            "Missing proposal fields: "
            + ", ".join(sorted(missing))
        )

    reason = proposal.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(
            "Proposal reason must be a non-empty string."
        )

    for field in {
        "table",
        "column",
        "index_name",
    } & required_fields[action_type]:
        if (
            not isinstance(proposal.get(field), str)
            or not proposal.get(field).strip()
        ):
            errors.append(
                f"Proposal field '{field}' must be a non-empty string."
            )

    query_fields = {
        "CREATE_INDEX": ["query"],
        "REWRITE_QUERY": [
            "original_query",
            "rewritten_query",
        ],
        "ANALYZE_TABLE": ["query"],
        "TERMINATE_BACKEND": [],
    }[action_type]
    for field in query_fields:
        value = proposal.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(
                f"Proposal field '{field}' must be non-empty SQL."
            )
            continue
        try:
            ensure_read_only_query(value)
        except ValueError as exc:
            errors.append(
                f"Unsafe {field}: {exc}"
            )

    if action_type == "CREATE_INDEX":
        table = proposal.get("table")
        column = proposal.get("column")
        if isinstance(table, str) and isinstance(column, str):
            base = f"idx_{table}_{column}"
            if len(base.encode("utf-8")) <= 63:
                expected_name = base
            else:
                suffix = "_" + hashlib.sha256(
                    base.encode("utf-8")
                ).hexdigest()[:10]
                prefix = base.encode("utf-8")[
                    : 63 - len(suffix)
                ]
                while True:
                    try:
                        expected_name = prefix.decode("utf-8") + suffix
                        break
                    except UnicodeDecodeError:
                        prefix = prefix[:-1]
            if proposal.get("index_name") != expected_name:
                errors.append(
                    "CREATE_INDEX index_name is not deterministic."
                )

    if action_type == "REWRITE_QUERY" and (
        canonical_query(proposal.get("original_query"))
        == canonical_query(proposal.get("rewritten_query"))
    ):
        errors.append(
            "Rewritten query must differ from the original query."
        )

    if action_type == "ANALYZE_TABLE":
        columns = proposal.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or any(
                not isinstance(column, str)
                or not column.strip()
                for column in columns
            )
            or len(columns) != len(set(columns))
        ):
            errors.append(
                "ANALYZE_TABLE columns must be a non-empty unique "
                "string list."
            )

    if action_type == "TERMINATE_BACKEND":
        for field in ("blocked_pid", "blocker_pid"):
            value = proposal.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                errors.append(
                    f"{field} must be a positive integer."
                )
        for field in (
            "blocker_backend_start",
            "blocker_xact_start",
        ):
            if (
                not isinstance(proposal.get(field), str)
                or not proposal.get(field).strip()
            ):
                errors.append(
                    f"{field} must be a non-empty timestamp string."
                )

    confidence = proposal.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(
            confidence,
            (int, float),
        )
        or not math.isfinite(float(confidence))
        or not (0 <= confidence <= 1)
    ):
        errors.append(
            "Proposal confidence is invalid."
        )

    if proposal.get("risk") != expected_risk[
        action_type
    ]:
        errors.append(
            "Proposal risk does not match policy."
        )

    evidence_refs = proposal.get(
        "evidence_refs"
    )
    if (
        not isinstance(evidence_refs, list)
        or not evidence_refs
        or any(
            not isinstance(ref, str)
            or not re.fullmatch(
                r"ev-\d{4,}",
                ref,
                re.IGNORECASE,
            )
            for ref in evidence_refs
        )
        or len(evidence_refs) != len(set(evidence_refs))
    ):
        errors.append(
            "Proposal lacks valid evidence references."
        )

    return errors

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

    if len(proposals) != 1:
        return {
            "passed": False,
            "expected_type": expected.get(
                "proposal_type"
            ),
            "actual_count": len(proposals),
            "reason": (
                "Action cases require exactly one proposal."
            ),
        }

    shape_errors = validate_proposal_shape(
        proposals[0]
    )
    if shape_errors:
        return {
            "passed": False,
            "expected_type": expected.get(
                "proposal_type"
            ),
            "reason": (
                "Proposal schema validation failed."
            ),
            "errors": shape_errors,
        }

    proposal_query = proposals[0].get({
        "CREATE_INDEX": "query",
        "REWRITE_QUERY": "original_query",
        "ANALYZE_TABLE": "query",
    }.get(proposals[0].get("type"), ""))
    case_query = case.get("query")
    if (
        isinstance(case_query, str)
        and proposals[0].get("type")
        != "TERMINATE_BACKEND"
        and canonical_query(proposal_query)
        != canonical_query(case_query)
    ):
        return {
            "passed": False,
            "expected_type": expected.get("proposal_type"),
            "reason": (
                "Proposal SQL is not bound to the benchmark case query."
            ),
        }

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


def evaluate_proposal_evidence_binding(
    result: dict,
) -> dict:
    successful_by_ref = {
        item.get("evidence_ref"): item
        for item in result.get("tool_trace", [])
        if (
            item.get("status") == "success"
            and isinstance(item.get("evidence_ref"), str)
        )
    }
    errors = []

    for index, proposal in enumerate(
        result.get("proposals", [])
    ):
        if not isinstance(proposal, dict):
            errors.append(
                f"Proposal {index} is not an object."
            )
            continue
        refs = proposal.get("evidence_refs", [])
        traces = [
            successful_by_ref.get(ref)
            for ref in refs
            if isinstance(ref, str)
        ]
        if not traces or any(trace is None for trace in traces):
            errors.append(
                f"Proposal {index} references missing/failed evidence."
            )
            continue

        def has(tool: str, expected: dict | None = None) -> bool:
            return any(
                trace.get("tool") == tool
                and (
                    expected is None
                    or _trace_arguments_match(trace, expected)
                )
                for trace in traces
            )

        action_type = proposal.get("type")
        if action_type == "CREATE_INDEX":
            if not has(
                "analyze_query",
                {"query": proposal.get("query")},
            ):
                errors.append(
                    f"Proposal {index} lacks same-query plan evidence."
                )
            if not has(
                "get_indexes",
                {"table_name": proposal.get("table")},
            ):
                errors.append(
                    f"Proposal {index} lacks same-table index evidence."
                )
        elif action_type == "REWRITE_QUERY":
            if not has(
                "analyze_query",
                {"query": proposal.get("original_query")},
            ):
                errors.append(
                    f"Proposal {index} lacks original-query evidence."
                )
            if not has("get_column_info"):
                errors.append(
                    f"Proposal {index} lacks column-type evidence."
                )
        elif action_type == "ANALYZE_TABLE":
            if not has(
                "analyze_query",
                {"query": proposal.get("query")},
            ):
                errors.append(
                    f"Proposal {index} lacks same-query plan evidence."
                )
            for column in proposal.get("columns", []):
                if not has(
                    "get_column_stats",
                    {
                        "table_name": proposal.get("table"),
                        "column_name": column,
                    },
                ):
                    errors.append(
                        f"Proposal {index} lacks statistics evidence "
                        f"for {proposal.get('table')}.{column}."
                    )
        elif action_type == "TERMINATE_BACKEND":
            if not has("get_lock_waits"):
                errors.append(
                    f"Proposal {index} lacks lock-wait evidence."
                )

    return {
        "passed": not errors,
        "errors": errors,
    }


def evaluate_diagnosis_expectation(
    case: dict,
    result: dict,
) -> dict:
    answer = result.get("answer", "")

    # A positive keyword match is not enough when the answer explicitly
    # negates the expected diagnosis (for example, "not a missing index").
    # Cases keep these contradictions explicit so benchmark semantics remain
    # reviewable instead of being hidden in a generic sentiment heuristic.
    forbidden_patterns = case.get(
        "forbidden_diagnosis_patterns",
        [],
    )
    for pattern in forbidden_patterns:
        try:
            if re.search(
                pattern,
                answer,
                re.IGNORECASE,
            ):
                return {
                    "passed": False,
                    "matched": None,
                    "matched_forbidden": pattern,
                    "reason": (
                        "Answer explicitly contradicted the expected "
                        "root-cause class."
                    ),
                }
        except re.error as exc:
            return {
                "passed": False,
                "matched": None,
                "matched_forbidden": None,
                "reason": (
                    "Invalid forbidden diagnosis regex: "
                    f"{exc}"
                ),
            }

    patterns = case.get(
        "required_diagnosis_patterns",
        [],
    )
    if not patterns:
        return {
            "passed": True,
            "matched": None,
        }

    for pattern in patterns:
        try:
            if re.search(
                pattern,
                answer,
                re.IGNORECASE,
            ):
                return {
                    "passed": True,
                    "matched": pattern,
                }
        except re.error as exc:
            return {
                "passed": False,
                "matched": None,
                "reason": f"Invalid diagnosis regex: {exc}",
            }

    return {
        "passed": False,
        "matched": None,
        "reason": (
            "Answer did not state the expected evidence-supported "
            "root-cause class."
        ),
    }


def evaluate_evidence_references(
    result: dict,
) -> dict:
    successful_refs = {
        item.get("evidence_ref")
        for item in result.get(
            "tool_trace",
            [],
        )
        if (
            item.get("status") == "success"
            and item.get("evidence_ref")
        )
    }
    answer_refs = {
        ref.lower()
        for ref in re.findall(
            r"\[(ev-\d{4,})\]",
            result.get("answer", ""),
            re.IGNORECASE,
        )
    }
    proposal_refs = {
        ref
        for proposal in result.get(
            "proposals",
            [],
        )
        if isinstance(proposal, dict)
        for ref in proposal.get(
            "evidence_refs",
            [],
        )
        if isinstance(ref, str)
    }
    invalid_answer_refs = (
        answer_refs - successful_refs
    )
    invalid_proposal_refs = (
        proposal_refs - successful_refs
    )
    missing_answer_refs = (
        bool(successful_refs)
        and not answer_refs
    )

    return {
        "passed": not any([
            invalid_answer_refs,
            invalid_proposal_refs,
            missing_answer_refs,
        ]),
        "successful_refs": sorted(
            successful_refs
        ),
        "answer_refs": sorted(answer_refs),
        "proposal_refs": sorted(
            proposal_refs
        ),
        "invalid_answer_refs": sorted(
            invalid_answer_refs
        ),
        "invalid_proposal_refs": sorted(
            invalid_proposal_refs
        ),
        "missing_answer_refs": (
            missing_answer_refs
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

    evidence_reference_check = (
        evaluate_evidence_references(
            result
        )
    )

    proposal_evidence_check = (
        evaluate_proposal_evidence_binding(
            result
        )
    )

    diagnosis_check = (
        evaluate_diagnosis_expectation(
            case,
            result,
        )
    )

    run_status_check = {
        "passed": (
            result.get("status")
            == "completed"
            and result.get("stop_reason")
            == "final_answer"
        ),
        "status": result.get("status"),
        "stop_reason": result.get(
            "stop_reason"
        ),
    }

    decision_passed = all([
        run_status_check["passed"],
        proposal_check["passed"],
        tool_check["passed"],
        forbidden_check[
            "passed"
        ],
        diagnosis_check["passed"],
    ])

    evidence_integrity_passed = (
        unsupported_claims_check[
            "passed"
        ]
        and evidence_reference_check[
            "passed"
        ]
        and proposal_evidence_check[
            "passed"
        ]
    )

    passed = (
        decision_passed
        and evidence_integrity_passed
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
        "evidence_references": (
            evidence_reference_check
        ),
        "proposal_evidence_binding": (
            proposal_evidence_check
        ),
        "diagnosis_expectation": (
            diagnosis_check
        ),
        "run_status": run_status_check,
                "decision_passed": (
            decision_passed
        ),

        "evidence_integrity_passed": (
            evidence_integrity_passed
        ),
        # Backward-compatible report field.  The precise metric name is
        # evidence_integrity because arbitrary natural-language claims do
        # not yet carry field-path-level proof.
        "evidence_fidelity_passed": (
            evidence_integrity_passed
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
        prompt,
        mode="propose",
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

        "proposals": result.get(
            "proposals",
            [],
        ),

        "agent_status": result.get(
            "status"
        ),

        "stop_reason": result.get(
            "stop_reason"
        ),

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
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
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
    print(
        f"Evaluation report saved to: "
        f"{REPORT_PATH}"
    )
