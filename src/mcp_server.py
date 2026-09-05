"""Local stdio MCP server for SafeDBA's read-only public capabilities."""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from mcp_adapter import SafeDBAMCPFacade


SERVER_NAME = "safedba"
SERVER_VERSION = "1.0.0"


def create_mcp_server(
    facade: SafeDBAMCPFacade | None = None,
) -> MCPServer:
    """Build an in-process-testable MCP server with a narrow allowlist."""

    api = facade or SafeDBAMCPFacade()
    server = MCPServer(
        name=SERVER_NAME,
        title="SafeDBA Read-Only Database Agent",
        version=SERVER_VERSION,
        description=(
            "Read-only PostgreSQL evidence, diagnosis-only SafeDBA runs, "
            "and public IncidentWorkflow state over local stdio."
        ),
        instructions=(
            "Use database evidence tools before making factual claims. "
            "This server cannot propose, approve, resume, or execute database "
            "changes. Use SafeDBA's native control plane for remediation."
        ),
    )

    database_read = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    metadata_read = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    local_state_read = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(
        name="analyze_query",
        description=(
            "Analyze a read-only PostgreSQL SELECT with SafeDBA's cost gate, "
            "timeout, EXPLAIN ANALYZE, and deterministic plan diagnostics."
        ),
        annotations=database_read,
        structured_output=True,
    )
    def analyze_query(query: str) -> dict[str, Any]:
        return api.call_database_tool(
            "analyze_query",
            {"query": query},
        )

    @server.tool(
        name="get_estimated_query_plan",
        description=(
            "Plan a read-only PostgreSQL SELECT with EXPLAIN only; the query "
            "is not executed. Prefer this before runtime analysis of an "
            "unfamiliar or potentially expensive query."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_estimated_query_plan(query: str) -> dict[str, Any]:
        return api.call_database_tool(
            "get_estimated_query_plan",
            {"query": query},
        )

    @server.tool(
        name="get_query_plan",
        description=(
            "Run SafeDBA's bounded EXPLAIN ANALYZE for a read-only PostgreSQL "
            "SELECT and return the JSON execution plan."
        ),
        annotations=database_read,
        structured_output=True,
    )
    def get_query_plan(query: str) -> dict[str, Any]:
        return api.call_database_tool(
            "get_query_plan",
            {"query": query},
        )

    @server.tool(
        name="get_indexes",
        description="Return existing PostgreSQL indexes for a table.",
        annotations=metadata_read,
        structured_output=True,
    )
    def get_indexes(table_name: str) -> dict[str, Any]:
        return api.call_database_tool(
            "get_indexes",
            {"table_name": table_name},
        )

    @server.tool(
        name="get_column_info",
        description=(
            "Return schema metadata, including data type, for one PostgreSQL "
            "table column."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_column_info(
        table_name: str,
        column_name: str,
    ) -> dict[str, Any]:
        return api.call_database_tool(
            "get_column_info",
            {
                "table_name": table_name,
                "column_name": column_name,
            },
        )

    @server.tool(
        name="get_column_stats",
        description=(
            "Return planner and table statistics health for one PostgreSQL "
            "column."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_column_stats(
        table_name: str,
        column_name: str,
    ) -> dict[str, Any]:
        return api.call_database_tool(
            "get_column_stats",
            {
                "table_name": table_name,
                "column_name": column_name,
            },
        )

    @server.tool(
        name="get_lock_waits",
        description=(
            "Inspect current PostgreSQL lock wait and blocker relationships. "
            "This never cancels or terminates a backend."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_lock_waits() -> dict[str, Any]:
        return api.call_database_tool(
            "get_lock_waits"
        )

    @server.tool(
        name="get_database_health",
        description=(
            "Capture a lightweight read-only PostgreSQL runtime health "
            "snapshot for initial incident triage."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_database_health() -> dict[str, Any]:
        return api.call_database_tool(
            "get_database_health"
        )

    @server.tool(
        name="get_operational_snapshot",
        description=(
            "Collect broad read-only PostgreSQL evidence for connection "
            "capacity, VACUUM pressure, replication, runtime health, and "
            "PostgreSQL-visible storage usage."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_operational_snapshot() -> dict[str, Any]:
        return api.call_database_tool(
            "get_operational_snapshot"
        )

    @server.tool(
        name="get_active_sessions",
        description=(
            "Inspect active PostgreSQL client sessions without cancelling or "
            "terminating them."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_active_sessions() -> dict[str, Any]:
        return api.call_database_tool(
            "get_active_sessions"
        )

    @server.tool(
        name="get_transaction_sessions",
        description=(
            "Inspect operationally relevant open PostgreSQL transactions, "
            "including idle-in-transaction and long-running sessions."
        ),
        annotations=metadata_read,
        structured_output=True,
    )
    def get_transaction_sessions() -> dict[str, Any]:
        return api.call_database_tool(
            "get_transaction_sessions"
        )

    @server.tool(
        name="diagnose_database",
        description=(
            "Run the SafeDBA Agent in diagnosis-only mode. It may collect "
            "read-only evidence but cannot emit or execute action proposals. "
            "Pass a stable session_id to opt into persistent Agent memory."
        ),
        annotations=database_read,
        structured_output=True,
    )
    def diagnose_database(
        request: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return api.diagnose_database(
            request,
            session_id=session_id,
        )

    @server.tool(
        name="list_incidents",
        description=(
            "List durable IncidentWorkflow summaries. This cannot create, "
            "approve, resume, or execute an incident."
        ),
        annotations=local_state_read,
        structured_output=True,
    )
    def list_incidents(limit: int = 20) -> dict[str, Any]:
        return api.list_incidents(limit=limit)

    @server.tool(
        name="get_incident",
        description=(
            "Read SafeDBA's reduced public view of one durable incident."
        ),
        annotations=local_state_read,
        structured_output=True,
    )
    def get_incident(incident_id: str) -> dict[str, Any]:
        return api.get_incident(incident_id)

    @server.resource(
        "safedba://capabilities",
        name="safedba_capabilities",
        description=(
            "Machine-readable SafeDBA MCP capability and exclusion boundary."
        ),
        mime_type="application/json",
    )
    def capabilities_resource() -> str:
        return json.dumps(
            api.capabilities(),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )

    @server.resource(
        "safedba://incidents/recent",
        name="safedba_recent_incidents",
        description="The 20 most recently updated incident summaries.",
        mime_type="application/json",
    )
    def recent_incidents_resource() -> str:
        return json.dumps(
            api.list_incidents(limit=20),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )

    @server.prompt(
        name="diagnose_slow_query",
        description=(
            "Prepare a read-only SafeDBA investigation of a slow query."
        ),
    )
    def diagnose_slow_query(query: str) -> str:
        return (
            "Diagnose this PostgreSQL query without proposing or executing "
            "changes. Start with get_estimated_query_plan. Use analyze_query "
            "only if the estimated plan is safe enough for bounded runtime "
            "analysis. Cite tool evidence and state uncertainty.\n\nQuery:\n"
            + query
        )

    @server.prompt(
        name="triage_lock_contention",
        description=(
            "Prepare a read-only investigation of current lock contention."
        ),
    )
    def triage_lock_contention(
        scope: str = "the current database",
    ) -> str:
        return (
            "Investigate lock contention in "
            + scope
            + ". Use get_database_health and get_lock_waits, identify each "
            "blocked/blocker relationship, and provide diagnosis only. Never "
            "cancel or terminate a backend through MCP."
        )

    return server


def main() -> None:
    """Run the server on local standard input/output."""

    create_mcp_server().run("stdio")


if __name__ == "__main__":
    main()
