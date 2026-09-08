"""Typed registry construction and database-tool bindings, independent of config."""

from agent_tool_catalog import TOOLS, _TOOL_CAPABILITIES
from tool_registry import ToolRegistry, ToolSpec

_ROUTES = {
    "get_query_plan": ("get_query_plan", ["query"], []),
    "get_estimated_query_plan": ("get_estimated_query_plan", ["query"], []),
    "get_indexes": ("get_indexes", ["table_name"], []),
    "get_column_info": (
        "get_column_info",
        [],
        [("table_name", "table_name"), ("column_name", "column_name")],
    ),
    "get_column_stats": (
        "get_column_stats",
        [],
        [("table_name", "table_name"), ("column_name", "column_name")],
    ),
    "get_lock_waits": ("get_lock_waits", [], []),
    "get_transaction_sessions": ("get_transaction_sessions", [], []),
    "get_database_health": ("get_database_health", [], []),
    "get_operational_snapshot": ("get_operational_snapshot", [], []),
    "get_active_sessions": ("get_active_sessions", [], []),
    "propose_create_index": (
        "build_create_index_proposal",
        [],
        [
            ("query", "query"),
            ("table", "table"),
            ("column", "column"),
            ("reason", "reason"),
            ("confidence", "confidence"),
        ],
    ),
    "propose_query_rewrite": (
        "build_query_rewrite_proposal",
        [],
        [
            ("original_query", "original_query"),
            ("rewritten_query", "rewritten_query"),
            ("reason", "reason"),
            ("confidence", "confidence"),
        ],
    ),
    "propose_analyze_table": (
        "build_analyze_table_proposal",
        [],
        [
            ("query", "query"),
            ("table", "table"),
            ("columns", "columns"),
            ("reason", "reason"),
            ("confidence", "confidence"),
        ],
    ),
    "propose_terminate_backend": (
        "build_terminate_backend_proposal",
        [],
        [
            ("blocked_pid", "blocked_pid"),
            ("blocker_pid", "blocker_pid"),
            ("blocker_backend_start", "blocker_backend_start"),
            ("blocker_xact_start", "blocker_xact_start"),
            ("reason", "reason"),
            ("confidence", "confidence"),
        ],
    ),
}


def dispatch_builtin_tool(name, arguments, implementations):
    if name == "analyze_query":
        plan = implementations["get_query_plan"](arguments["query"])
        analysis = implementations["analyze_query_plan"](plan)
        analysis["non_sargable_findings"] = implementations[
            "detect_non_sargable_predicates"
        ](analysis)
        analysis["cardinality_findings"] = implementations[
            "detect_cardinality_anomalies"
        ](analysis)
        return analysis
    if name not in _ROUTES:
        raise ValueError(f"Unknown tool: {name}")
    handler, positional, keywords = _ROUTES[name]
    return implementations[handler](
        *(arguments[key] for key in positional),
        **{parameter: arguments[key] for parameter, key in keywords},
    )


def build_tool_registry(dispatcher) -> ToolRegistry:
    registry = ToolRegistry()
    for wire_tool in TOOLS:
        function = wire_tool["function"]
        name = function["name"]
        category, risk, freshness = _TOOL_CAPABILITIES[name]

        def handler(_name=name, **arguments):
            return dispatcher(_name, arguments)

        registry.register(
            ToolSpec(
                name=name,
                description=function["description"],
                parameters=function["parameters"],
                handler=handler,
                category=category,
                risk=risk,
                freshness_seconds=freshness,
                idempotent=category != "runtime",
                side_effect=False,
                requires_approval=False,
            )
        )
    return registry
