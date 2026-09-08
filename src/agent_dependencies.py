"""Explicit integration boundary between diagnostic logic and application wiring."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tool_registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentDependencies:
    settings: Any
    registry: ToolRegistry
    dispatch_tool: Callable
    get_provider: Callable
    memory_store_factory: Callable
    experience_store_factory: Callable
    telemetry_factory: Callable
    verify_security: Callable
    validate_proposal: Callable
    require_operation: Callable
    get_runtime_policy: Callable
    filter_tools: Callable
    clock: Any
    evidence_ttl_seconds: float
    instructions: str
