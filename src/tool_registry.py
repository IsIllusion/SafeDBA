"""Typed tool metadata and dispatch for Agent tool calls.

The registry deliberately has no dependency on an LLM SDK.  It owns the
capability metadata used by policy code and can render the small wire format
expected by Chat Completions APIs at the boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import copy
import json
import math
import re
from threading import RLock
from types import MappingProxyType
from typing import Any


_TOOL_NAME_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
)
_CATEGORY_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$"
)
_JSON_TYPES = frozenset({
    "array",
    "boolean",
    "integer",
    "null",
    "number",
    "object",
    "string",
})


class ToolRisk(str, Enum):
    """Risk classification used by tool-selection and approval policy."""

    READ = "READ"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ToolRegistryError(Exception):
    """Base class for registry errors."""


class InvalidToolSpecError(
    ToolRegistryError,
    ValueError,
):
    """Raised when tool metadata is malformed or inconsistent."""


class DuplicateToolError(
    ToolRegistryError,
    ValueError,
):
    """Raised when a tool name is already registered."""


class UnknownToolError(
    ToolRegistryError,
    KeyError,
):
    """Raised when dispatch targets an unregistered tool."""


class InvalidToolArgumentsError(
    ToolRegistryError,
    TypeError,
):
    """Raised when dispatch arguments are not a string-keyed mapping."""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze_json(item)
            for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(
            _freeze_json(item)
            for item in value
        )
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _thaw_json(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [
            _thaw_json(item)
            for item in value
        ]
    return value


def _validate_json_value(
    node: Any,
    path: str,
) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if not isinstance(key, str):
                raise InvalidToolSpecError(
                    f"{path} contains a non-string JSON key."
                )
            _validate_json_value(
                value,
                f"{path}.{key}",
            )
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _validate_json_value(
                value,
                f"{path}[{index}]",
            )
    elif isinstance(node, float) and not math.isfinite(node):
        raise InvalidToolSpecError(
            f"{path} contains a non-finite number."
        )
    elif not isinstance(
        node,
        (str, int, float, bool, type(None)),
    ):
        raise InvalidToolSpecError(
            f"{path} contains a non-JSON value."
        )


def _validate_subschema(
    schema: Any,
    path: str,
) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise InvalidToolSpecError(
            f"{path} must be a schema object or boolean."
        )
    _validate_schema_node(schema, path)


def _validate_schema_node(
    node: Mapping[str, Any],
    path: str,
) -> None:
    declared_type = node.get("type")
    if declared_type is not None:
        if isinstance(declared_type, str):
            declared_types = [declared_type]
        elif isinstance(declared_type, (list, tuple)):
            declared_types = list(declared_type)
            if not declared_types:
                raise InvalidToolSpecError(
                    f"{path}.type cannot be empty."
                )
        else:
            raise InvalidToolSpecError(
                f"{path}.type must be a JSON type or list of types."
            )

        if (
            any(
                item not in _JSON_TYPES
                for item in declared_types
            )
            or len(set(declared_types)) != len(declared_types)
        ):
            raise InvalidToolSpecError(
                f"{path}.type contains an invalid or duplicate JSON type."
            )

    properties = node.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise InvalidToolSpecError(
                f"{path}.properties must be an object."
            )
        for property_name, property_schema in properties.items():
            if not property_name:
                raise InvalidToolSpecError(
                    f"{path}.properties contains an empty name."
                )
            _validate_subschema(
                property_schema,
                f"{path}.properties.{property_name}",
            )

    required = node.get("required")
    if required is not None:
        if not isinstance(required, (list, tuple)):
            raise InvalidToolSpecError(
                f"{path}.required must be an array."
            )
        if (
            any(
                not isinstance(item, str)
                or not item
                for item in required
            )
            or len(set(required)) != len(required)
        ):
            raise InvalidToolSpecError(
                f"{path}.required must contain unique non-empty strings."
            )
        if properties is not None:
            unknown = set(required) - set(properties)
            if unknown:
                raise InvalidToolSpecError(
                    f"{path}.required references unknown properties: "
                    + ", ".join(sorted(unknown))
                )

    for keyword in (
        "additionalProperties",
        "contains",
        "if",
        "then",
        "else",
        "items",
        "not",
        "propertyNames",
    ):
        if keyword in node:
            _validate_subschema(
                node[keyword],
                f"{path}.{keyword}",
            )

    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if keyword not in node:
            continue
        schemas = node[keyword]
        if (
            not isinstance(schemas, (list, tuple))
            or not schemas
        ):
            raise InvalidToolSpecError(
                f"{path}.{keyword} must be a non-empty schema array."
            )
        for index, schema in enumerate(schemas):
            _validate_subschema(
                schema,
                f"{path}.{keyword}[{index}]",
            )

    for keyword in (
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
    ):
        if keyword not in node:
            continue
        schemas = node[keyword]
        if not isinstance(schemas, Mapping):
            raise InvalidToolSpecError(
                f"{path}.{keyword} must be an object of schemas."
            )
        for schema_name, schema in schemas.items():
            _validate_subschema(
                schema,
                f"{path}.{keyword}.{schema_name}",
            )


def _normalize_schema(
    parameters: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(parameters, Mapping):
        raise InvalidToolSpecError(
            "Tool parameters must be a JSON Schema object."
        )

    schema = copy.deepcopy(dict(parameters))
    _validate_json_value(schema, "parameters")
    _validate_schema_node(schema, "parameters")

    if schema.get("type") != "object":
        raise InvalidToolSpecError(
            "Tool parameters must declare type 'object'."
        )
    if "properties" not in schema:
        raise InvalidToolSpecError(
            "Tool parameters must declare properties."
        )

    try:
        json.dumps(
            schema,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidToolSpecError(
            "Tool parameters must be valid JSON."
        ) from exc

    return _freeze_json(schema)


def _normalize_risk(
    risk: ToolRisk | str,
) -> ToolRisk:
    if isinstance(risk, ToolRisk):
        return risk
    if isinstance(risk, str):
        try:
            return ToolRisk(risk.strip().upper())
        except ValueError as exc:
            raise InvalidToolSpecError(
                "Tool risk must be READ, LOW, MEDIUM, or HIGH."
            ) from exc
    raise InvalidToolSpecError(
        "Tool risk must be READ, LOW, MEDIUM, or HIGH."
    )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A validated tool contract independent from any model provider."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[..., Any]
    category: str = "general"
    risk: ToolRisk | str = ToolRisk.READ
    freshness_seconds: float | None = None
    idempotent: bool = True
    side_effect: bool = False
    requires_approval: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _TOOL_NAME_RE.fullmatch(self.name)
        ):
            raise InvalidToolSpecError(
                "Tool name must be 1-64 characters and match "
                "[A-Za-z_][A-Za-z0-9_-]*."
            )
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
        ):
            raise InvalidToolSpecError(
                "Tool description must be non-empty."
            )
        if self.description != self.description.strip():
            raise InvalidToolSpecError(
                "Tool description cannot have surrounding whitespace."
            )
        if not callable(self.handler):
            raise InvalidToolSpecError(
                "Tool handler must be callable."
            )
        if (
            not isinstance(self.category, str)
            or not _CATEGORY_RE.fullmatch(self.category)
        ):
            raise InvalidToolSpecError(
                "Tool category must be 1-64 identifier characters."
            )

        normalized_risk = _normalize_risk(self.risk)
        object.__setattr__(
            self,
            "risk",
            normalized_risk,
        )

        freshness = self.freshness_seconds
        if freshness is not None:
            if (
                isinstance(freshness, bool)
                or not isinstance(freshness, (int, float))
                or not math.isfinite(float(freshness))
                or freshness < 0
            ):
                raise InvalidToolSpecError(
                    "Tool freshness_seconds must be a finite non-negative number."
                )
            object.__setattr__(
                self,
                "freshness_seconds",
                float(freshness),
            )

        for field_name in (
            "idempotent",
            "side_effect",
            "requires_approval",
        ):
            if not isinstance(
                getattr(self, field_name),
                bool,
            ):
                raise InvalidToolSpecError(
                    f"Tool {field_name} must be boolean."
                )

        if self.side_effect and normalized_risk is ToolRisk.READ:
            raise InvalidToolSpecError(
                "A side-effecting tool cannot have READ risk."
            )
        if self.requires_approval and not self.side_effect:
            raise InvalidToolSpecError(
                "Approval can only be required for a side-effecting tool."
            )

        object.__setattr__(
            self,
            "parameters",
            _normalize_schema(self.parameters),
        )

    @property
    def json_schema(self) -> dict[str, Any]:
        """Return a detached, mutable copy of the parameters schema."""

        return _thaw_json(self.parameters)

    @property
    def action(self) -> bool:
        """Whether the tool performs an externally observable action."""

        return self.side_effect

    def to_chat_completions_tool(self) -> dict[str, Any]:
        """Render this tool in the Chat Completions function format."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema,
            },
        }


