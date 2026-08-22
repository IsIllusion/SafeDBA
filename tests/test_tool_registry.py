import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from tool_registry import (  # noqa: E402
    DuplicateToolError,
    InvalidToolArgumentsError,
    InvalidToolSpecError,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    UnknownToolError,
)


def make_spec(
    name="inspect_database",
    handler=lambda database: database,
    **overrides,
):
    values = {
        "name": name,
        "description": "Inspect a database safely.",
        "parameters": {
            "type": "object",
            "properties": {
                "database": {
                    "type": "string",
                },
            },
            "required": ["database"],
            "additionalProperties": False,
        },
        "handler": handler,
        "category": "database.read",
        "risk": ToolRisk.READ,
        "freshness_seconds": 5,
        "idempotent": True,
        "side_effect": False,
        "requires_approval": False,
    }
    values.update(overrides)
    return ToolSpec(**values)


class ToolSpecTests(unittest.TestCase):
    def test_normalizes_risk_and_freshness(self):
        spec = make_spec(
            risk="read",
            freshness_seconds=3,
        )

        self.assertIs(spec.risk, ToolRisk.READ)
        self.assertEqual(spec.freshness_seconds, 3.0)
        self.assertFalse(spec.action)

    def test_renders_chat_completions_schema(self):
        spec = make_spec()

        rendered = spec.to_chat_completions_tool()

        self.assertEqual(rendered["type"], "function")
        self.assertEqual(
            rendered["function"]["name"],
            "inspect_database",
        )
        self.assertEqual(
            rendered["function"]["parameters"]["required"],
            ["database"],
        )
        self.assertNotIn(
            "risk",
            rendered["function"],
        )

    def test_schema_is_immutable_and_render_is_detached(self):
        original = {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
            },
            "required": ["database"],
        }
        spec = make_spec(parameters=original)
        original["required"].clear()

        with self.assertRaises(TypeError):
            spec.parameters["new"] = True

        first = spec.to_chat_completions_tool()
        first["function"]["parameters"]["required"].clear()
        second = spec.to_chat_completions_tool()
        self.assertEqual(
            second["function"]["parameters"]["required"],
            ["database"],
        )

    def test_property_names_may_match_schema_keywords(self):
        spec = make_spec(
            parameters={
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "required": {"type": "boolean"},
                },
                "required": ["type"],
            },
        )

        self.assertIn(
            "type",
            spec.json_schema["properties"],
        )

    def test_rejects_invalid_names_descriptions_handlers_and_category(self):
        invalid = [
            {"name": "bad name"},
            {"name": "1bad"},
            {"name": "x" * 65},
            {"description": ""},
            {"description": " padded "},
            {"handler": object()},
            {"category": "bad category"},
        ]

        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(InvalidToolSpecError):
                    make_spec(**overrides)

    def test_rejects_invalid_risk_and_freshness(self):
        for value in ["CRITICAL", 3, None]:
            with self.subTest(risk=value):
                with self.assertRaises(InvalidToolSpecError):
                    make_spec(risk=value)

        for value in [-1, True, math.inf, math.nan, "5"]:
            with self.subTest(freshness=value):
                with self.assertRaises(InvalidToolSpecError):
                    make_spec(freshness_seconds=value)

    def test_rejects_inconsistent_capability_metadata(self):
        with self.assertRaises(InvalidToolSpecError):
            make_spec(
                side_effect=True,
                risk=ToolRisk.READ,
            )
        with self.assertRaises(InvalidToolSpecError):
            make_spec(
                requires_approval=True,
                side_effect=False,
            )
        with self.assertRaises(InvalidToolSpecError):
            make_spec(idempotent=1)

    def test_rejects_malformed_parameter_schemas(self):
        invalid = [
            [],
            {},
            {"type": "string", "properties": {}},
            {"type": "object"},
            {
                "type": "object",
                "properties": [],
            },
            {
                "type": "object",
                "properties": {"x": {"type": "unknown"}},
            },
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["missing"],
            },
            {
                "type": "object",
                "properties": {},
                "required": ["x", "x"],
            },
            {
                "type": "object",
                "properties": {},
                "example": math.inf,
            },
        ]

        for parameters in invalid:
            with self.subTest(parameters=parameters):
                with self.assertRaises(InvalidToolSpecError):
                    make_spec(parameters=parameters)


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.read_spec = make_spec()
        self.low_action = make_spec(
            name="refresh_stats",
            handler=lambda table: f"refreshed:{table}",
            parameters={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                },
                "required": ["table"],
            },
            category="database.maintenance",
            risk=ToolRisk.LOW,
            freshness_seconds=None,
            side_effect=True,
            idempotent=True,
        )
        self.high_action = make_spec(
            name="terminate_backend",
            handler=lambda pid: {"terminated": pid},
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer"},
                },
                "required": ["pid"],
            },
            category="database.incident",
            risk="HIGH",
            freshness_seconds=0,
            side_effect=True,
            idempotent=False,
            requires_approval=True,
        )
        self.registry = ToolRegistry([
            self.read_spec,
            self.low_action,
            self.high_action,
        ])

    def test_register_rejects_duplicates_and_non_specs(self):
        with self.assertRaises(DuplicateToolError):
            self.registry.register(self.read_spec)
        with self.assertRaises(InvalidToolSpecError):
            self.registry.register({})

        self.assertEqual(len(self.registry), 3)
        self.assertIn("inspect_database", self.registry)

    def test_get_and_dispatch_by_name(self):
        self.assertIs(
            self.registry.get("refresh_stats"),
            self.low_action,
        )
        self.assertEqual(
            self.registry.dispatch(
                "refresh_stats",
                {"table": "orders"},
            ),
            "refreshed:orders",
        )
        self.assertEqual(
            self.registry.dispatch(
                "terminate_backend",
                {"pid": 42},
            ),
            {"terminated": 42},
        )

    def test_dispatch_allows_none_for_no_argument_handler(self):
        registry = ToolRegistry([
            make_spec(
                name="health",
                handler=lambda: "ok",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )
        ])

        self.assertEqual(
            registry.dispatch("health"),
            "ok",
        )

    def test_dispatch_rejects_unknown_and_malformed_arguments(self):
        with self.assertRaises(UnknownToolError):
            self.registry.dispatch("missing", {})
        for arguments in [[], "database", {1: "value"}]:
            with self.subTest(arguments=arguments):
                with self.assertRaises(InvalidToolArgumentsError):
                    self.registry.dispatch(
                        "inspect_database",
                        arguments,
                    )

    def test_filters_action_risk_approval_and_category(self):
        self.assertEqual(
            tuple(
                spec.name
                for spec in self.registry.filter_tools(
                    action=True,
                )
            ),
            ("refresh_stats", "terminate_backend"),
        )
        self.assertEqual(
            tuple(
                spec.name
                for spec in self.registry.filter_tools(
                    risk=["LOW", ToolRisk.HIGH],
                    requires_approval=False,
                )
            ),
            ("refresh_stats",),
        )
        self.assertEqual(
            tuple(
                spec.name
                for spec in self.registry.filter_tools(
                    action=True,
                    risk="high",
                    requires_approval=True,
                    category="database.incident",
                )
            ),
            ("terminate_backend",),
        )
        self.assertEqual(
            self.registry.filter_tools(risk=[]),
            (),
        )

    def test_chat_schema_honors_capability_filters(self):
        rendered = self.registry.to_chat_completions_tools(
            action=True,
            requires_approval=True,
        )

        self.assertEqual(len(rendered), 1)
        self.assertEqual(
            rendered[0]["function"]["name"],
            "terminate_backend",
        )

    def test_filter_rejects_invalid_values(self):
        invalid = [
            {"action": "yes"},
            {"requires_approval": 1},
            {"category": 3},
            {"risk": "CRITICAL"},
            {"risk": 3},
        ]

        for filters in invalid:
            with self.subTest(filters=filters):
                with self.assertRaises(ValueError):
                    self.registry.filter_tools(**filters)

    def test_names_preserve_registration_order(self):
        self.assertEqual(
            self.registry.names,
            (
                "inspect_database",
                "refresh_stats",
                "terminate_backend",
            ),
        )


if __name__ == "__main__":
    unittest.main()
