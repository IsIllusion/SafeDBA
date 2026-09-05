"""Trusted, deny-only runtime controls, independent of prompts and approval.

The file is re-read at operation boundaries. It is not a sandbox against a
process that can edit its own code/configuration, nor an in-flight SQL cancel.
"""

import importlib
import json
from pathlib import Path


ACTION_TYPES = frozenset({
    "CREATE_INDEX", "ANALYZE_TABLE", "TERMINATE_BACKEND", "REWRITE_QUERY",
})
MUTATIONS = frozenset({
    "CREATE_INDEX", "ANALYZE_TABLE", "TERMINATE_BACKEND", "DROP_INDEX",
})
OPERATIONS = frozenset({
    "AGENT_RUN", "OBSERVE", "EXPLAIN_ANALYZE", "BENCHMARK",
    "COMPARE_QUERY_RESULTS", *ACTION_TYPES, *MUTATIONS,
})
RUNTIME_TOOLS = frozenset({"analyze_query", "get_query_plan"})
ENVIRONMENTS = frozenset({"development", "staging", "production", "benchmark"})
MAX_CONTROL_BYTES = 16_384


class RuntimePolicyError(RuntimeError):
    def __init__(self, operation: str, reason: str):
        self.operation = operation
        self.reason = reason
        super().__init__(f"{operation} blocked by runtime policy: {reason}")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate control key.")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError("Non-finite control value.")


def _read_controls(settings) -> dict:
    path = getattr(settings, "RUNTIME_CONTROLS_PATH", None)
    required = getattr(settings, "RUNTIME_CONTROLS_REQUIRED", False)
    if not path:
        if required:
            raise ValueError("Required controls path is missing.")
        return {}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_CONTROL_BYTES + 1)
    except FileNotFoundError:
        if required:
            raise
        return {}
    if len(raw) > MAX_CONTROL_BYTES:
        raise ValueError("Runtime controls exceed their size bound.")
    controls = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    boolean_keys = {
        "disable_agent", "disable_mutations", "disable_runtime_analysis",
        "disable_benchmarks",
    }
    if (
        not isinstance(controls, dict)
        or set(controls) - {"version", "disabled_actions", *boolean_keys}
        or type(controls.get("version")) is not int
        or controls["version"] != 1
        or any(type(controls[key]) is not bool for key in boolean_keys & controls.keys())
    ):
        raise ValueError("Invalid runtime controls schema.")
    actions = controls.get("disabled_actions", [])
    if (
        not isinstance(actions, list)
        or any(not isinstance(item, str) or item not in ACTION_TYPES for item in actions)
        or len(actions) != len(set(actions))
    ):
        raise ValueError("Invalid disabled action list.")
    return controls


def get_runtime_policy() -> dict:
    """Return a secret-free snapshot. Invalid controls deny every operation."""
    settings = importlib.import_module("config")
    environment = getattr(settings, "SAFEDBA_ENV", "development")
    try:
        if environment not in ENVIRONMENTS:
            raise ValueError("Unknown runtime environment.")
        controls = _read_controls(settings)
    except (OSError, ValueError, RecursionError):
        return {
            "environment": environment, "valid": False,
            "allowed": [],
            "blocked": {op: "runtime_controls_invalid_or_unavailable" for op in sorted(OPERATIONS)},
        }

    blocked = {}
    if environment == "production":
        # Existing index/statistics/rewrite workflows rely on before/after
        # workload execution. They are not production deployment workflows.
        blocked.update({op: "production_observe_only" for op in {
            "EXPLAIN_ANALYZE", "BENCHMARK", "COMPARE_QUERY_RESULTS",
            "CREATE_INDEX", "DROP_INDEX", "ANALYZE_TABLE", "REWRITE_QUERY",
        }})
    if not getattr(settings, "ALLOW_RUNTIME_ANALYSIS", environment != "production") or controls.get("disable_runtime_analysis"):
        blocked.update({op: "runtime_analysis_disabled" for op in {
            "EXPLAIN_ANALYZE", "BENCHMARK", "COMPARE_QUERY_RESULTS", "REWRITE_QUERY",
            "CREATE_INDEX", "ANALYZE_TABLE",
        }})
    if not getattr(settings, "ALLOW_BENCHMARK", environment in {"development", "benchmark"}) or controls.get("disable_benchmarks"):
        blocked.update({op: "benchmarks_disabled" for op in {"BENCHMARK", "REWRITE_QUERY", "CREATE_INDEX", "ANALYZE_TABLE"}})
    if not getattr(settings, "ENABLE_MUTATIONS", environment != "production") or controls.get("disable_mutations"):
        blocked.update({op: "mutations_disabled" for op in MUTATIONS})
    for action in ACTION_TYPES:
        enabled_by_default = action != "TERMINATE_BACKEND" or environment != "production"
        if not getattr(settings, f"ENABLE_{action}", enabled_by_default) or action in controls.get("disabled_actions", []):
            blocked[action] = "action_disabled"
            if action == "CREATE_INDEX":
                blocked["DROP_INDEX"] = "action_disabled"
    if not getattr(settings, "ENABLE_AGENT", True) or controls.get("disable_agent"):
        blocked.update({op: "agent_disabled" for op in OPERATIONS})
    return {
        "environment": environment, "valid": True,
        "allowed": sorted(OPERATIONS - blocked.keys()),
        "blocked": dict(sorted(blocked.items())),
    }


def require_operation(operation: str) -> None:
    if operation not in OPERATIONS:
        raise RuntimePolicyError(operation, "unknown_operation")
    policy = get_runtime_policy()
    if operation in policy["blocked"]:
        raise RuntimePolicyError(operation, policy["blocked"][operation])


def require_tool(name: str) -> None:
    require_operation("AGENT_RUN")
    if name in RUNTIME_TOOLS:
        require_operation("EXPLAIN_ANALYZE")


def filter_tools(tools: list[dict], policy: dict) -> list[dict]:
    if "AGENT_RUN" in policy["blocked"]:
        return []
    if "EXPLAIN_ANALYZE" in policy["blocked"]:
        return [tool for tool in tools if tool["function"]["name"] not in RUNTIME_TOOLS]
    return tools


if __name__ == "__main__":
    print(json.dumps(get_runtime_policy(), ensure_ascii=False, indent=2))
