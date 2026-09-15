"""Public Agent facade and dependency composition; policy lives in focused modules."""

# Historical imports remain available here for existing integrations and frozen
# compatibility tests. New diagnostic code consumes AgentDependencies instead
# of reaching back into this module or importing application configuration.

from agent_tool_catalog import TOOLS, _TOOL_CAPABILITIES
from agent_tools import build_tool_registry, dispatch_builtin_tool
from llm_provider import get_llm_provider
import json
import math
import time
import uuid
from agent_graph import run_diagnostic_graph
from langchain_bridge import LangChainProvider, langchain_tools
import config as runtime_config
from runtime_policy import (
    RuntimePolicyError,
    filter_tools,
    get_runtime_policy,
    require_operation,
    require_tool,
)
from agent_policy import (
    EvidenceLedger,
    PROPOSAL_TOOL_TO_ACTION,
    PROPOSAL_TOOLS,
    is_diagnosis_only_request,
    is_explicit_proposal_request,
    serialize_tool_output,
    summarize_arguments,
    summarize_result,
    validate_answer_evidence,
    validate_tool_arguments,
)
from config import (
    AGENT_DEADLINE_SECONDS,
    AGENT_MAX_TOOL_CALLS_PER_TURN,
    AGENT_MAX_TOOL_OUTPUT_CHARS,
    AGENT_MAX_TOTAL_TOOL_CALLS,
    AGENT_RUNTIME_EVIDENCE_TTL_SECONDS,
)
from db_tools import (
    get_active_sessions,
    get_column_info,
    get_column_stats,
    get_database_health,
    get_estimated_query_plan,
    get_indexes,
    get_lock_waits,
    get_operational_snapshot,
    get_query_plan,
    get_transaction_sessions,
    verify_runtime_security,
)
from diagnostics import (
    analyze_query_plan,
    detect_cardinality_anomalies,
    detect_non_sargable_predicates,
)
from actions import (
    build_analyze_table_proposal,
    build_create_index_proposal,
    build_query_rewrite_proposal,
    build_terminate_backend_proposal,
    validate_proposal_shape,
)
from tool_registry import ToolRegistry, ToolRisk, ToolSpec
from experience_store import SQLiteExperienceStore
from agent_memory import SQLiteAgentMemory
from telemetry import get_telemetry_manager
from agent_context import AgentRunContext
from agent_dependencies import AgentDependencies
from agent_knowledge import configure_knowledge, KNOWLEDGE_INSTRUCTIONS
from agent_runtime import DiagnosticAgent
from agent_prompts import AGENT_INSTRUCTIONS
from agent_review import review_execution_result as _review_execution_result
from model_metadata import (
    safe_usage_count as _safe_usage_count,
    provider_route_metadata as _provider_route_metadata,
)


def _dispatch_builtin_tool(name: str, arguments: dict):
    return dispatch_builtin_tool(
        name,
        arguments,
        implementations={
            "analyze_query_plan": analyze_query_plan,
            "build_analyze_table_proposal": build_analyze_table_proposal,
            "build_create_index_proposal": build_create_index_proposal,
            "build_query_rewrite_proposal": build_query_rewrite_proposal,
            "build_terminate_backend_proposal": build_terminate_backend_proposal,
            "detect_cardinality_anomalies": detect_cardinality_anomalies,
            "detect_non_sargable_predicates": detect_non_sargable_predicates,
            "get_active_sessions": get_active_sessions,
            "get_column_info": get_column_info,
            "get_column_stats": get_column_stats,
            "get_database_health": get_database_health,
            "get_estimated_query_plan": get_estimated_query_plan,
            "get_indexes": get_indexes,
            "get_lock_waits": get_lock_waits,
            "get_operational_snapshot": get_operational_snapshot,
            "get_query_plan": get_query_plan,
            "get_transaction_sessions": get_transaction_sessions,
        },
    )


def _build_tool_registry() -> ToolRegistry:
    return build_tool_registry(_dispatch_builtin_tool)


TOOL_REGISTRY = _build_tool_registry()


def call_tool(name: str, arguments: dict):
    """Dispatch through the typed capability registry."""
    require_tool(name)
    return TOOL_REGISTRY.dispatch(name, arguments)


def _dependencies():
    knowledge, registry, dispatch = configure_knowledge(
        runtime_config, TOOL_REGISTRY, call_tool, require_tool
    )
    return AgentDependencies(
        settings=runtime_config,
        registry=registry,
        dispatch_tool=dispatch,
        get_provider=get_llm_provider,
        memory_store_factory=SQLiteAgentMemory,
        experience_store_factory=SQLiteExperienceStore,
        telemetry_factory=get_telemetry_manager,
        verify_security=verify_runtime_security,
        validate_proposal=validate_proposal_shape,
        require_operation=require_operation,
        get_runtime_policy=get_runtime_policy,
        filter_tools=filter_tools,
        clock=time,
        evidence_ttl_seconds=AGENT_RUNTIME_EVIDENCE_TTL_SECONDS,
        instructions=(
            AGENT_INSTRUCTIONS + "\n\n" + KNOWLEDGE_INSTRUCTIONS
            if knowledge is not None
            else AGENT_INSTRUCTIONS
        ),
        knowledge=knowledge,
    )


def run_agent(
    user_message: str,
    max_iterations: int = 8,
    *,
    run_id: str | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    mode: str = "diagnose",
    allowed_actions: set[str] | None = None,
    provider=None,
    chat_model=None,
    memory_store=None,
    use_memory: bool | None = None,
    experience_store=None,
    capture_experience: bool | None = None,
    max_total_tool_calls: int = AGENT_MAX_TOTAL_TOOL_CALLS,
    max_tool_calls_per_turn: int = AGENT_MAX_TOOL_CALLS_PER_TURN,
    deadline_seconds: float = AGENT_DEADLINE_SECONDS,
    max_tool_output_chars: int = AGENT_MAX_TOOL_OUTPUT_CHARS,
    verify_environment: bool = True,
    telemetry_manager=None
) -> dict:
    context = AgentRunContext(
        user_message,
        max_iterations,
        dependencies=_dependencies(),
        run_id=run_id,
        thread_id=thread_id,
        session_id=session_id,
        mode=mode,
        allowed_actions=allowed_actions,
        provider=provider,
        chat_model=chat_model,
        memory_store=memory_store,
        use_memory=use_memory,
        experience_store=experience_store,
        capture_experience=capture_experience,
        max_total_tool_calls=max_total_tool_calls,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
        deadline_seconds=deadline_seconds,
        max_tool_output_chars=max_tool_output_chars,
        verify_environment=verify_environment,
        telemetry_manager=telemetry_manager,
    )
    return DiagnosticAgent(context).run()


def review_execution_result(
    proposal: dict, execution_result: dict, *, chat_model=None
) -> str:
    return _review_execution_result(
        proposal,
        execution_result,
        chat_model=chat_model,
        get_provider=get_llm_provider,
        require_operation=require_operation,
    )


if __name__ == "__main__":
    from agent_example import run_example

    run_example(run_agent)
