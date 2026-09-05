import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

if MCP_AVAILABLE:
    from mcp import Client
    from mcp_server import create_mcp_server


EXPECTED_TOOLS = {
    "analyze_query",
    "get_estimated_query_plan",
    "get_query_plan",
    "get_indexes",
    "get_column_info",
    "get_column_stats",
    "get_lock_waits",
    "get_database_health",
    "get_operational_snapshot",
    "get_active_sessions",
    "get_transaction_sessions",
    "diagnose_database",
    "list_incidents",
    "get_incident",
}


class FakeFacade:
    def __init__(self):
        self.calls = []

    def call_database_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {
            "status": "success",
            "tool": name,
            "runtime_security": "verified",
            "data": {"active_sessions": 2},
        }

    def diagnose_database(self, request, *, session_id=None):
        return {
            "status": "completed",
            "mode": "diagnose",
            "answer": request,
            "session_id": session_id,
        }

    def list_incidents(self, *, limit=20):
        return {"count": 0, "incidents": [], "limit": limit}

    def get_incident(self, incident_id):
        return {"incident_id": incident_id, "state": "COMPLETED"}

    def capabilities(self):
        return {
            "server": "SafeDBA",
            "transport": "stdio",
            "database_access": "read_only",
        }


@unittest.skipUnless(
    MCP_AVAILABLE,
    "MCP SDK is not installed in this interpreter.",
)
class MCPServerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_discovery_call_resource_and_prompt(self):
        facade = FakeFacade()
        server = create_mcp_server(facade)

        async with Client(server) as client:
            tools = await client.list_tools()
            tool_names = {
                item.name
                for item in tools.tools
            }
            self.assertEqual(tool_names, EXPECTED_TOOLS)
            self.assertFalse(any(
                forbidden in name
                for name in tool_names
                for forbidden in (
                    "propose",
                    "terminate",
                    "execute",
                    "approve",
                    "resume",
                )
            ))

            health = await client.call_tool(
                "get_database_health"
            )
            self.assertFalse(health.is_error)
            self.assertEqual(
                health.structured_content["tool"],
                "get_database_health",
            )
            self.assertEqual(
                facade.calls,
                [("get_database_health", None)],
            )

            resources = await client.list_resources()
            resource_uris = {
                str(item.uri)
                for item in resources.resources
            }
            self.assertEqual(
                resource_uris,
                {
                    "safedba://capabilities",
                    "safedba://incidents/recent",
                },
            )
            capabilities = await client.read_resource(
                "safedba://capabilities"
            )
            capability_payload = json.loads(
                capabilities.contents[0].text
            )
            self.assertEqual(
                capability_payload["database_access"],
                "read_only",
            )

            prompts = await client.list_prompts()
            self.assertEqual(
                {item.name for item in prompts.prompts},
                {
                    "diagnose_slow_query",
                    "triage_lock_contention",
                },
            )
            prompt = await client.get_prompt(
                "diagnose_slow_query",
                {"query": "SELECT 1"},
            )
            self.assertIn(
                "SELECT 1",
                prompt.messages[0].content.text,
            )


if __name__ == "__main__":
    unittest.main()
