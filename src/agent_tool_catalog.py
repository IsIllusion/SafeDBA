"""Agent tool wire contracts; descriptions and schema order are stable API data."""

from tool_registry import ToolRisk


def _parameter(kind, *, description=None, items=None):
    value = {"type": kind}
    if description is not None:
        value["description"] = description
    if items is not None:
        value["items"] = items
    return value


def _tool(name, description, properties, required=None):
    parameters = {"type": "object", "properties": properties}
    if required is not None:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


TOOLS = [
    _tool(
        "analyze_query",
        "Analyze a read-only PostgreSQL SELECT query using a cost-gated, timeout-bounded EXPLAIN ANALYZE and return deterministic structured performance evidence. This tool correctly accounts for parallel execution loops, rows removed by filters, selectivity, buffer usage, execution time, and cardinality estimation accuracy. Use this as the primary tool when diagnosing query performance.",
        {
            "query": _parameter(
                "string", description="The PostgreSQL SELECT query to analyze."
            )
        },
        required=["query"],
    ),
    _tool(
        "get_estimated_query_plan",
        "Run EXPLAIN without ANALYZE for a read-only PostgreSQL SELECT query. This plans but does not execute the query. Use it for an unfamiliar, potentially expensive, or production-sensitive query before requesting runtime evidence.",
        {
            "query": _parameter(
                "string", description="The PostgreSQL SELECT query to plan."
            )
        },
        required=["query"],
    ),
    _tool(
        "get_query_plan",
        "Run EXPLAIN ANALYZE on a read-only PostgreSQL SELECT query and return the JSON execution plan. Use this when investigating query performance.",
        {
            "query": _parameter(
                "string", description="The PostgreSQL SELECT query to analyze."
            )
        },
        required=["query"],
    ),
    _tool(
        "get_indexes",
        "Return all existing indexes for a PostgreSQL table. Use this to verify whether columns involved in filters or joins already have indexes.",
        {"table_name": _parameter("string", description="The PostgreSQL table name.")},
        required=["table_name"],
    ),
    _tool(
        "propose_create_index",
        "Propose creating a PostgreSQL index. This does NOT execute any database modification. Use this only after database evidence shows that a missing index is a strong optimization candidate and get_indexes has confirmed that the index does not already exist.",
        {
            "query": _parameter(
                "string",
                description="The original SELECT query whose performance should be improved.",
            ),
            "table": _parameter("string"),
            "column": _parameter("string"),
            "reason": _parameter(
                "string", description="Evidence-based reason for proposing the index."
            ),
            "confidence": _parameter(
                "number",
                description="Confidence from 0 to 1 that this action should be evaluated.",
            ),
        },
        required=["query", "table", "column", "reason", "confidence"],
    ),
    _tool(
        "propose_query_rewrite",
        "Propose a candidate SQL rewrite to improve PostgreSQL query performance. The candidate may be supported by schema/type evidence and may be expected to preserve semantics, but semantic equivalence is NOT established by this tool. Deterministic executor validation is required to establish result-set equivalence. This tool does NOT execute the rewritten query. Use it when database evidence indicates that the SQL predicate itself prevents an existing index from being used.",
        {
            "original_query": _parameter("string"),
            "rewritten_query": _parameter("string"),
            "reason": _parameter("string"),
            "confidence": _parameter("number"),
        },
        required=["original_query", "rewritten_query", "reason", "confidence"],
    ),
    _tool(
        "get_column_info",
        "Return PostgreSQL schema metadata for a specific column, including its data type. Use this before proposing rewrites whose semantic correctness depends on the column type.",
        {"table_name": _parameter("string"), "column_name": _parameter("string")},
        required=["table_name", "column_name"],
    ),
    _tool(
        "get_column_stats",
        "Return PostgreSQL planner statistics for a specific column together with table-level statistics health such as modifications since the last ANALYZE. Use this when investigating serious cardinality estimation errors.",
        {"table_name": _parameter("string"), "column_name": _parameter("string")},
        required=["table_name", "column_name"],
    ),
    _tool(
        "propose_analyze_table",
        "Propose refreshing PostgreSQL planner statistics for a table or selected columns. This does NOT execute ANALYZE. Use only when severe cardinality estimation error exists and database evidence supports stale planner statistics.",
        {
            "query": _parameter("string"),
            "table": _parameter("string"),
            "columns": _parameter("array", items=_parameter("string")),
            "reason": _parameter("string"),
            "confidence": _parameter("number"),
        },
        required=["query", "table", "columns", "reason", "confidence"],
    ),
    _tool(
        "get_lock_waits",
        "Inspect current PostgreSQL blocking relationships using deterministic runtime evidence from pg_blocking_pids, pg_stat_activity, and pg_locks. Returns blocked sessions, blocking sessions, wait events, transaction ages, queries, and waiting-lock details. Use this as the primary tool when investigating queries or sessions that are currently blocked, waiting on locks, or unexpectedly hanging. This tool is read-only and does not cancel or terminate any backend.",
        {},
    ),
    _tool(
        "propose_terminate_backend",
        "Propose terminating a PostgreSQL blocking backend through the separate deterministic safety and execution layer. This tool does NOT terminate any backend. Use it only when current get_lock_waits evidence identifies a concrete blocked PID and blocker PID, the blocker is idle in transaction, and the user is asking for remediation rather than diagnosis only. TERMINATE_BACKEND is a HIGH-risk action and requires deterministic revalidation and explicit human approval before execution.",
        {
            "blocked_pid": _parameter(
                "integer",
                description="PID of the session currently blocked on a PostgreSQL lock.",
            ),
            "blocker_pid": _parameter(
                "integer",
                description="PID of the backend currently blocking blocked_pid.",
            ),
            "blocker_backend_start": _parameter(
                "string",
                description="Exact blocker_backend_start timestamp from the latest get_lock_waits evidence.",
            ),
            "blocker_xact_start": _parameter(
                "string",
                description="Exact blocker_xact_start timestamp from the latest get_lock_waits evidence.",
            ),
            "reason": _parameter(
                "string",
                description="Evidence-grounded reason for proposing backend termination.",
            ),
            "confidence": _parameter(
                "number",
                description="Confidence from 0 to 1 that the proposal should be evaluated by the deterministic executor.",
            ),
        },
        required=[
            "blocked_pid",
            "blocker_pid",
            "blocker_backend_start",
            "blocker_xact_start",
            "reason",
            "confidence",
        ],
    ),
    _tool(
        "get_database_health",
        "Capture a lightweight runtime health snapshot for the current PostgreSQL database. Returns counts of client sessions, active sessions, currently blocked sessions, idle-in-transaction sessions, long-running queries, long-running transactions, and the ages of the oldest active query and open transaction. Use this as the first triage tool when the user reports general database slowness, degraded responsiveness, or an operational problem without identifying a specific query or lock incident. This is read-only and is not a complete database health assessment.",
        {},
    ),
    _tool(
        "get_operational_snapshot",
        "Collect one broad, read-only PostgreSQL operational snapshot. It combines runtime health, cluster connection capacity, tables ranked by dead-tuple and transaction-ID age pressure, primary/standby replication state and lag, database size, temporary-file counters, deadlock counters, and the largest relations. Use this first for a general incident involving connection exhaustion, VACUUM pressure, replication delay, database growth, or otherwise unexplained degradation. Database and relation sizes are not filesystem free-space measurements. This tool makes no database changes.",
        {},
    ),
    _tool(
        "get_active_sessions",
        "Inspect currently active PostgreSQL client sessions in the current database. Returns PID, user, application, query text, wait event, query age, transaction age, and whether PostgreSQL currently reports blockers for the session. Use this after get_database_health when general runtime triage shows active or long-running queries and additional session-level evidence is required. This tool is read-only and does not cancel or terminate sessions.",
        {},
    ),
    _tool(
        "get_transaction_sessions",
        "Inspect PostgreSQL client sessions with operationally relevant open transactions, including idle-in-transaction sessions and transactions exceeding the configured age threshold. Returns PID, application, transaction state, query text, wait event, transaction age, query age, and current blocking PIDs. Use this after get_database_health when idle-in-transaction sessions or long-running transactions are present. This tool is read-only.",
        {},
    ),
]
_TOOL_CAPABILITIES = {
    "analyze_query": ("query", ToolRisk.READ, 0.0),
    "get_estimated_query_plan": ("query", ToolRisk.READ, None),
    "get_query_plan": ("query", ToolRisk.READ, 0.0),
    "get_indexes": ("catalog", ToolRisk.READ, 30.0),
    "get_column_info": ("catalog", ToolRisk.READ, 30.0),
    "get_column_stats": ("catalog", ToolRisk.READ, 30.0),
    "get_lock_waits": ("runtime", ToolRisk.READ, 0.0),
    "get_database_health": ("runtime", ToolRisk.READ, 0.0),
    "get_operational_snapshot": ("runtime", ToolRisk.READ, 0.0),
    "get_active_sessions": ("runtime", ToolRisk.READ, 0.0),
    "get_transaction_sessions": ("runtime", ToolRisk.READ, 0.0),
    "propose_create_index": ("proposal", ToolRisk.MEDIUM, None),
    "propose_query_rewrite": ("proposal", ToolRisk.LOW, None),
    "propose_analyze_table": ("proposal", ToolRisk.MEDIUM, None),
    "propose_terminate_backend": ("proposal", ToolRisk.HIGH, 0.0),
}
