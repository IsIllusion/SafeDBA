"""Fail-closed boundary between SafeDBA and an MCP transport.

The adapter deliberately exposes a smaller capability set than the internal
Agent registry.  MCP callers may collect database evidence, request a
diagnosis-only Agent run, and inspect public incident state.  Proposal,
approval, resume, and execution capabilities stay behind the native SafeDBA
control plane.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from agent_policy import validate_tool_arguments
from tool_registry import ToolRisk


READ_ONLY_TOOL_ALLOWLIST = frozenset({
    "analyze_query",
    "get_estimated_query_plan",
    "get_query_plan",
    "get_indexes",
    "get_column_info",
    "get_column_stats",
    "get_lock_waits",
    "get_database_health",
    "get_active_sessions",
    "get_transaction_sessions",
})

READ_ONLY_CATEGORIES = frozenset({
    "query",
    "catalog",
    "runtime",
})

MAX_DIAGNOSIS_REQUEST_CHARS = 50_000
MAX_SESSION_ID_CHARS = 200
MAX_INCIDENT_ID_CHARS = 200

_SECRET_KEY_PARTS = frozenset({
    "api_key",
    "authorization",
    "password",
    "secret",
})


class MCPPolicyError(ValueError):
    """Raised when an MCP request crosses the public capability boundary."""


def _json_safe(value: Any, *, key: str | None = None) -> Any:
    """Return detached JSON data and redact common secret-bearing keys."""

    normalized_key = key.lower() if isinstance(key, str) else ""
    if normalized_key and (
        any(
            part in normalized_key
            for part in _SECRET_KEY_PARTS
        )
        or normalized_key == "token"
        or normalized_key.endswith("_token")
    ):
        return "[REDACTED]"

    if isinstance(value, Mapping):
        normalized = {
            str(child_key): _json_safe(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    elif isinstance(value, (list, tuple)):
        normalized = [
            _json_safe(item)
            for item in value
        ]
    elif isinstance(value, float) and not math.isfinite(value):
        normalized = str(value)
    else:
        normalized = value

    return json.loads(
        json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
    )


def _non_empty_text(
    value: object,
    *,
    field: str,
    max_chars: int,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPPolicyError(
            f"{field} must be a non-empty string."
        )
    normalized = value.strip()
    if len(normalized) > max_chars:
        raise MCPPolicyError(
            f"{field} exceeds the {max_chars}-character limit."
        )
    return normalized


class SafeDBAMCPFacade:
    """Narrow, dependency-injectable public API used by the MCP server."""

    def __init__(
        self,
        *,
        registry=None,
        security_verifier: Callable[[], object] | None = None,
        agent_runner: Callable[..., dict] | None = None,
        incident_store_factory: Callable[[], object] | None = None,
        incident_view: Callable[[dict], dict] | None = None,
    ) -> None:
        if registry is None or agent_runner is None:
            from agent import TOOL_REGISTRY, run_agent

            registry = registry or TOOL_REGISTRY
            agent_runner = agent_runner or run_agent
        if security_verifier is None:
            from db_tools import verify_runtime_security

            security_verifier = verify_runtime_security
        if incident_store_factory is None:
            from workflow_store import SQLiteIncidentStore

            incident_store_factory = SQLiteIncidentStore
        if incident_view is None:
            from incident_workflow import incident_public_view

            incident_view = incident_public_view

        self._registry = registry
        self._security_verifier = security_verifier
        self._agent_runner = agent_runner
        self._incident_store_factory = incident_store_factory
        self._incident_view = incident_view

    @property
    def database_tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(READ_ONLY_TOOL_ALLOWLIST))

    def capabilities(self) -> dict[str, Any]:
        """Describe the stable public boundary without importing MCP types."""

        return {
            "server": "SafeDBA",
            "transport": "stdio",
            "database_access": "read_only",
            "database_tools": list(self.database_tool_names),
            "agent_modes": ["diagnose"],
            "incident_access": ["list", "get_public_view"],
            "memory": {
                "available": True,
                "opt_in_via_session_id": True,
            },
            "not_exposed": [
                "action proposal tools",
                "action executors",
                "incident creation or resume",
                "approval creation or bypass",
                "backend termination",
                "DDL or table maintenance",
            ],
        }

    def call_database_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch one explicitly allowed evidence tool after attestation."""

        if name not in READ_ONLY_TOOL_ALLOWLIST:
            raise MCPPolicyError(
                f"Tool {name!r} is not exposed through MCP."
            )

        spec = self._registry.get(name)
        if (
            spec.risk is not ToolRisk.READ
            or spec.category not in READ_ONLY_CATEGORIES
            or spec.side_effect
            or spec.requires_approval
        ):
            raise MCPPolicyError(
                f"Tool {name!r} no longer satisfies the MCP read-only policy."
            )

        normalized_arguments = dict(arguments or {})
        validation_errors = validate_tool_arguments(
            normalized_arguments,
            spec.json_schema,
        )
        if validation_errors:
            raise MCPPolicyError(
                "Invalid tool arguments: "
                + "; ".join(validation_errors)
            )

        # Re-attest immediately before every database observation.  This
        # catches role/configuration drift even for long-lived MCP processes.
        self._security_verifier()
        result = self._registry.dispatch(
            name,
            normalized_arguments,
        )
        return {
            "status": "success",
            "tool": name,
            "runtime_security": "verified",
            "data": _json_safe(result),
        }

    def diagnose_database(
        self,
        request: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run SafeDBA with action proposals deterministically disabled."""

        normalized_request = _non_empty_text(
            request,
            field="request",
            max_chars=MAX_DIAGNOSIS_REQUEST_CHARS,
        )
        normalized_session_id = None
        if session_id is not None:
            normalized_session_id = _non_empty_text(
                session_id,
                field="session_id",
                max_chars=MAX_SESSION_ID_CHARS,
            )

        kwargs: dict[str, Any] = {
            "mode": "diagnose",
            "thread_id": "mcp",
        }
        if normalized_session_id is not None:
            kwargs["session_id"] = normalized_session_id

        result = self._agent_runner(
            normalized_request,
            **kwargs,
        )
        if not isinstance(result, dict):
            raise MCPPolicyError(
                "SafeDBA Agent returned an invalid result."
            )
        if result.get("mode") not in (None, "diagnose"):
            raise MCPPolicyError(
                "SafeDBA Agent escaped diagnosis-only mode."
            )
        if result.get("proposals"):
            raise MCPPolicyError(
                "Diagnosis-only MCP runs cannot return action proposals."
            )

        public_result = {
            "run_id": result.get("run_id"),
            "session_id": result.get("session_id"),
            "status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "mode": "diagnose",
            "answer": result.get("answer", ""),
            "tool_trace": result.get("tool_trace", []),
            "errors": result.get("errors", []),
            "usage": result.get("usage", {}),
            "memory": result.get("memory", {}),
        }
        return _json_safe(public_result)

    def list_incidents(
        self,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List durable incident summaries without exposing mutation APIs."""

        store = self._incident_store_factory()
        incidents = store.list_incidents(limit=limit)
        return {
            "count": len(incidents),
            "incidents": _json_safe(incidents),
        }

    def get_incident(
        self,
        incident_id: str,
    ) -> dict[str, Any]:
        """Return the workflow's deliberately reduced public incident view."""

        normalized_id = _non_empty_text(
            incident_id,
            field="incident_id",
            max_chars=MAX_INCIDENT_ID_CHARS,
        )
        store = self._incident_store_factory()
        incident = store.load_incident(normalized_id)
        return _json_safe(
            self._incident_view(incident)
        )
