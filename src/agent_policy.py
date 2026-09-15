"""Deterministic orchestration policy for the SafeDBA agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from typing import Any


PROPOSAL_TOOL_TO_ACTION = {
    "propose_create_index": "CREATE_INDEX",
    "propose_query_rewrite": "REWRITE_QUERY",
    "propose_analyze_table": "ANALYZE_TABLE",
    "propose_terminate_backend": "TERMINATE_BACKEND",
}

PROPOSAL_TOOLS = frozenset(
    PROPOSAL_TOOL_TO_ACTION
)

VOLATILE_OBSERVATION_TOOLS = frozenset({
    "get_active_sessions",
    "get_database_health",
    "get_lock_waits",
    "get_transaction_sessions",
})


_DIAGNOSIS_ONLY_PATTERNS = (
    r"\bdiagnos(?:e|is|tic)\s+only\b",
    r"\bread[- ]only\s+(?:investigation|diagnosis)\b",
    r"\bdo\s+not\s+(?:execute|apply|change|modify|terminate)\b",
    r"\bno\s+(?:changes|remediation|execution)\b",
    r"仅\s*诊断",
    r"只\s*诊断",
    r"不要\s*(?:执行|修改|变更|终止)",
    r"不\s*(?:执行|修改|变更|终止)",
)

_EXPLICIT_PROPOSAL_PATTERNS = (
    r"\bpropos(?:e|al|als)\b",
    r"\brecommend(?:ation|ations)?\b",
    r"\bremediat(?:e|ion)\b",
    r"\baction\s+plan\b",
    r"(?:提出|给出|生成).{0,8}(?:提案|建议|方案)",
    r"(?:优化|修复|处置).{0,6}(?:提案|建议|方案)",
)


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def digest_value(value: Any) -> str:
    return hashlib.sha256(
        stable_json(value).encode("utf-8")
    ).hexdigest()


def canonical_query(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1]

    return " ".join(
        normalized.split()
    )


def canonical_call_key(
    tool: str,
    arguments: dict,
) -> str:
    canonical_arguments = dict(arguments)

    for key in (
        "query",
        "original_query",
        "rewritten_query",
    ):
        if key in canonical_arguments:
            canonical_arguments[key] = canonical_query(
                canonical_arguments[key]
            )

    return digest_value({
        "tool": tool,
        "arguments": canonical_arguments,
    })


def is_diagnosis_only_request(
    user_message: str,
) -> bool:
    return any(
        re.search(
            pattern,
            user_message,
            re.IGNORECASE,
        )
        for pattern in _DIAGNOSIS_ONLY_PATTERNS
    )


def is_explicit_proposal_request(
    user_message: str,
) -> bool:
    return any(
        re.search(
            pattern,
            user_message,
            re.IGNORECASE,
        )
        for pattern in _EXPLICIT_PROPOSAL_PATTERNS
    )


def normalize_citation_placeholders(answer: str) -> str:
    """Render exact syntax examples as plain text, not purported source links."""
    return re.sub(r"\[(kb|ev)-(?:\.\.\.|…)\]", r"\1-…", answer, flags=re.IGNORECASE)


def validate_answer_evidence(
    answer: str,
    records: list[EvidenceRecord] | None = None,
    *,
    successful_refs: set[str] | None = None,
    required_refs: set[str] | None = None,
) -> list[str]:
    """Require final answers to cite real, successful evidence records."""

    if successful_refs is None:
        successful_refs = {
            record.ref
            for record in (records or [])
            if record.status == "success"
        }

    cited_refs = {
        match.lower()
        for match in re.findall(
            r"\[(ev-\d{4,})\]",
            answer or "",
            re.IGNORECASE,
        )
    }
    errors: list[str] = []

    if successful_refs and not cited_refs:
        errors.append(
            "The final answer must cite at least one successful "
            "evidence reference in [ev-0001] form."
        )

    invalid_refs = cited_refs - successful_refs
    if invalid_refs:
        errors.append(
            "The final answer cites unknown or unsuccessful evidence: "
            + ", ".join(sorted(invalid_refs))
        )

    missing_required = (
        (required_refs or set()) - cited_refs
    )
    if missing_required:
        errors.append(
            "The final answer must cite every prerequisite used by an "
            "action proposal: "
            + ", ".join(sorted(missing_required))
        )

    return errors


def summarize_arguments(
    arguments: dict,
) -> dict:
    """Return a trace-safe argument summary.

    Query text is fingerprinted rather than copied into ordinary logs.
    The full arguments remain in the in-memory evidence ledger and in the
    provider conversation for the current turn.
    """

    summary: dict[str, Any] = {}

    for key, value in arguments.items():
        if key in {
            "query",
            "original_query",
            "rewritten_query",
            "blocked_query",
            "blocker_query",
        } and isinstance(value, str):
            normalized_query = (
                canonical_query(value)
                or value
            )
            summary[key] = {
                "sha256": hashlib.sha256(
                    value.encode("utf-8")
                ).hexdigest(),
                "characters": len(value),
                "canonical_sha256": hashlib.sha256(
                    normalized_query.encode("utf-8")
                ).hexdigest(),
                "canonical_characters": len(
                    normalized_query
                ),
            }
        elif isinstance(value, str) and len(value) > 500:
            summary[key] = (
                value[:500]
                + "...[truncated]"
            )
        else:
            summary[key] = value

    return summary


def summarize_result(
    result: Any,
) -> dict:
    summary: dict[str, Any] = {
        "sha256": digest_value(result),
        "type": type(result).__name__,
    }

    if isinstance(result, dict):
        summary["keys"] = sorted(
            str(key)
            for key in result.keys()
        )[:50]
    elif isinstance(result, list):
        summary["items"] = len(result)

    return summary


def _strict_tool_json_value(value: Any) -> Any:
    """Convert non-finite floats so tool messages remain strict JSON."""

    if isinstance(value, float) and not math.isfinite(value):
        return {
            "non_finite_float": str(value),
        }
    if isinstance(value, dict):
        return {
            key: _strict_tool_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _strict_tool_json_value(item)
            for item in value
        ]
    return value


def serialize_tool_output(
    result: Any,
    *,
    max_chars: int,
) -> str:
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 256
    ):
        raise ValueError(
            "Tool output limit must be an integer of at least 256."
        )

    payload = json.dumps(
        _strict_tool_json_value(result),
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    )

    if len(payload) <= max_chars:
        return payload

    envelope = {
        "truncated": True,
        "reason": (
            "Tool output exceeded the configured "
            "agent-context limit."
        ),
        "original_characters": len(payload),
        "sha256": hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest(),
        "preview": "",
    }

    # JSON escaping can make N source characters much larger than N output
    # characters. Binary-search the preview so the configured ceiling is a
    # real ceiling even for quote/backslash/control-character-heavy data.
    lower = 0
    upper = len(payload)
    best = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
    )
    while lower <= upper:
        midpoint = (lower + upper) // 2
        envelope["preview"] = payload[:midpoint]
        candidate = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
        )
        if len(candidate) <= max_chars:
            best = candidate
            lower = midpoint + 1
        else:
            upper = midpoint - 1

    if len(best) > max_chars:
        raise ValueError(
            "Tool output limit is too small for truncation metadata."
        )
    return best


def validate_tool_arguments(
    arguments: Any,
    parameters: dict,
) -> list[str]:
    """Validate the small JSON-Schema subset used by SafeDBA tools."""

    if not isinstance(arguments, dict):
        return [
            "Tool arguments must be a JSON object."
        ]

    properties = parameters.get(
        "properties",
        {},
    )
    required = set(
        parameters.get("required", [])
    )
    errors: list[str] = []

    missing = required - set(arguments)
    if missing:
        errors.append(
            "Missing required arguments: "
            + ", ".join(sorted(missing))
        )

    unexpected = set(arguments) - set(properties)
    if unexpected:
        errors.append(
            "Unexpected arguments: "
            + ", ".join(sorted(unexpected))
        )

    type_checks = {
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: (
            isinstance(value, int)
            and not isinstance(value, bool)
        ),
        "number": lambda value: (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ),
        "array": lambda value: isinstance(value, list),
        "object": lambda value: isinstance(value, dict),
        "boolean": lambda value: isinstance(value, bool),
    }

    for name, value in arguments.items():
        specification = properties.get(name)
        if not specification:
            continue

        expected_type = specification.get("type")
        check = type_checks.get(expected_type)

        if check and not check(value):
            errors.append(
                f"Argument '{name}' must have type "
                f"{expected_type}."
            )
            continue

        if (
            expected_type == "string"
            and isinstance(value, str)
        ):
            minimum_length = specification.get(
                "minLength",
                1 if name in required else 0,
            )
            maximum_length = specification.get("maxLength")
            if len(value.strip()) < minimum_length:
                errors.append(
                    f"Argument '{name}' must be a non-blank string."
                )
            if (
                isinstance(maximum_length, int)
                and len(value) > maximum_length
            ):
                errors.append(
                    f"Argument '{name}' exceeds maxLength "
                    f"{maximum_length}."
                )

        if (
            expected_type in {"integer", "number"}
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            minimum = specification.get("minimum")
            maximum = specification.get("maximum")
            if minimum is not None and value < minimum:
                errors.append(
                    f"Argument '{name}' must be at least {minimum}."
                )
            if maximum is not None and value > maximum:
                errors.append(
                    f"Argument '{name}' must be at most {maximum}."
                )

        if expected_type == "array" and isinstance(value, list):
            item_type = specification.get(
                "items",
                {},
            ).get("type")
            item_check = type_checks.get(item_type)

            if item_check and any(
                not item_check(item)
                for item in value
            ):
                errors.append(
                    f"Every item in '{name}' must have type "
                    f"{item_type}."
                )

            minimum_items = specification.get(
                "minItems",
                1 if name in required else 0,
            )
            maximum_items = specification.get("maxItems")
            if len(value) < minimum_items:
                errors.append(
                    f"Argument '{name}' must contain at least "
                    f"{minimum_items} item(s)."
                )
            if (
                isinstance(maximum_items, int)
                and len(value) > maximum_items
            ):
                errors.append(
                    f"Argument '{name}' exceeds maxItems "
                    f"{maximum_items}."
                )
            if (
                item_type == "string"
                and any(
                    isinstance(item, str) and not item.strip()
                    for item in value
                )
            ):
                errors.append(
                    f"Every item in '{name}' must be non-blank."
                )

        enum = specification.get("enum")
        if enum is not None and value not in enum:
            errors.append(
                f"Argument '{name}' must be one of: "
                + ", ".join(map(str, enum))
            )

    return errors


def _predicate_has_column(
    scan: Any,
    column: Any,
) -> bool:
    """Bind only to diagnostics' structured predicate-column output."""

    if not isinstance(scan, dict) or not isinstance(column, str):
        return False

    target = column.strip()
    if not target:
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", target):
        target = target.lower()

    predicate_columns = scan.get("predicate_columns")
    return bool(
        isinstance(predicate_columns, list)
        and target in {
            candidate
            for candidate in predicate_columns
            if isinstance(candidate, str) and candidate
        }
    )


