"""Diagnostic model and evidence-citation nodes; no persistence or DB connections."""

import json
from agent_graph import run_diagnostic_graph
from agent_policy import validate_answer_evidence
from agent_tool_execution import AgentToolExecutor
from langchain_bridge import langchain_tools
from model_metadata import (
    safe_usage_count as _safe_usage_count,
    provider_route_metadata as _provider_route_metadata,
)
from runtime_policy import RuntimePolicyError


class DiagnosticAgent:
    """LangGraph node callbacks composed with one AgentRunContext."""

    def __init__(self, context):
        self.context = context
        self.tool_executor = AgentToolExecutor(context)

    def run(self):
        try:
            self.context.dependencies.require_operation("AGENT_RUN")
        except RuntimePolicyError as exc:
            self.context.errors.append(
                {"type": type(exc).__name__, "message": str(exc)}
            )
            return self.context.finish(
                status="stopped", stop_reason="runtime_policy_blocked"
            )
        self.context.messages.append(
            {
                "role": "system",
                "content": "Trusted runtime execution policy (not a user preference): "
                + json.dumps(
                    self.context.dependencies.get_runtime_policy(), ensure_ascii=False
                )
                + ". Do not request blocked operations. In production use estimated plans and catalog/session observations; do not claim runtime evidence. Proposals never override execution policy or human approval.",
            }
        )
        if self.context.verify_environment:
            try:
                with self.context.telemetry_run.span("safedba.runtime_security.verify"):
                    self.context.runtime_security = (
                        self.context.dependencies.verify_security()
                    )
            except Exception as exc:
                self.context.errors.append(
                    {"type": type(exc).__name__, "message": str(exc)[:2000]}
                )
                return self.context.finish(
                    status="failed", stop_reason="runtime_security_check_failed"
                )
        self.context.message = None
        self.context.tool_calls = []
        self.context.finish_reason = None
        self.context.framework_tools = langchain_tools(
            self.context.dependencies.registry, self.context.dependencies.dispatch_tool
        )
        return run_diagnostic_graph(
            model_step=self.model_step,
            tools_step=self.tool_executor.run_turn,
            answer_step=self.answer_step,
            has_tool_calls=lambda: bool(self.context.tool_calls),
            snapshot=self.context.graph_snapshot,
            exhausted=lambda: self.context.finish(
                status="stopped", stop_reason="max_iterations_exceeded"
            ),
            max_iterations=self.context.max_iterations,
        )

    def model_step(self, iteration):
        try:
            self.context.dependencies.require_operation("AGENT_RUN")
        except RuntimePolicyError as exc:
            self.context.errors.append(
                {"type": type(exc).__name__, "message": str(exc)}
            )
            return self.context.finish(
                status="stopped", stop_reason="runtime_policy_blocked"
            )
        if (
            self.context.dependencies.clock.monotonic() - self.context.started
            >= self.context.deadline_seconds
        ):
            return self.context.finish(
                status="stopped", stop_reason="deadline_exceeded"
            )
        model_started = self.context.dependencies.clock.monotonic()
        provider_route = {}
        try:
            with self.context.telemetry_run.span(
                "safedba.llm.complete",
                {
                    "safedba.iteration": iteration + 1,
                    "gen_ai.request.model": getattr(
                        self.context.provider_instance, "model", None
                    ),
                },
            ) as model_span:
                response = self.context.provider_instance.complete(
                    messages=self.context.messages,
                    tools=self.context.dependencies.filter_tools(
                        self.context.available_tools,
                        self.context.dependencies.get_runtime_policy(),
                    ),
                    tool_choice="auto",
                )
                provider_route = _provider_route_metadata(
                    self.context.provider_instance
                )
                set_attribute = getattr(model_span, "set_attribute", None)
                if callable(set_attribute):
                    telemetry_attributes = {
                        "gen_ai.response.model": provider_route.get("selected_model"),
                        "safedba.llm.provider": provider_route.get("selected_provider"),
                        "safedba.llm.fallback_used": provider_route.get(
                            "fallback_used"
                        ),
                        "safedba.llm.failover_reason": provider_route.get(
                            "failover_reason"
                        ),
                        "safedba.llm.primary_circuit_state": provider_route.get(
                            "primary_circuit_state"
                        ),
                    }
                    try:
                        for key, value in telemetry_attributes.items():
                            if value is not None:
                                set_attribute(key, value)
                    except Exception:
                        pass
        except Exception as exc:
            provider_route = _provider_route_metadata(self.context.provider_instance)
            self.context.model_trace.append(
                {
                    "iteration": iteration + 1,
                    "model": provider_route.get("selected_model")
                    or getattr(self.context.provider_instance, "model", None),
                    "provider_route": provider_route,
                    "status": "error",
                    "duration_ms": round(
                        (self.context.dependencies.clock.monotonic() - model_started)
                        * 1000.0,
                        3,
                    ),
                    "error_type": type(exc).__name__,
                }
            )
            self.context.errors.append(
                {"type": type(exc).__name__, "message": "LLM provider request failed."}
            )
            return self.context.finish(status="failed", stop_reason="provider_error")
        self.context.llm_turns += 1
        response_usage = getattr(response, "usage", None)
        turn_prompt_tokens = _safe_usage_count(
            getattr(response_usage, "prompt_tokens", 0)
        )
        turn_completion_tokens = _safe_usage_count(
            getattr(response_usage, "completion_tokens", 0)
        )
        turn_total_tokens = _safe_usage_count(
            getattr(
                response_usage,
                "total_tokens",
                turn_prompt_tokens + turn_completion_tokens,
            )
        )
        self.context.prompt_tokens += turn_prompt_tokens
        self.context.completion_tokens += turn_completion_tokens
        self.context.total_tokens += turn_total_tokens
        self.context.model_trace.append(
            {
                "iteration": iteration + 1,
                "model": provider_route.get("selected_model")
                or getattr(self.context.provider_instance, "model", None),
                "provider_route": provider_route,
                "status": "success",
                "duration_ms": round(
                    (self.context.dependencies.clock.monotonic() - model_started)
                    * 1000.0,
                    3,
                ),
                "usage": {
                    "prompt_tokens": turn_prompt_tokens,
                    "completion_tokens": turn_completion_tokens,
                    "total_tokens": turn_total_tokens,
                },
            }
        )
        choices = getattr(response, "choices", None)
        if not choices:
            self.context.errors.append(
                {
                    "type": "MalformedProviderResponse",
                    "message": "Provider response contained no choices.",
                }
            )
            return self.context.finish(
                status="failed", stop_reason="malformed_provider_response"
            )
        choice = choices[0]
        self.context.message = getattr(choice, "message", None)
        if self.context.message is None:
            self.context.errors.append(
                {
                    "type": "MalformedProviderResponse",
                    "message": "Provider choice contained no message.",
                }
            )
            return self.context.finish(
                status="failed", stop_reason="malformed_provider_response"
            )
        self.context.messages.append(
            self.context.provider_instance.assistant_message_for_history(
                self.context.message
            )
        )
        self.context.tool_calls = (
            getattr(self.context.message, "tool_calls", None) or []
        )
        self.context.finish_reason = getattr(choice, "finish_reason", None)
        if self.context.tool_calls and self.context.finish_reason in {
            "length",
            "content_filter",
        }:
            self.context.errors.append(
                {
                    "type": "TruncatedToolCallResponse",
                    "message": "Provider returned tool calls from a truncated or filtered response; no tools were executed.",
                }
            )
            return self.context.finish(
                status="stopped", stop_reason="model_" + self.context.finish_reason
            )
        if self.context.tool_calls and self.context.finish_reason not in {
            "tool_calls",
            "function_call",
        }:
            self.context.errors.append(
                {
                    "type": "MalformedProviderResponse",
                    "message": "Provider returned tool calls with an inconsistent finish reason.",
                }
            )
            return self.context.finish(
                status="failed", stop_reason="malformed_provider_response"
            )

    def answer_step(self, iteration):
        content = getattr(self.context.message, "content", None) or ""
        if not content.strip():
            self.context.errors.append(
                {
                    "type": "EmptyAgentAnswer",
                    "message": "Model returned neither tools nor an answer.",
                }
            )
            return self.context.finish(
                status="failed", stop_reason="empty_model_response"
            )
        if self.context.finish_reason in {"length", "content_filter"}:
            return self.context.finish(
                status="stopped",
                stop_reason="model_" + self.context.finish_reason,
                answer=content,
            )
        required_refs = {
            ref
            for proposal in self.context.proposals
            for ref in proposal.get("evidence_refs", [])
            if isinstance(ref, str)
        }
        citation_errors = validate_answer_evidence(
            content, self.context.ledger.records, required_refs=required_refs
        )
        if citation_errors:
            if iteration + 1 >= self.context.max_iterations:
                self.context.errors.append(
                    {"type": "EvidenceCitationRequired", "messages": citation_errors}
                )
                return self.context.finish(
                    status="stopped",
                    stop_reason="evidence_citation_missing",
                    answer=content,
                )
            self.context.messages.append(
                {
                    "role": "user",
                    "content": "Deterministic evidence policy rejected the draft answer. Revise it without calling more tools and cite the required successful evidence references exactly. "
                    + " ".join(citation_errors),
                }
            )
            return None
        return self.context.finish(
            status="completed", stop_reason="final_answer", answer=content
        )
