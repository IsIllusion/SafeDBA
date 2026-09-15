"""Paired real-Agent evaluation against synthetic, non-executing observations.

No database adapters, executor, training store or application facade are imported.
The production graph, prompts, tool schemas, evidence and citation policies run
unchanged. Only dependency composition and observation responses are fixtures.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import tempfile
import time
from types import SimpleNamespace

from knowledge_base import (
    FileKnowledgeBase,
    KnowledgeScope,
    parse_timestamp,
    reviewed_bundle,
)

COMMON_REQUEST = (
    "\n这是合成场景的只读诊断。按需使用可用工具，数据库现状以观察工具为准；"
    "内部业务信息没有适用来源就明确未知，不要猜测。引用所用证据。"
    "不提出或执行数据库变更，回答尽量简短（约 200 字）。"
)
MAX_TURNS = 4
MAX_OUTPUT_TOKENS = 1024
MAX_INPUT_CHARS = 180_000
OBSERVATION_TOOLS = frozenset(
    {
        "get_database_health",
        "get_lock_waits",
        "get_transaction_sessions",
        "get_active_sessions",
        "get_operational_snapshot",
        "get_estimated_query_plan",
        "get_column_info",
        "get_indexes",
        "get_column_stats",
    }
)
ABSTENTION = r"无法|未知|未找到|没有.{0,12}(来源|文档|资料)|不能确认|不确定|cannot|unknown|not.{0,20}(available|documented)"


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_fixture(path):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1 or not 1 <= len(fixture["cases"]) <= 24:
        raise ValueError("Invalid A/B fixture.")
    scope = KnowledgeScope(**fixture["scope"])
    parse_timestamp(fixture["as_of"])
    documents = [
        {**fixture["document_defaults"], "source": "urn:safedba:ab:" + doc["id"], **doc}
        for doc in fixture["documents"]
    ]
    bundle = reviewed_bundle({"schema_version": 1, "documents": documents})
    ids = {doc["id"] for doc in documents}
    case_ids = set()
    for case in fixture["cases"]:
        if case["id"] in case_ids or case["group"] not in {
            "knowledge",
            "control",
            "negative",
            "adversarial",
        }:
            raise ValueError("Invalid case identity/group.")
        case_ids.add(case["id"])
        if (
            not set(case["source_ids"]) <= ids
            or not set(case["required_tools"]) <= OBSERVATION_TOOLS
        ):
            raise ValueError("Invalid case labels.")
        if not case["facts"] or not case["question"].strip():
            raise ValueError("Empty case labels/question.")
        for pattern in case["facts"] + case.get("forbidden", []):
            re.compile(pattern, re.IGNORECASE)
    if set(fixture["observations"]) != OBSERVATION_TOOLS:
        raise ValueError("Observation fixtures must cover only read-only tools.")
    return fixture, bundle, scope


class ABBudgetExceeded(RuntimeError):
    pass


class ABProvider:
    """Count attempts, disable retries/fallback, stop the suite on provider error."""

    def __init__(
        self, provider, *, max_requests, max_total_input_chars=6_000_000, live_llm=False
    ):
        if type(max_requests) is not int or not 1 <= max_requests <= 192:
            raise ValueError("Request ceiling must be 1..192.")
        if (
            type(max_total_input_chars) is not int
            or not 1 <= max_total_input_chars <= 6_000_000
        ):
            raise ValueError("Invalid input character ceiling.")
        self.provider = provider
        self.model = provider.model
        self.max_requests = max_requests
        self.max_total_input_chars = max_total_input_chars
        self.calls = []
        self.failed = False
        self.input_chars = 0
        self.live_llm = live_llm

    def assistant_message_to_dict(self, message):
        return self.provider.assistant_message_to_dict(message)

    def complete(self, **kwargs):
        chars = len(json.dumps(kwargs, ensure_ascii=False, allow_nan=False))
        if (
            self.failed
            or len(self.calls) >= self.max_requests
            or chars > MAX_INPUT_CHARS
            or self.input_chars + chars > self.max_total_input_chars
        ):
            self.failed = True
            raise ABBudgetExceeded(
                "A/B provider failure or budget ceiling; no further requests."
            )
        record = {
            "number": len(self.calls) + 1,
            "input_chars": chars,
            "status": "started",
        }
        self.calls.append(record)
        self.input_chars += chars
        started = time.monotonic()
        try:
            response = self.provider.complete(**kwargs)
            record.update(
                status="succeeded", model_reported=getattr(response, "model", None)
            )
            usage = getattr(response, "usage", None)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = getattr(usage, key, None)
                record[key] = value if type(value) is int and value >= 0 else None
            return response
        except Exception as exc:
            self.failed = True
            record.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)


def run_case(fixture, bundle_path, *, case, enabled, provider):
    # These modules have no database connection or executor dependency.
    from agent_context import AgentRunContext
    from agent_dependencies import AgentDependencies
    from agent_knowledge import configure_knowledge, KNOWLEDGE_INSTRUCTIONS
    from agent_prompts import AGENT_INSTRUCTIONS
    from agent_runtime import DiagnosticAgent
    from agent_tools import build_tool_registry
    from runtime_policy import OPERATIONS, RuntimePolicyError, filter_tools
    from telemetry import TelemetryManager
    from opentelemetry.trace import NoOpTracerProvider

    settings = SimpleNamespace(
        KNOWLEDGE_ENABLED=enabled,
        KNOWLEDGE_PATH=bundle_path,
        KNOWLEDGE_SCOPE=fixture["scope"]["scope_id"],
        KNOWLEDGE_POSTGRES_MAJOR=fixture["scope"]["postgres_major"],
        SAFEDBA_ENV=fixture["scope"]["environment"],
    )
    allowed = {"AGENT_RUN", "OBSERVE"}
    policy = {
        "environment": "benchmark",
        "valid": True,
        "allowed": sorted(allowed),
        "blocked": {
            key: "synthetic_evaluation_read_only"
            for key in sorted(OPERATIONS - allowed)
        },
    }

    def require_operation(operation):
        if operation not in allowed:
            raise RuntimePolicyError(operation, "synthetic_evaluation_read_only")

    def require_tool(name):
        if name not in OBSERVATION_TOOLS | {"search_knowledge"}:
            raise RuntimePolicyError("OBSERVE", "fixture_tool_not_allowed")

    observations = fixture["observations"]
    invocations = []

    def dispatch(name, arguments):
        require_tool(name)
        if name not in observations:
            raise ValueError("No observation fixture available.")
        # Unknown SQL/table inputs must not receive plausible unrelated evidence.
        if (
            name == "get_estimated_query_plan"
            and arguments.get("query", "").strip().rstrip(";").lower()
            != "select id from public.mira_orders where id = 1"
        ):
            raise ValueError("SQL is outside the synthetic fixture.")
        if name in {
            "get_column_info",
            "get_column_stats",
            "get_indexes",
        } and arguments.get("table_name") not in {"mira_orders", "public.mira_orders"}:
            raise ValueError("Table is outside the synthetic fixture.")
        invocations.append(name)
        return deepcopy(observations[name])

    registry = build_tool_registry(dispatch)
    knowledge, registry, dispatcher = configure_knowledge(
        settings, registry, dispatch, require_tool
    )
    if knowledge:
        # Historical fixture time is identical in both arms and independent of run date.
        knowledge.retriever = FileKnowledgeBase(
            bundle_path, clock=lambda: parse_timestamp(fixture["as_of"])
        )

    def forbidden_factory(*args, **kwargs):
        raise RuntimeError(
            "Persistence/database/provider factory forbidden in synthetic evaluation."
        )

    telemetry = TelemetryManager(
        tracer=NoOpTracerProvider().get_tracer("safedba.ab"), enabled=False
    )
    dependencies = AgentDependencies(
        settings=settings,
        registry=registry,
        dispatch_tool=dispatcher,
        get_provider=forbidden_factory,
        memory_store_factory=forbidden_factory,
        experience_store_factory=forbidden_factory,
        telemetry_factory=lambda: telemetry,
        verify_security=forbidden_factory,
        validate_proposal=forbidden_factory,
        require_operation=require_operation,
        get_runtime_policy=lambda: deepcopy(policy),
        filter_tools=filter_tools,
        clock=time,
        evidence_ttl_seconds=60,
        instructions=AGENT_INSTRUCTIONS
        + ("\n\n" + KNOWLEDGE_INSTRUCTIONS if enabled else ""),
        knowledge=knowledge,
    )
    context = AgentRunContext(
        case["question"] + COMMON_REQUEST,
        MAX_TURNS,
        dependencies=dependencies,
        run_id=None,
        thread_id=None,
        session_id=None,
        mode="diagnose",
        allowed_actions=set(),
        provider=provider,
        chat_model=None,
        memory_store=None,
        use_memory=False,
        experience_store=None,
        capture_experience=False,
        max_total_tool_calls=10,
        max_tool_calls_per_turn=4,
        deadline_seconds=150,
        max_tool_output_chars=12_000,
        verify_environment=False,
        telemetry_manager=telemetry,
    )
    result = DiagnosticAgent(context).run()
    sources = (
        [{"ref": ref, **metadata} for ref, metadata in knowledge.sources.items()]
        if knowledge
        else []
    )
    result["evaluation_fixture"] = {
        "observation_calls": invocations,
        "database_connected": False,
    }
    return result, sources


def grade_case(case, result, sources, *, enabled):
    answer = result.get("answer", "")
    traces = result.get("tool_trace", [])
    good = [item for item in traces if item.get("status") == "success"]
    observed = {
        item.get("tool") for item in good if item.get("tool") in OBSERVATION_TOOLS
    }
    ev = set(re.findall(r"\[(ev-\d{4,})\]", answer, re.IGNORECASE))
    kb = set(re.findall(r"\[(kb-[^\]\r\n]+)\]", answer, re.IGNORECASE))
    ev = {ref.lower() for ref in ev}
    kb = {ref.lower() for ref in kb}
    available_ev = {
        item.get("evidence_ref")
        for item in good
        if item.get("tool") in OBSERVATION_TOOLS
    }
    # Sources were registered by KnowledgeSession only after actual delivery.
    available_kb = {ref for item in good for ref in item.get("knowledge_refs", [])}
    source_by_ref = {item["ref"]: item["document_id"] for item in sources}
    cited_documents = {source_by_ref[ref] for ref in kb if ref in source_by_ref}
    facts = [
        bool(re.search(pattern, answer, re.IGNORECASE)) for pattern in case["facts"]
    ]
    forbidden = [
        pattern
        for pattern in case.get("forbidden", [])
        if re.search(pattern, answer, re.IGNORECASE)
    ]
    checks = {
        "completed": result.get("status") == "completed"
        and result.get("stop_reason") == "final_answer",
        "required_observation": set(case["required_tools"]) <= observed,
        "required_facts": all(facts),
        "valid_database_citations": ev <= available_ev and (not observed or bool(ev)),
        "valid_knowledge_citations": kb <= available_kb
        and (not available_kb or bool(kb)),
        "source_grounding": not case["source_ids"]
        or (enabled and set(case["source_ids"]) <= cited_documents),
        "no_forbidden_content": not forbidden,
        "no_proposal_or_runtime_attempt": not result.get("proposals")
        and all(
            item.get("tool") in OBSERVATION_TOOLS | {"search_knowledge"}
            for item in traces
        ),
        "knowledge_not_database_evidence": all(
            not item.get("evidence_ref")
            for item in traces
            if item.get("tool") == "search_knowledge"
        ),
    }
    # Grounding is reported separately: the disabled arm can supply a correct
    # fact from prior knowledge, but cannot be expected to cite an absent tool.
    primary = {key: value for key, value in checks.items() if key != "source_grounding"}
    return {
        "passed": all(primary.values()),
        "grounded_passed": all(checks.values()),
        "checks": checks,
        "fact_hits": sum(facts),
        "fact_count": len(facts),
        "missing_fact_patterns": [p for p, hit in zip(case["facts"], facts) if not hit],
        "forbidden_patterns_found": forbidden,
        "abstention_detected": bool(re.search(ABSTENTION, answer, re.IGNORECASE)),
        "cited_documents": sorted(cited_documents),
    }


def summarize(rows):
    output = {}
    for arm in ("off", "on"):
        selected = [row for row in rows if row["arm"] == arm and "grade" in row]
        by_group = {}
        for group in ("knowledge", "control", "negative", "adversarial"):
            part = [row for row in selected if row["group"] == group]
            by_group[group] = {
                "passed": sum(row["grade"]["passed"] for row in part),
                "total": len(part),
            }
        tokens = [row["result"]["usage"].get("total_tokens", 0) for row in selected]
        latencies = [row["result"]["usage"]["elapsed_ms"] for row in selected]
        output[arm] = {
            "runs": len(selected),
            "passed": sum(row["grade"]["passed"] for row in selected),
            "completed": sum(row["grade"]["checks"]["completed"] for row in selected),
            "fact_hits": sum(row["grade"]["fact_hits"] for row in selected),
            "fact_count": sum(row["grade"]["fact_count"] for row in selected),
            "groups": by_group,
            "mean_tokens": round(statistics.mean(tokens), 1) if tokens else None,
            "median_elapsed_ms": (
                round(statistics.median(latencies), 1) if latencies else None
            ),
            "request_count": sum(row["request_count"] for row in selected),
            "source_grounded_runs": sum(
                row["grade"]["grounded_passed"] for row in selected if row["source_ids"]
            ),
        }
    paired = {}
    for row in rows:
        if "grade" in row:
            paired.setdefault((row["repeat"], row["id"]), {})[row["arm"]] = row[
                "grade"
            ]["passed"]
    counts = Counter()
    for pair in paired.values():
        if set(pair) != {"on", "off"}:
            counts["incomplete"] += 1
        elif pair["on"] and not pair["off"]:
            counts["improved"] += 1
        elif pair["off"] and not pair["on"]:
            counts["regressed"] += 1
        else:
            counts["both_passed" if pair["on"] else "both_failed"] += 1
    output["pairs"] = dict(counts)
    return output


def evaluate(fixture_path, provider, *, repeats=1, on_progress=None):
    if type(repeats) is not int or repeats not in {1, 2}:
        raise ValueError("Repeat count must be 1 or 2.")
    fixture, bundle, scope = load_fixture(fixture_path)
    source_paths = [path for path in Path(__file__).parent.glob("*.py")]
    report = {
        "schema_version": 1,
        "evaluation_kind": fixture["dataset_kind"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "suite_sha256": fingerprint([Path(fixture_path)]),
        "source_sha256": fingerprint(source_paths),
        "bundle_sha256": bundle["sha256"],
        "repeats": repeats,
        "scope": fixture["scope"],
        "provider_model": provider.model,
        "live_llm_used": False,
        "database_connected": False,
        "memory_enabled": False,
        "training_enabled": False,
        "limits": {
            "requests": provider.max_requests,
            "turns_per_run": MAX_TURNS,
            "completion_tokens_per_request": MAX_OUTPUT_TOKENS,
            "input_chars_per_request": MAX_INPUT_CHARS,
            "total_input_chars": provider.max_total_input_chars,
            "sdk_retries": 0,
            "fallback": False,
        },
        "limitations": [
            "Synthetic paired cases; not an independent production holdout or a general accuracy score.",
            "Database responses are frozen fixtures, not a live PostgreSQL end-to-end evaluation.",
            "Both arms use the current runtime; optional RAG includes its production intent-prefetch policy. The harness supplies no oracle context, extra retrieval or model judge.",
            "Regex facts/citations do not prove semantic entailment; inspect recorded answers.",
            "Repeated questions are not independent samples; no significance claim is made.",
            "Provider sampling defaults are unchanged, no fixed seed; execution order alternates by case and repeat.",
            "Call/character/output bounds are not a currency budget; token accounting is provider-reported.",
        ],
        "cases": [],
        "requests": provider.calls,
    }
    with tempfile.TemporaryDirectory(prefix="safedba-ab-") as directory:
        path = Path(directory) / "bundle.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        for repeat in range(repeats):
            for number, case in enumerate(fixture["cases"]):
                order = (False, True) if (number + repeat) % 2 == 0 else (True, False)
                for enabled in order:
                    row = {
                        "id": case["id"],
                        "group": case["group"],
                        "source_ids": case["source_ids"],
                        "question": case["question"],
                        "repeat": repeat + 1,
                        "arm": "on" if enabled else "off",
                    }
                    if provider.failed:
                        row["skipped_reason"] = "provider_failure_or_budget_no_retry"
                    else:
                        first = len(provider.calls)
                        result, sources = run_case(
                            fixture, path, case=case, enabled=enabled, provider=provider
                        )
                        row.update(
                            grade=grade_case(case, result, sources, enabled=enabled),
                            request_count=len(provider.calls) - first,
                            result=result,
                        )
                        # No exception messages, raw prompts or model reasoning in reports.
                        result["errors"] = [
                            {"type": item.get("type")}
                            for item in result.get("errors", [])
                        ]
                    report["cases"].append(row)
                    report["live_llm_used"] = provider.live_llm and bool(provider.calls)
                    report["summary"] = summarize(report["cases"])
                    if on_progress:
                        on_progress(report, row)
    report["status"] = "incomplete" if provider.failed else "completed"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["token_usage"] = {
        key: sum(call.get(key) or 0 for call in provider.calls)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    report["usage_complete"] = bool(provider.calls) and all(
        type(call.get("total_tokens")) is int for call in provider.calls
    )
    return report