def _proposal_argument_sanity_errors(
    tool: str,
    arguments: Any,
) -> list[str]:
    """Defense-in-depth for callers bypassing the public tool schema."""

    if not isinstance(arguments, dict):
        return ["Proposal arguments must be an object."]

    errors: list[str] = []
    required_strings = {
        "propose_create_index": (
            "query",
            "table",
            "column",
            "reason",
        ),
        "propose_query_rewrite": (
            "original_query",
            "rewritten_query",
            "reason",
        ),
        "propose_analyze_table": (
            "query",
            "table",
            "reason",
        ),
        "propose_terminate_backend": (
            "blocker_backend_start",
            "blocker_xact_start",
            "reason",
        ),
    }.get(tool, ())

    for name in required_strings:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(
                f"Proposal argument '{name}' must be non-blank."
            )

    confidence = arguments.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= confidence <= 1
    ):
        errors.append(
            "Proposal confidence must be a finite number between 0 and 1."
        )

    if tool == "propose_analyze_table":
        columns = arguments.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or any(
                not isinstance(column, str) or not column.strip()
                for column in columns
            )
        ):
            errors.append(
                "Proposal columns must be a non-empty list of "
                "non-blank strings."
            )

    if tool == "propose_terminate_backend":
        pids = (
            arguments.get("blocked_pid"),
            arguments.get("blocker_pid"),
        )
        if any(
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            for pid in pids
        ):
            errors.append("Proposal PIDs must be positive integers.")
        elif pids[0] == pids[1]:
            errors.append("Blocked and blocker PIDs must differ.")

    return errors


