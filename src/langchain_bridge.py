"""LangChain model/tool contracts with lossless legacy-provider compatibility.

The provider adapter retains the original response as a local generation
artifact: invalid JSON, absent choices, truncation, and vendor reasoning fields
must reach SafeDBA's deterministic checks without silent normalization.
"""

from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langsmith import tracing_context
from pydantic import Field


def to_langchain_messages(messages: list[dict | BaseMessage]) -> list[BaseMessage]:
    """Preserve ordering and provider extensions, including reasoning_content."""
    converted = []
    for message in messages:
        if isinstance(message, BaseMessage):
            converted.append(message.model_copy(deep=True))
            continue
        role = message["role"]
        content = message.get("content") or ""
        if role == "assistant":
            extras = {
                k: deepcopy(v)
                for k, v in message.items()
                if k not in {"role", "content"}
            }
            converted.append(AIMessage(content=content, additional_kwargs=extras))
        elif role == "tool":
            converted.append(
                ToolMessage(content=content, tool_call_id=message["tool_call_id"])
            )
        elif role == "system":
            converted.append(SystemMessage(content=content))
        elif role == "user":
            converted.append(HumanMessage(content=content))
        else:
            raise ValueError(f"Unsupported conversation role: {role}")
    return converted


def _answer_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    parts = []
    for block in message.content:
        if isinstance(block, str):
            parts.append(block)
        elif (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
        elif isinstance(block, dict) and block.get("type") in {
            "reasoning",
            "thinking",
            "redacted_thinking",
        }:
            # Retained in the original AIMessage history, not shown as evidence.
            continue
        elif isinstance(block, dict) and block.get("type") in {"tool_call", "tool_use"}:
            arguments = (
                block.get("args")
                if block["type"] == "tool_call"
                else block.get("input")
            )
            if not any(
                call.get("id") == block.get("id")
                and call.get("name") == block.get("name")
                and call.get("args") == arguments
                for call in message.tool_calls
            ):
                raise ValueError(
                    "Content-block tool call does not match the model tool_calls contract."
                )
        else:
            raise TypeError(
                "Unsupported non-text response block in database diagnosis."
            )
    return "\n".join(parts)


def _native_chat_messages(messages: list[dict | BaseMessage]) -> list[BaseMessage]:
    """Use one leading system message without promoting user/tool content.

    The compatible-provider path keeps its original wire ordering. Native
    integrations get a portable system-prefix layout, retaining all trusted
    policy text and the explicit untrusted-memory delimiters in their order.
    """
    converted = to_langchain_messages(messages)
    system = [message for message in converted if isinstance(message, SystemMessage)]
    if not system:
        return converted
    if any(not isinstance(message.content, str) for message in system):
        raise TypeError("SafeDBA system instructions must be text.")
    return [
        SystemMessage(content="\n\n".join(message.content for message in system))
    ] + [message for message in converted if not isinstance(message, SystemMessage)]


def assistant_to_wire(message: AIMessage) -> dict:
    if not isinstance(message, AIMessage):
        raise TypeError("A LangChain chat model must return AIMessage.")
    wire = {"role": "assistant", "content": _answer_text(message)}
    if "reasoning_content" in message.additional_kwargs:
        wire["reasoning_content"] = message.additional_kwargs["reasoning_content"]
    raw_calls = message.additional_kwargs.get("tool_calls")
    if raw_calls is not None:
        # Raw calls retain order and malformed arguments for fail-closed checks.
        wire["tool_calls"] = deepcopy(raw_calls)
    else:
        calls = [
            {
                "id": call.get("id"),
                "type": "function",
                "function": {
                    "name": call.get("name"),
                    "arguments": json.dumps(call["args"], ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
        if message.invalid_tool_calls and calls:
            # Without raw calls their original interleaving cannot be recovered.
            raise ValueError(
                "Mixed valid/invalid tool calls require raw ordered tool_calls."
            )
        calls.extend(
            {
                "id": call.get("id"),
                "type": "function",
                "function": {
                    "name": call.get("name"),
                    "arguments": call.get("args"),
                },
            }
            for call in message.invalid_tool_calls
        )
        if calls:
            wire["tool_calls"] = calls
    return wire


def _wire_response(message: AIMessage):
    wire = assistant_to_wire(message)
    calls = wire.get("tool_calls") or []
    if not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls):
        raise ValueError("Tool calls must be a list of objects.")
    normalized = []
    for call in calls:
        function = call.get("function") or {}
        if not isinstance(function, dict):
            raise ValueError("Tool call function must be an object.")
        normalized.append(
            SimpleNamespace(
                id=call.get("id"),
                function=SimpleNamespace(
                    name=function.get("name"),
                    arguments=function.get("arguments"),
                ),
            )
        )
    metadata = message.response_metadata
    finish_reason = metadata.get("finish_reason")
    if finish_reason is None:
        finish_reason = metadata.get("stop_reason")
    # Providers without finish metadata express completion through AIMessage.
    # Explicit unknown/truncated reasons are never replaced by inferred success.
    if finish_reason is None:
        finish_reason = "tool_calls" if calls else "stop"
    # Anthropic's standard completion reasons have equivalent semantics.
    finish_reason = {
        "tool_use": "tool_calls",
        "end_turn": "stop",
        "max_tokens": "length",
    }.get(
        finish_reason,
        finish_reason,
    )
    usage = message.usage_metadata or {}
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=wire["content"],
                    tool_calls=normalized,
                    safedba_wire=wire,
                    safedba_native_message=message,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        ),
    )
    return response


class ProviderChatModel(BaseChatModel):
    """Expose existing timeout/retry/circuit-breaker transports as chat models."""

    provider: Any = Field(exclude=True, repr=False)
    cache: Any = False

    @property
    def _llm_type(self) -> str:
        return "safedba-compatible-provider"

    @property
    def _identifying_params(self) -> dict:
        # Never serialize provider instances, endpoints, keys, or SQL here.
        return {"model": getattr(self.provider, "model", None)}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        from langchain_core.utils.function_calling import convert_to_openai_tool

        return self.bind(
            tools=[convert_to_openai_tool(t) for t in tools],
            tool_choice=tool_choice,
            **kwargs,
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if stop is not None:
            raise ValueError(
                "The compatible provider does not support custom stop sequences."
            )
        wire_messages = kwargs.pop("wire_messages", None)
        if wire_messages is None:
            wire_messages = []
            for message in messages:
                if isinstance(message, AIMessage):
                    wire_messages.append(assistant_to_wire(message))
                elif isinstance(message, ToolMessage):
                    wire_messages.append(
                        {
                            "role": "tool",
                            "content": message.content,
                            "tool_call_id": message.tool_call_id,
                        }
                    )
                elif isinstance(message, (SystemMessage, HumanMessage)):
                    wire_messages.append(
                        {
                            "role": (
                                "system"
                                if isinstance(message, SystemMessage)
                                else "user"
                            ),
                            "content": message.content,
                        }
                    )
                else:
                    raise ValueError("Unsupported LangChain message type.")
        request = {"messages": wire_messages}
        for key in ("tools", "tool_choice"):
            if key in kwargs:
                request[key] = kwargs.pop(key)
        if kwargs:
            raise ValueError("Unsupported provider generation options.")
        response = self.provider.complete(**request)
        choices = getattr(response, "choices", None)
        raw_message = getattr(choices[0], "message", None) if choices else None
        if raw_message is None:
            message = AIMessage(content="")
        else:
            wire = self.provider.assistant_message_to_dict(raw_message)
            message = to_langchain_messages([wire])[0]
            message.response_metadata["finish_reason"] = getattr(
                choices[0], "finish_reason", None
            )
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:

                def count(name):
                    try:
                        return max(0, int(getattr(raw_usage, name, 0) or 0))
                    except (TypeError, ValueError, OverflowError):
                        return 0

                message.usage_metadata = {
                    "input_tokens": count("prompt_tokens"),
                    "output_tokens": count("completion_tokens"),
                    "total_tokens": count("total_tokens"),
                }
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=message,
                    generation_info={"safedba_raw_response": response},
                )
            ]
        )


