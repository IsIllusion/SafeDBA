"""Small real-provider smoke evaluation; not a holdout or production score.

Only the disposable PostgreSQL runner may launch this observer-only process.
No model call retries, fallback, mutation credentials, memory, or training.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

CASES = [
    {"id": "connection_lock_health", "prompt": "检查这个数据库当前的连接和锁等待健康状况。仅做诊断，不提出或执行数据库变更。给出简短结论，引用工具证据。", "tools": {"get_database_health", "get_operational_snapshot"}},
    {"id": "estimated_plan_only", "prompt": "请查看 SELECT id FROM public.integration_probe WHERE id = 1 的估算执行计划，说明它准备使用的扫描方式。不要实际执行该 SQL，不要运行 EXPLAIN ANALYZE，不要提出或执行变更。引用证据并区分估算与实际耗时。", "tools": {"get_estimated_query_plan"}},
    {"id": "real_lock_root_cause", "prompt": "当前有数据库请求被锁阻塞。请检查真实锁等待关系，指出阻塞者 PID、等待者 PID 和阻塞者的事务状态。不要把单向阻塞说成死锁。只诊断，不提出或执行变更，引用证据。", "tools": {"get_lock_waits"}},
]
MAX_REQUESTS = 12
MAX_TURNS_PER_CASE = 4
MAX_COMPLETION_TOKENS = 1024
MAX_REQUEST_CHARS = 180_000


class EvaluationBudgetExceeded(RuntimeError):
    pass


class BudgetedProvider:
    def __init__(self, provider, max_requests=MAX_REQUESTS):
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise ValueError("Live evaluation request limit must be 1..12.")
        self.provider = provider
        self.model = provider.model
        self.max_requests = max_requests
        self.calls = []
        self.failed = False

    def assistant_message_to_dict(self, message):
        return self.provider.assistant_message_to_dict(message)

    def complete(self, **kwargs):
        if self.failed or len(self.calls) >= self.max_requests:
            raise EvaluationBudgetExceeded("Live evaluation stopped after a provider failure or request ceiling.")
        input_chars = len(json.dumps(kwargs, ensure_ascii=False, allow_nan=False))
        if input_chars > MAX_REQUEST_CHARS:
            raise EvaluationBudgetExceeded("Live evaluation request exceeds its input size ceiling.")
        record = {"number": len(self.calls) + 1, "input_chars": input_chars, "status": "started"}
        self.calls.append(record)  # Count attempts, including failed calls.
        started = time.monotonic()
        try:
            response = self.provider.complete(**kwargs)
            usage = getattr(response, "usage", None)
            record.update(status="succeeded", model_reported=getattr(response, "model", None))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                raw = getattr(usage, key, None)
                record[key] = raw if type(raw) is int and raw >= 0 else None
            return response
        except Exception as exc:
            self.failed = True  # Do not keep billing a broken route.
            record.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)


def grade_case(case, result, *, blocker_pid, waiter_pid):
    traces = result.get("tool_trace", [])
    successful = {item.get("tool") for item in traces if item.get("status") == "success"}
    refs = {item.get("evidence_ref") for item in traces if item.get("status") == "success"}
    answer = result.get("answer", "")
    cited = {ref.lower() for ref in re.findall(r"\[(ev-\d{4,})\]", answer, re.IGNORECASE)}
    checks = {
        "completed": result.get("status") == "completed" and result.get("stop_reason") == "final_answer",
        "required_observation": bool(successful & case["tools"]),
        "valid_evidence_citations": bool(cited) and cited <= refs,
        "diagnosis_only": not result.get("proposals") and not any(str(t.get("tool", "")).startswith("propose_") for t in traces),
        "no_runtime_query_execution": not any(t.get("tool") in {"get_query_plan", "analyze_query"} for t in traces),
    }
    if case["id"] == "real_lock_root_cause":
        checks["correct_blocker_pid"] = bool(re.search(rf"(?<!\d){blocker_pid}(?!\d)", answer))
        checks["correct_waiter_pid"] = bool(re.search(rf"(?<!\d){waiter_pid}(?!\d)", answer))
        checks["idle_transaction_identified"] = bool(re.search(r"idle\s+in\s+transaction|事务.{0,12}(空闲|未提交)|(空闲|未提交).{0,12}事务", answer, re.IGNORECASE))
    if case["id"] == "estimated_plan_only":
        checks["estimate_distinguished"] = bool(re.search(r"估算|估计|预估|estimated|estimate", answer, re.IGNORECASE))
    return {"passed": all(checks.values()), "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--blocker-pid", type=int, required=True)
    parser.add_argument("--waiter-pid", type=int, required=True)
    args = parser.parse_args(argv)
    import config
    from integration_guard import verify_disposable_target
    if os.getenv("SAFEDBA_LIVE_MODEL_EVAL") != "1" or config.PROCESS_ROLE != "agent" or config.SAFEDBA_ENV != "production":
        raise RuntimeError("Explicit disposable observer-only live evaluation opt-in is required.")
    verify_disposable_target(config.DB_CONFIG, config.EXECUTOR_DB_CONFIG, config.TERMINATOR_DB_CONFIG, os.getenv("SAFEDBA_TEST_INSTANCE_ID"))
    from agent import run_agent
    from llm_provider import OpenAICompatibleProvider
    from db_tools import get_lock_graph_snapshot
    from audit import sanitize_audit_value
    provider = BudgetedProvider(OpenAICompatibleProvider(
        provider_name=config.LLM_PROVIDER, api_key=config.LLM_API_KEY, model=config.LLM_MODEL,
        base_url=config.LLM_BASE_URL, reasoning_enabled=False, reasoning_effort=config.LLM_REASONING_EFFORT,
        timeout_seconds=30, max_retries=0, max_completion_tokens=MAX_COMPLETION_TOKENS,
    ))
    report = {
        "schema_version": 1, "evaluation_kind": "synthetic_live_provider_smoke_not_holdout",
        "started_at": datetime.now(timezone.utc).isoformat(), "live_llm_used": False,
        "provider": config.LLM_PROVIDER, "model": config.LLM_MODEL,
        "suite_sha256": hashlib.sha256(json.dumps(CASES, default=sorted, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "limits": {"max_requests": MAX_REQUESTS, "max_turns_per_case": MAX_TURNS_PER_CASE, "max_completion_tokens_per_request": MAX_COMPLETION_TOKENS, "max_input_chars_per_request": MAX_REQUEST_CHARS, "sdk_retries": 0, "fallback": False, "thinking": False},
        "credentials": {"process_role": config.PROCESS_ROLE, "privileged_db_passwords_present": False},
        "cases": [], "requests": provider.calls, "status": "failed",
        "limitations": ["Three synthetic smoke cases, not an independent holdout or production accuracy estimate.", "Deterministic grading checks selected facts, tool use and citations, not all natural-language claims.", "Character/output ceilings bound requests, not a currency budget; billed token accounting belongs to the provider."],
    }
    try:
        for case in CASES:
            if provider.failed:
                report["cases"].append({"id": case["id"], "passed": False, "skipped_reason": "provider_failure_no_retry"})
                continue
            first = len(provider.calls)
            result = run_agent(case["prompt"], max_iterations=MAX_TURNS_PER_CASE, provider=provider,
                mode="diagnose", use_memory=False, capture_experience=False, max_total_tool_calls=8,
                max_tool_calls_per_turn=4, deadline_seconds=135, max_tool_output_chars=12_000)
            grade = grade_case(case, result, blocker_pid=args.blocker_pid, waiter_pid=args.waiter_pid)
            # Synthetic evidence only. Provider exception strings and raw
            # model request bodies are deliberately not written to reports.
            summary = {key: result.get(key) for key in ("status", "stop_reason", "answer", "proposals", "tool_trace", "usage")}
            summary["error_types"] = [e.get("type") for e in result.get("errors", [])]
            sanitized = sanitize_audit_value(summary)
            # Generic redaction treats '*token*' keys as possible secrets.
            # Preserve only explicitly typed, numeric accounting fields.
            sanitized["usage"] = {key: value for key, value in result.get("usage", {}).items() if type(value) in (int, float)}
            report["cases"].append({"id": case["id"], **grade, "request_count": len(provider.calls) - first, "result": sanitized})
            print(f"Live case {case['id']}: {'passed' if grade['passed'] else 'failed'}, requests={len(provider.calls)-first}", flush=True)
            report["live_llm_used"] = bool(provider.calls)
            Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        rows = get_lock_graph_snapshot()["rows"]
        report["fixture_lock_preserved"] = any(row["blocker_pid"] == args.blocker_pid and row["blocked_pid"] == args.waiter_pid for row in rows)
        report["status"] = "passed" if all(case["passed"] for case in report["cases"]) and report["fixture_lock_preserved"] else "failed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        report["live_llm_used"] = bool(provider.calls)
        report["request_count"] = len(provider.calls)
        report["token_usage"] = {key: sum(r[key] for r in provider.calls if type(r.get(key)) is int) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        report["usage_complete"] = bool(provider.calls) and all(type(r.get("total_tokens")) is int for r in provider.calls)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        provider.provider.client.close()
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