def _index_has_leading_column(
    index: Any,
    column: str,
) -> bool:
    if (
        not isinstance(index, dict)
        or not isinstance(column, str)
        or not column
    ):
        return False

    definition = index.get("index_definition")
    if not isinstance(definition, str):
        return False

    opening = definition.find("(")
    if opening < 0:
        return False

    leading_expression = definition[
        opening + 1 :
    ].lstrip()
    quoted = '"' + column.replace('"', '""') + '"'
    return bool(
        re.match(
            rf"(?:{re.escape(quoted)}|{re.escape(column)})"
            rf"(?:\s|,|\))",
            leading_expression,
            re.IGNORECASE,
        )
    )


def _supported_date_rewrite(
    original_query: Any,
    rewritten_query: Any,
    finding: dict,
    column_info: dict,
) -> bool:
    if (
        not isinstance(original_query, str)
        or not isinstance(rewritten_query, str)
        or finding.get("function") != "DATE"
        or column_info.get("data_type")
        != "timestamp without time zone"
    ):
        return False

    column = finding.get("column")
    if not isinstance(column, str):
        return False

    pattern = re.compile(
        rf"\bDATE\s*\(\s*(?P<column>{re.escape(column)})\s*\)"
        rf"\s*=\s*DATE\s*'(?P<date>\d{{4}}-\d{{2}}-\d{{2}})'",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(original_query))
    if len(matches) != 1:
        return False

    match = matches[0]
    date_value = match.group("date")
    replacement = (
        f"{match.group('column')} >= DATE '{date_value}' "
        f"AND {match.group('column')} < DATE '{date_value}' "
        "+ INTERVAL '1 day'"
    )
    expected = (
        original_query[: match.start()]
        + replacement
        + original_query[match.end() :]
    )
    return (
        canonical_query(rewritten_query)
        == canonical_query(expected)
    )


