from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mcp_adapter import (  # noqa: E402
    MCPPolicyError,
    READ_ONLY_TOOL_ALLOWLIST,
    SafeDBAMCPFacade,
)
from tool_registry import (  # noqa: E402
    ToolRegistry,
    ToolRisk,
    ToolSpec,
)


def make_spec(
    name="get_database_health",
    *,
    handler=lambda: {"active_sessions": 1},
    parameters=None,
    category="runtime",
    risk=ToolRisk.READ,
    side_effect=False,
    requires_approval=False,
):
    return ToolSpec(
        name=name,
        description="Read test database evidence.",
        parameters=parameters or {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=handler,
        category=category,
        risk=risk,
        freshness_seconds=0,
        idempotent=True,
        side_effect=side_effect,
        requires_approval=requires_approval,
    )


class FakeStore:
    def __init__(self):
        self.loaded = []

    def list_incidents(self, *, limit):
        return [{
            "incident_id": "incident-1",
            "state": "COMPLETED",
            "limit_seen": limit,
        }]

    def load_incident(self, incident_id):
        self.loaded.append(incident_id)
        return {
            "incident_id": incident_id,
            "private": "must-not-leak",
        }


def make_facade(
    registry,
    *,
    verifier=lambda: {"status": "ok"},
    runner=None,
    store=None,
):
    resolved_store = store or FakeStore()
    return SafeDBAMCPFacade(
        registry=registry,
        security_verifier=verifier,
        agent_runner=(
            runner
            or (
                lambda request, **kwargs: {
                    "status": "completed",
                    "mode": "diagnose",
                    "answer": request,
                    "proposals": [],
                }
            )
        ),
        incident_store_factory=lambda: resolved_store,
        incident_view=lambda value: {
            "incident_id": value["incident_id"],
        },
    )


class MCPFacadeDatabaseBoundaryTests(unittest.TestCase):
    def test_read_tool_attests_before_dispatch_and_redacts_secrets(self):
        events = []
        registry = ToolRegistry([
            make_spec(
                handler=lambda: (
                    events.append("dispatch")
                    or {
                        "active_sessions": 1,
                        "password": "database-secret",
                    }
                ),
            )
        ])
        facade = make_facade(
            registry,
            verifier=lambda: events.append("attest"),
        )

        result = facade.call_database_tool(
            "get_database_health"
        )

        self.assertEqual(events, ["attest", "dispatch"])
        self.assertEqual(result["runtime_security"], "verified")
        self.assertEqual(result["data"]["password"], "[REDACTED]")

    def test_rejects_internal_tool_outside_fixed_allowlist(self):
        registry = ToolRegistry([
            make_spec(name="propose_terminate_backend")
        ])
        facade = make_facade(registry)

        with self.assertRaisesRegex(
            MCPPolicyError,
            "not exposed",
        ):
            facade.call_database_tool(
                "propose_terminate_backend"
            )

    def test_rejects_allowlisted_name_when_metadata_drifts(self):
        registry = ToolRegistry([
            make_spec(
                risk=ToolRisk.LOW,
                side_effect=True,
            )
        ])
        facade = make_facade(registry)

        with self.assertRaisesRegex(
            MCPPolicyError,
            "read-only policy",
        ):
            facade.call_database_tool(
                "get_database_health"
            )

    def test_validates_arguments_before_attestation_or_dispatch(self):
        events = []
        registry = ToolRegistry([
            make_spec(
                name="get_indexes",
                parameters={
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string"},
                    },
                    "required": ["table_name"],
                    "additionalProperties": False,
                },
                category="catalog",
                handler=lambda **kwargs: events.append("dispatch"),
            )
        ])
        facade = make_facade(
            registry,
            verifier=lambda: events.append("attest"),
        )

        with self.assertRaisesRegex(
            MCPPolicyError,
            "Missing required arguments",
        ):
            facade.call_database_tool("get_indexes", {})

        self.assertEqual(events, [])


class MCPFacadeAgentAndIncidentTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry([
            make_spec()
        ])

    def test_diagnosis_is_forced_to_diagnose_mode_with_opt_in_memory(self):
        calls = []

        def runner(request, **kwargs):
            calls.append((request, kwargs))
            return {
                "run_id": "run-1",
                "session_id": kwargs.get("session_id"),
                "status": "completed",
                "stop_reason": "final_answer",
                "mode": kwargs["mode"],
                "answer": "diagnosis",
                "proposals": [],
                "tool_trace": [],
                "errors": [],
            }

        facade = make_facade(
            self.registry,
            runner=runner,
        )

        result = facade.diagnose_database(
            "Why is the database slow?",
            session_id="customer-a",
        )

        self.assertEqual(
            calls,
            [(
                "Why is the database slow?",
                {
                    "mode": "diagnose",
                    "thread_id": "mcp",
                    "session_id": "customer-a",
                },
            )],
        )
        self.assertEqual(result["mode"], "diagnose")
        self.assertNotIn("proposals", result)

    def test_fail_closed_if_diagnosis_returns_a_proposal(self):
        facade = make_facade(
            self.registry,
            runner=lambda request, **kwargs: {
                "mode": "diagnose",
                "proposals": [{"type": "TERMINATE_BACKEND"}],
            },
        )

        with self.assertRaisesRegex(
            MCPPolicyError,
            "cannot return action proposals",
        ):
            facade.diagnose_database("Resolve all locks")

    def test_incident_access_uses_only_public_view(self):
        store = FakeStore()
        facade = make_facade(
            self.registry,
            store=store,
        )

        listed = facade.list_incidents(limit=7)
        incident = facade.get_incident("incident-1")

        self.assertEqual(listed["count"], 1)
        self.assertEqual(
            listed["incidents"][0]["limit_seen"],
            7,
        )
        self.assertEqual(incident, {"incident_id": "incident-1"})
        self.assertEqual(store.loaded, ["incident-1"])

    def test_capability_document_has_no_action_tools(self):
        facade = make_facade(self.registry)

        capabilities = facade.capabilities()

        self.assertEqual(
            set(capabilities["database_tools"]),
            set(READ_ONLY_TOOL_ALLOWLIST),
        )
        self.assertFalse(any(
            name.startswith("propose_")
            for name in capabilities["database_tools"]
        ))
        self.assertEqual(
            capabilities["agent_modes"],
            ["diagnose"],
        )


if __name__ == "__main__":
    unittest.main()
