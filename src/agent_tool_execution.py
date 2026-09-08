"""Serial tool execution, evidence authorization, and bounded tool replies."""

import json
from agent_policy import (
    PROPOSAL_TOOLS,
    serialize_tool_output,
    summarize_arguments,
    summarize_result,
    validate_tool_arguments,
)


class AgentToolExecutor:
    """Execute observations/proposals only; action authority stays in the executor."""

    def __init__(self, context):
        self.context = context

    def run_turn(self, iteration):
        if len(self.context.tool_calls) > self.context.max_tool_calls_per_turn:
            self.context.errors.append(
                {
                    "type": "ToolBudgetExceeded",
                    "message": "Model requested too many tool calls in one turn.",
                }
            )
            return self.context.finish(
                status="stopped", stop_reason="per_turn_tool_budget_exceeded"
            )
        if (
            self.context.attempted_tool_calls + len(self.context.tool_calls)
            > self.context.max_total_tool_calls
        ):
            self.context.errors.append(
                {
                    "type": "ToolBudgetExceeded",
                    "message": "Model requested more tool calls than the run budget allows.",
                }
            )
            return self.context.finish(
                status="stopped", stop_reason="total_tool_budget_exceeded"
            )
        self.context.attempted_tool_calls += len(self.context.tool_calls)
        turn_evidence_cutoff = len(self.context.ledger.records)
        for tool_call in self.context.tool_calls:
            terminal = self.invoke_one(tool_call, turn_evidence_cutoff)
            if terminal is not None:
                return terminal
        self.context.checkpoint_memory_run(f"ITERATION_{iteration + 1}_TOOLS_COMPLETED")

    def invoke_one(self, tool_call, turn_evidence_cutoff):
        if (
            self.context.dependencies.clock.monotonic() - self.context.started
            >= self.context.deadline_seconds
        ):
            return self.context.finish(
                status="stopped", stop_reason="deadline_exceeded"
            )
        call_id = getattr(tool_call, "id", None)
        function = getattr(tool_call, "function", None)
        tool_name = getattr(function, "name", None)
        raw_arguments = getattr(function, "arguments", None)
        if not call_id or not tool_name:
            self.context.errors.append(
                {
                    "type": "MalformedToolCall",
                    "message": "Tool call lacked an ID or function name.",
                }
            )
            return self.context.finish(
                status="failed", stop_reason="malformed_tool_call"
            )
        try:
            arguments = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError) as exc:
            self._invalid_arguments(
                call_id, tool_name, None, {"message": str(exc)[:500]}
            )
            return
        schema = self.context.tool_parameters.get(tool_name)
        validation_errors = (
            [f"Unknown tool: {tool_name}"]
            if schema is None
            else validate_tool_arguments(arguments, schema)
        )
        if validation_errors:
            self._invalid_arguments(
                call_id, tool_name, arguments, {"messages": validation_errors}
            )
            return
        if self.context.ledger.is_duplicate(tool_name, arguments):
            result = {
                "error": {
                    "type": "DuplicateToolCall",
                    "message": "An identical tool call already ran in this diagnostic turn.",
                }
            }
            self._record_rejection(
                call_id, tool_name, arguments, result, "blocked_duplicate"
            )
            return
        evidence_refs: list[str] = []
        if tool_name in PROPOSAL_TOOLS:
            policy_errors, evidence_refs = self.context.ledger.proposal_authorization(
                tool_name,
                arguments,
                proposals_allowed=self.context.proposals_allowed,
                allowed_action_types=self.context.allowed_action_types,
                evidence_cutoff=turn_evidence_cutoff,
                runtime_evidence_ttl_seconds=self.context.dependencies.evidence_ttl_seconds,
            )
            if policy_errors:
                result = {
                    "error": {
                        "type": "ProposalPolicyRejected",
                        "messages": policy_errors,
                    }
                }
                self._record_rejection(
                    call_id, tool_name, arguments, result, "policy_rejected"
                )
                return
        self.context.ledger.mark_attempted(tool_name, arguments)
        tool_started = self.context.dependencies.clock.monotonic()
        try:
            tool_spec = self.context.dependencies.registry.get(tool_name)
            with self.context.telemetry_run.span(
                "safedba.tool.call",
                {
                    "safedba.tool.name": tool_name,
                    "safedba.tool.category": tool_spec.category,
                    "safedba.tool.risk": tool_spec.risk.value,
                },
            ):
                result = self.context.framework_tools[tool_name].invoke(
                    arguments, config={"callbacks": []}
                )
                duration_ms = (
                    self.context.dependencies.clock.monotonic() - tool_started
                ) * 1000.0
                if tool_name in PROPOSAL_TOOLS:
                    if not isinstance(result, dict):
                        raise TypeError("Proposal tool returned a non-object.")
                    result = dict(result)
                    result["evidence_refs"] = evidence_refs
                    shape_check = self.context.dependencies.validate_proposal(result)
                    if not shape_check.get("valid"):
                        raise ValueError(
                            "Built proposal failed deterministic shape validation: "
                            + "; ".join(shape_check.get("errors", []))
                        )
            record = self.context.ledger.add(
                tool=tool_name,
                arguments=arguments,
                result=result,
                status="success",
                duration_ms=duration_ms,
            )
            tool_output = serialize_tool_output(
                {"evidence_ref": record.ref, "data": result},
                max_chars=self.context.max_tool_output_chars,
            )
            if tool_name in PROPOSAL_TOOLS:
                self.context.proposals.append(result)
            self.context.tool_trace.append(
                {
                    "evidence_ref": record.ref,
                    "tool_call_id": call_id,
                    "tool": tool_name,
                    "arguments": summarize_arguments(arguments),
                    "status": "success",
                    "duration_ms": round(duration_ms, 3),
                    "result": summarize_result(result),
                }
            )
        except Exception as exc:
            duration_ms = (
                self.context.dependencies.clock.monotonic() - tool_started
            ) * 1000.0
            safe_message = (
                str(exc)[:1000]
                if isinstance(exc, (ValueError, KeyError, TypeError))
                else f"Tool execution failed with {type(exc).__name__}."
            )
            result = {"error": {"type": type(exc).__name__, "message": safe_message}}
            record = self.context.ledger.add(
                tool=tool_name,
                arguments=arguments,
                result=result,
                status="error",
                duration_ms=duration_ms,
            )
            self.context.errors.append(
                {
                    "evidence_ref": record.ref,
                    "tool": tool_name,
                    "type": type(exc).__name__,
                    "message": safe_message,
                }
            )
            self.context.tool_trace.append(
                {
                    "evidence_ref": record.ref,
                    "tool_call_id": call_id,
                    "tool": tool_name,
                    "arguments": summarize_arguments(arguments),
                    "status": "error",
                    "duration_ms": round(duration_ms, 3),
                    "result": summarize_result(result),
                }
            )
            tool_output = serialize_tool_output(
                result, max_chars=self.context.max_tool_output_chars
            )
        self.context.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": tool_output}
        )

    def _record_rejection(self, call_id, tool_name, arguments, result, status):
        record = self.context.ledger.add(
            tool=tool_name,
            arguments=arguments,
            result=result,
            status=status,
            duration_ms=0.0,
        )
        self.context.tool_trace.append(
            {
                "evidence_ref": record.ref,
                "tool_call_id": call_id,
                "tool": tool_name,
                "arguments": summarize_arguments(arguments),
                "status": status,
                "duration_ms": 0.0,
                "result": summarize_result(result),
            }
        )
        self.context.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": serialize_tool_output(
                    result, max_chars=self.context.max_tool_output_chars
                ),
            }
        )
        return

    def _invalid_arguments(self, call_id, tool_name, arguments, detail):
        tool_output = serialize_tool_output(
            {"error": {"type": "InvalidToolArguments", **detail}},
            max_chars=self.context.max_tool_output_chars,
        )
        self.context.tool_trace.append(
            {
                "tool_call_id": call_id,
                "tool": tool_name,
                "arguments": (
                    summarize_arguments(arguments)
                    if isinstance(arguments, dict)
                    else None
                ),
                "status": "invalid_arguments",
                "duration_ms": 0.0,
            }
        )
        self.context.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": tool_output}
        )