def _stats_support_refresh(result: Any) -> bool:
    if not isinstance(result, dict):
        return False

    modified = result.get("n_mod_since_analyze")
    live_rows = result.get("n_live_tup")
    if (
        isinstance(modified, bool)
        or not isinstance(modified, (int, float))
        or isinstance(live_rows, bool)
        or not isinstance(live_rows, (int, float))
    ):
        return False

    return modified >= max(
        1_000,
        max(live_rows, 1) * 0.1,
    )


@dataclass(frozen=True)
class EvidenceRecord:
    ref: str
    tool: str
    arguments: dict
    result: Any
    status: str
    recorded_at: str
    recorded_monotonic: float
    duration_ms: float


@dataclass
class EvidenceLedger:
    """In-memory evidence and authorization state for one Agent run."""

    records: list[EvidenceRecord] = field(
        default_factory=list
    )
    attempted_call_keys: set[str] = field(
        default_factory=set
    )
    _next_ref: int = 1

    def is_duplicate(
        self,
        tool: str,
        arguments: dict,
    ) -> bool:
        if tool in VOLATILE_OBSERVATION_TOOLS:
            return False

        return (
            canonical_call_key(
                tool,
                arguments,
            )
            in self.attempted_call_keys
        )

    def mark_attempted(
        self,
        tool: str,
        arguments: dict,
    ) -> None:
        if tool in VOLATILE_OBSERVATION_TOOLS:
            return

        self.attempted_call_keys.add(
            canonical_call_key(
                tool,
                arguments,
            )
        )

    def add(
        self,
        *,
        tool: str,
        arguments: dict,
        result: Any,
        status: str,
        duration_ms: float,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            ref=f"ev-{self._next_ref:04d}",
            tool=tool,
            arguments=dict(arguments),
            result=result,
            status=status,
            recorded_at=utc_now(),
            recorded_monotonic=time.monotonic(),
            duration_ms=duration_ms,
        )
        self._next_ref += 1
        self.records.append(record)
        return record

    def successful(
        self,
        tool: str,
        *,
        evidence_cutoff: int | None = None,
    ) -> list[EvidenceRecord]:
        records = (
            self.records
            if evidence_cutoff is None
            else self.records[:evidence_cutoff]
        )
        return [
            record
            for record in records
            if (
                record.tool == tool
                and record.status == "success"
            )
        ]

    def proposal_authorization(
        self,
        tool: str,
        arguments: dict,
        *,
        proposals_allowed: bool,
        allowed_action_types: set[str],
        evidence_cutoff: int | None = None,
        runtime_evidence_ttl_seconds: float = 15.0,
        now_monotonic: float | None = None,
    ) -> tuple[list[str], list[str]]:
        """Return policy errors and evidence refs for a proposal call."""

        errors: list[str] = []
        evidence_refs: list[str] = []
        action_type = PROPOSAL_TOOL_TO_ACTION.get(
            tool
        )

        if action_type is None:
            return [
                f"Unknown proposal tool: {tool}"
            ], []

        if not proposals_allowed:
            errors.append(
                "This Agent run is diagnosis-only; action proposals "
                "are not authorized."
            )

        if action_type not in allowed_action_types:
            errors.append(
                f"Action type {action_type} is not authorized "
                "for this Agent run."
            )

        errors.extend(
            _proposal_argument_sanity_errors(
                tool,
                arguments,
            )
        )

        if errors:
            return errors, evidence_refs

        if tool == "propose_create_index":
            query = canonical_query(arguments.get("query"))
            table = arguments.get("table")
            column = arguments.get("column")
            plan_records = [
                record
                for record in self.successful(
                    "analyze_query",
                    evidence_cutoff=evidence_cutoff,
                )
                if canonical_query(record.arguments.get("query"))
                == query
            ]
            qualifying_plans = []
            for record in plan_records:
                scans = (
                    record.result.get("scan_nodes", [])
                    if isinstance(record.result, dict)
                    else []
                )
                if any(
                    isinstance(scan, dict)
                    and scan.get("table") == table
                    and scan.get("scan_type") in {
                        "Sequential Scan",
                        "Parallel Sequential Scan",
                    }
                    and _predicate_has_column(scan, column)
                    and isinstance(scan.get("rows_examined"), (int, float))
                    and not isinstance(scan.get("rows_examined"), bool)
                    and scan.get("rows_examined") > 0
                    and isinstance(scan.get("selectivity"), (int, float))
                    and not isinstance(scan.get("selectivity"), bool)
                    and 0 <= scan.get("selectivity") <= 0.1
                    for scan in scans
                ):
                    qualifying_plans.append(record)

            index_records = [
                record
                for record in self.successful(
                    "get_indexes",
                    evidence_cutoff=evidence_cutoff,
                )
                if record.arguments.get("table_name") == table
            ]
            latest_indexes = index_records[-1:]
            index_absence_proven = bool(
                latest_indexes
                and isinstance(latest_indexes[0].result, list)
                and not any(
                    _index_has_leading_column(index, column)
                    for index in latest_indexes[0].result
                )
            )

            if not plan_records:
                errors.append(
                    "CREATE_INDEX requires prior analyze_query evidence "
                    "for the same query."
                )
            elif not qualifying_plans:
                errors.append(
                    "CREATE_INDEX requires a selective sequential scan "
                    "on the proposed table and filtered column."
                )
            if not index_records:
                errors.append(
                    "CREATE_INDEX requires prior get_indexes evidence "
                    "for the target table."
                )
            elif not index_absence_proven:
                errors.append(
                    "CREATE_INDEX is not authorized because the latest "
                    "index evidence does not prove the leading column is "
                    "unindexed."
                )

            evidence_refs.extend(
                record.ref
                for record in (
                    qualifying_plans[-1:]
                    + latest_indexes
                )
            )

        elif tool == "propose_query_rewrite":
            original_query = arguments.get("original_query")
            query = canonical_query(original_query)
            plan_records = [
                record
                for record in self.successful(
                    "analyze_query",
                    evidence_cutoff=evidence_cutoff,
                )
                if canonical_query(record.arguments.get("query"))
                == query
            ]
            supported_pair = None
            for plan_record in reversed(plan_records):
                findings = (
                    plan_record.result.get(
                        "non_sargable_findings",
                        [],
                    )
                    if isinstance(plan_record.result, dict)
                    else []
                )
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    info_records = [
                        record
                        for record in self.successful(
                            "get_column_info",
                            evidence_cutoff=evidence_cutoff,
                        )
                        if (
                            record.arguments.get("table_name")
                            == finding.get("table")
                            and record.arguments.get("column_name")
                            == finding.get("column")
                        )
                    ]
                    if (
                        info_records
                        and isinstance(info_records[-1].result, dict)
                        and _supported_date_rewrite(
                            original_query,
                            arguments.get("rewritten_query"),
                            finding,
                            info_records[-1].result,
                        )
                    ):
                        supported_pair = (
                            plan_record,
                            info_records[-1],
                        )
                        break
                if supported_pair:
                    break

            if not plan_records:
                errors.append(
                    "REWRITE_QUERY requires prior analyze_query evidence "
                    "for the same original query."
                )
            elif not supported_pair:
                errors.append(
                    "REWRITE_QUERY requires a matching non-sargable DATE "
                    "finding, timestamp-without-time-zone column evidence, "
                    "and the exact supported half-open-range transform."
                )
            if supported_pair:
                evidence_refs.extend(
                    record.ref
                    for record in supported_pair
                )

        elif tool == "propose_analyze_table":
            query = canonical_query(arguments.get("query"))
            table = arguments.get("table")
            columns = arguments.get("columns")
            requested_columns = (
                columns
                if (
                    isinstance(columns, list)
                    and columns
                    and all(
                        isinstance(column, str) and column
                        for column in columns
                    )
                )
                else []
            )
            if not requested_columns:
                errors.append(
                    "ANALYZE_TABLE requires at least one non-empty "
                    "affected column."
                )

            plan_records = []
            for record in self.successful(
                "analyze_query",
                evidence_cutoff=evidence_cutoff,
            ):
                if (
                    canonical_query(record.arguments.get("query"))
                    != query
                    or not isinstance(record.result, dict)
                    or not record.result.get("cardinality_findings")
                ):
                    continue
                scans = record.result.get("scan_nodes", [])
                if any(
                    isinstance(scan, dict)
                    and scan.get("table") == table
                    and all(
                        _predicate_has_column(scan, column)
                        for column in requested_columns
                    )
                    for scan in scans
                ):
                    plan_records.append(record)

            latest_stats_by_column = {}
            for record in self.successful(
                "get_column_stats",
                evidence_cutoff=evidence_cutoff,
            ):
                column = record.arguments.get("column_name")
                if (
                    record.arguments.get("table_name") == table
                    and column in requested_columns
                ):
                    latest_stats_by_column[column] = record

            unsupported_stats = {
                column
                for column in requested_columns
                if (
                    column not in latest_stats_by_column
                    or not _stats_support_refresh(
                        latest_stats_by_column[column].result
                    )
                )
            }
            if requested_columns and not plan_records:
                errors.append(
                    "ANALYZE_TABLE requires a severe cardinality anomaly "
                    "on the same table and affected filter column(s)."
                )
            if unsupported_stats:
                errors.append(
                    "ANALYZE_TABLE requires recent statistics-health "
                    "evidence showing substantial modifications for: "
                    + ", ".join(sorted(unsupported_stats))
                )

            evidence_refs.extend(
                record.ref
                for record in (
                    plan_records[-1:]
                    + [
                        latest_stats_by_column[column]
                        for column in requested_columns
                        if column in latest_stats_by_column
                    ]
                )
            )

        elif tool == "propose_terminate_backend":
            blocked_pid = arguments.get("blocked_pid")
            blocker_pid = arguments.get("blocker_pid")
            eligible_records = (
                self.records
                if evidence_cutoff is None
                else self.records[:evidence_cutoff]
            )
            lock_records = [
                record
                for record in eligible_records
                if record.tool == "get_lock_waits"
            ]
            post_cutoff_lock_records = (
                []
                if evidence_cutoff is None
                else [
                    record
                    for record in self.records[evidence_cutoff:]
                    if record.tool == "get_lock_waits"
                ]
            )
            latest = lock_records[-1] if lock_records else None
            latest_succeeded = bool(
                latest and latest.status == "success"
            )
            now_value = (
                time.monotonic()
                if now_monotonic is None
                else now_monotonic
            )
            fresh = bool(
                latest_succeeded
                and now_value - latest.recorded_monotonic
                <= runtime_evidence_ttl_seconds
            )
            relationships = (
                latest.result
                if latest_succeeded and isinstance(latest.result, list)
                else []
            )
            identity_bound = all(
                isinstance(arguments.get(field), str)
                and arguments.get(field)
                for field in (
                    "blocker_backend_start",
                    "blocker_xact_start",
                )
            )
            matching = identity_bound and any(
                relationship.get("blocked_pid") == blocked_pid
                and relationship.get("blocker_pid") == blocker_pid
                and relationship.get("blocker_backend_start")
                == arguments.get("blocker_backend_start")
                and relationship.get("blocker_xact_start")
                == arguments.get("blocker_xact_start")
                and relationship.get("blocked_wait_event_type") == "Lock"
                and relationship.get("blocker_state") in {
                    "idle in transaction",
                    "idle in transaction (aborted)",
                }
                for relationship in relationships
                if isinstance(relationship, dict)
            )

            if post_cutoff_lock_records:
                errors.append(
                    "A lock-wait refresh occurred in the same assistant "
                    "response. It invalidates older runtime evidence but "
                    "cannot authorize termination until the next turn."
                )
            elif not latest:
                errors.append(
                    "TERMINATE_BACKEND requires a prior lock-wait snapshot."
                )
            elif not latest_succeeded:
                errors.append(
                    "The newest lock-wait refresh failed; older runtime "
                    "evidence cannot authorize termination."
                )
            elif not fresh:
                errors.append(
                    "TERMINATE_BACKEND requires a fresh lock-wait snapshot."
                )
            elif not identity_bound:
                errors.append(
                    "TERMINATE_BACKEND requires blocker backend and "
                    "transaction start identity from the latest snapshot."
                )
            elif not matching:
                errors.append(
                    "The latest lock-wait snapshot does not contain the "
                    "same waiting pair in the allowed blocker state."
                )
            else:
                evidence_refs.append(latest.ref)

        return errors, list(dict.fromkeys(
            evidence_refs
        ))