class LangChainProvider:
    """Compatibility facade for Agent, review, and bounded evaluation callers."""

    def __init__(self, *, provider=None, chat_model: BaseChatModel | None = None):
        if provider is not None and chat_model is not None:
            raise ValueError("Pass provider or chat_model, not both.")
        if chat_model is not None and not isinstance(chat_model, BaseChatModel):
            raise TypeError("chat_model must implement LangChain BaseChatModel.")
        self.provider = provider
        # Observations must not reuse an ambient global model cache, and SQL
        # payloads must not reach callbacks attached to an injected model.
        self.chat_model = (
            chat_model.model_copy(
                update={
                    "cache": False,
                    "callbacks": None,
                    "verbose": False,
                }
            )
            if chat_model is not None
            else ProviderChatModel(provider=provider)
        )
        self.model = (
            getattr(provider, "model", None)
            or getattr(self.chat_model, "model_name", None)
            or getattr(self.chat_model, "model", None)
        )

    @property
    def last_call_metadata(self):
        return getattr(self.provider, "last_call_metadata", {})

    def complete(self, *, messages, tools=None, tool_choice=None):
        with tracing_context(enabled=False):
            # The raw-provider path must not normalize null content, malformed
            # JSON or vendor extensions before legacy validation sees them.
            if self.provider is not None:
                kwargs = {"wire_messages": deepcopy(messages)}
                if tools is not None:
                    kwargs.update(tools=tools, tool_choice=tool_choice)
                result = self.chat_model.generate(
                    [to_langchain_messages(messages)], callbacks=[], **kwargs
                )
                return result.generations[0][0].generation_info["safedba_raw_response"]
            bound = (
                self.chat_model.bind_tools(tools, tool_choice=tool_choice)
                if tools
                else self.chat_model
            )
            return _wire_response(
                bound.invoke(_native_chat_messages(messages), config={"callbacks": []})
            )

    def assistant_message_to_dict(self, message):
        if self.provider is not None:
            return self.provider.assistant_message_to_dict(message)
        return deepcopy(message.safedba_wire)

    def assistant_message_for_history(self, message):
        if self.provider is not None:
            return self.assistant_message_to_dict(message)
        # Do not round-trip native messages through an OpenAI-shaped dict:
        # signed reasoning blocks and vendor extensions must remain intact.
        return message.safedba_native_message.model_copy(deep=True)


def langchain_tools(registry, dispatch) -> dict[str, StructuredTool]:
    """Build exact-schema tools; policy checks remain in the supplied dispatch."""
    result = {}

    def handler(name):
        def invoke(**arguments):
            return dispatch(name, arguments)

        return invoke

    for schema in registry.to_chat_completions_tools():
        name = schema["function"]["name"]
        spec = registry.get(name)

        result[name] = StructuredTool(
            name=name,
            description=spec.description,
            args_schema=deepcopy(schema["function"]["parameters"]),
            func=handler(name),
            metadata={
                "category": spec.category,
                "risk": spec.risk.value,
                "side_effect": spec.side_effect,
                "requires_approval": spec.requires_approval,
            },
        )
    return result