class ToolRegistry:
    """Thread-safe registry of validated tool specifications."""

    def __init__(
        self,
        specs: Iterable[ToolSpec] | None = None,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._lock = RLock()
        if specs is not None:
            for spec in specs:
                self.register(spec)

    def __len__(self) -> int:
        with self._lock:
            return len(self._specs)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._specs

    @property
    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._specs)

    def register(self, spec: ToolSpec) -> ToolSpec:
        """Register a spec, rejecting invalid types and duplicate names."""

        if not isinstance(spec, ToolSpec):
            raise InvalidToolSpecError(
                "Registry entries must be ToolSpec instances."
            )
        with self._lock:
            if spec.name in self._specs:
                raise DuplicateToolError(
                    f"Tool '{spec.name}' is already registered."
                )
            self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        with self._lock:
            try:
                return self._specs[name]
            except (KeyError, TypeError) as exc:
                raise UnknownToolError(
                    f"Unknown tool: {name!r}."
                ) from exc

    def filter_tools(
        self,
        *,
        action: bool | None = None,
        risk: (
            ToolRisk
            | str
            | Iterable[ToolRisk | str]
            | None
        ) = None,
        requires_approval: bool | None = None,
        category: str | None = None,
    ) -> tuple[ToolSpec, ...]:
        """Select tools by action, risk, approval, and category capability."""

        if action is not None and not isinstance(action, bool):
            raise ValueError("action filter must be boolean or None.")
        if (
            requires_approval is not None
            and not isinstance(requires_approval, bool)
        ):
            raise ValueError(
                "requires_approval filter must be boolean or None."
            )
        if category is not None and not isinstance(category, str):
            raise ValueError("category filter must be a string or None.")

        risks: frozenset[ToolRisk] | None
        if risk is None:
            risks = None
        elif isinstance(risk, (ToolRisk, str)):
            risks = frozenset({_normalize_risk(risk)})
        else:
            try:
                risks = frozenset(
                    _normalize_risk(item)
                    for item in risk
                )
            except TypeError as exc:
                raise ValueError(
                    "risk filter must be a risk or iterable of risks."
                ) from exc
            if not risks:
                return ()

        with self._lock:
            snapshot = tuple(self._specs.values())

        return tuple(
            spec
            for spec in snapshot
            if (
                (action is None or spec.action is action)
                and (risks is None or spec.risk in risks)
                and (
                    requires_approval is None
                    or spec.requires_approval
                    is requires_approval
                )
                and (
                    category is None
                    or spec.category == category
                )
            )
        )

    def to_chat_completions_tools(
        self,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """Render selected registry entries for a Chat Completions request."""

        return [
            spec.to_chat_completions_tool()
            for spec in self.filter_tools(**filters)
        ]

    def dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> Any:
        """Invoke a named handler with string-keyed keyword arguments."""

        spec = self.get(name)
        if arguments is None:
            keyword_arguments: dict[str, Any] = {}
        elif not isinstance(arguments, Mapping):
            raise InvalidToolArgumentsError(
                "Tool arguments must be an object or None."
            )
        else:
            keyword_arguments = dict(arguments)

        if any(
            not isinstance(key, str)
            for key in keyword_arguments
        ):
            raise InvalidToolArgumentsError(
                "Tool argument names must be strings."
            )

        return spec.handler(**keyword_arguments)
