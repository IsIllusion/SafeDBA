from copy import deepcopy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from langchain_core.caches import InMemoryCache
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from langsmith.run_helpers import get_tracing_context
from pydantic import Field

from test_agent_loop import AGENT, CALLS, FakeProvider, response, tool_call
from langchain_bridge import LangChainProvider, ProviderChatModel, langchain_tools


class ScriptedChatModel(BaseChatModel):
    responses: list = Field(default_factory=list)
    requests: list = Field(default_factory=list)
    model_name: str = "native-langchain-script"

    @property
    def _llm_type(self):
        return "safedba-test-chat-model"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(
            {
                "messages": messages,
                "options": kwargs,
                "tracing": get_tracing_context().get("enabled"),
            }
        )
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return ChatResult(generations=[ChatGeneration(message=result)])


def call_message(*, finish_reason="tool_calls", call_id="health-1", args=None):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_database_health",
                "args": args or {},
                "id": call_id,
            }
        ],
        response_metadata={"finish_reason": finish_reason},
    )


class LangChainBridgeTests(unittest.TestCase):
    def setUp(self):
        CALLS.clear()

    def run_native(self, responses, **kwargs):
        model = ScriptedChatModel(responses=responses)
        result = AGENT.run_agent(
            "Investigate database health.",
            chat_model=model,
            use_memory=False,
            capture_experience=False,
            **kwargs,
        )
        return result, model

    def test_native_chat_model_tools_messages_and_usage(self):
        result, model = self.run_native(
            [
                call_message(),
                AIMessage(
                    content="Health evidence [ev-0001].",
                    usage_metadata={
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "total_tokens": 25,
                    },
                ),
            ]
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["total_tokens"], 25)
        self.assertEqual(result["model_trace"][0]["model"], model.model_name)
        self.assertEqual(CALLS, [("get_database_health", (), {})])
        tools = model.requests[0]["options"]["tools"]
        self.assertNotIn("propose_create_index", [t["function"]["name"] for t in tools])
        message = next(
            m for m in model.requests[1]["messages"] if isinstance(m, ToolMessage)
        )
        self.assertEqual(message.tool_call_id, "health-1")
        self.assertEqual(json.loads(message.content)["evidence_ref"], "ev-0001")

    def test_native_truncated_calls_never_execute(self):
        for reason in ("length", "content_filter", "max_tokens"):
            with self.subTest(reason=reason):
                result, _ = self.run_native([call_message(finish_reason=reason)])
                self.assertEqual(result["status"], "stopped")
                self.assertEqual(CALLS, [])

    def test_native_inconsistent_finish_reason_never_executes(self):
        result, _ = self.run_native([call_message(finish_reason="stop")])
        self.assertEqual(result["stop_reason"], "malformed_provider_response")
        self.assertEqual(CALLS, [])

    def test_native_raw_malformed_arguments_are_not_dropped(self):
        raw = {
            "id": "invalid-1",
            "type": "function",
            "function": {
                "name": "get_database_health",
                "arguments": "{broken",
            },
        }
        result, model = self.run_native(
            [
                AIMessage(
                    content="",
                    additional_kwargs={"tool_calls": [raw]},
                    response_metadata={"finish_reason": "tool_calls"},
                ),
                AIMessage(content="Invalid request rejected."),
            ]
        )
        self.assertEqual(result["tool_trace"][0]["status"], "invalid_arguments")
        self.assertEqual(CALLS, [])
        self.assertTrue(
            any(
                isinstance(m, ToolMessage) and m.tool_call_id == "invalid-1"
                for m in model.requests[1]["messages"]
            )
        )

    def test_native_invalid_only_calls_keep_their_error(self):
        result, _ = self.run_native(
            [
                AIMessage(
                    content="",
                    invalid_tool_calls=[
                        {
                            "name": "get_database_health",
                            "args": "{bad",
                            "id": "bad",
                            "error": "Invalid JSON",
                            "type": "invalid_tool_call",
                        }
                    ],
                ),
                AIMessage(content="Rejected."),
            ]
        )
        self.assertEqual(result["tool_trace"][0]["status"], "invalid_arguments")
        self.assertEqual(CALLS, [])

    def test_ambiguous_mixed_call_order_is_rejected_before_tools(self):
        result, _ = self.run_native(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_database_health",
                            "args": {},
                            "id": "valid",
                        }
                    ],
                    invalid_tool_calls=[
                        {
                            "name": "get_database_health",
                            "args": "{bad",
                            "id": "invalid",
                            "error": "bad",
                        }
                    ],
                )
            ]
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(CALLS, [])

    def test_native_unknown_blocks_fail_closed_without_dropping_content(self):
        result, _ = self.run_native(
            [AIMessage(content=[{"type": "image", "url": "https://image.invalid"}])]
        )
        self.assertEqual(result["stop_reason"], "provider_error")

    def test_native_content_blocks_and_signed_reasoning_round_trip(self):
        content = [
            {
                "type": "thinking",
                "thinking": "Inspect the database",
                "signature": "provider-signature",
            },
            {
                "type": "tool_use",
                "id": "health-1",
                "name": "get_database_health",
                "input": {},
            },
        ]
        first = AIMessage(
            content=content,
            tool_calls=[{"id": "health-1", "name": "get_database_health", "args": {}}],
            response_metadata={"stop_reason": "tool_use"},
        )
        result, model = self.run_native(
            [
                first,
                AIMessage(
                    content=[{"type": "text", "text": "Health evidence [ev-0001]."}],
                    response_metadata={"stop_reason": "end_turn"},
                ),
            ]
        )
        self.assertEqual(result["status"], "completed")
        original = next(
            m for m in model.requests[1]["messages"] if isinstance(m, AIMessage)
        )
        self.assertEqual(original.content, content)
        self.assertEqual(result["answer"], "Health evidence [ev-0001].")
        for request in model.requests:
            self.assertIsInstance(request["messages"][0], SystemMessage)
            self.assertEqual(
                sum(isinstance(m, SystemMessage) for m in request["messages"]), 1
            )
            self.assertIn(
                "Trusted runtime execution policy", request["messages"][0].content
            )

    def test_unmatched_native_tool_use_block_cannot_be_ignored(self):
        result, _ = self.run_native(
            [
                AIMessage(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "lost",
                            "name": "get_database_health",
                            "input": {},
                        }
                    ]
                )
            ]
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(CALLS, [])

    def test_both_model_interfaces_cannot_be_selected(self):
        with self.assertRaises(ValueError):
            AGENT.run_agent(
                "Inspect", provider=FakeProvider([]), chat_model=ScriptedChatModel()
            )
        with self.assertRaises(TypeError):
            AGENT.run_agent("Inspect", chat_model=object())

    def test_exact_registry_schemas_and_capabilities(self):
        tools = langchain_tools(AGENT.TOOL_REGISTRY, AGENT.call_tool)
        for expected in AGENT.TOOL_REGISTRY.to_chat_completions_tools():
            tool = tools[expected["function"]["name"]]
            self.assertEqual(convert_to_openai_tool(tool), expected)
            spec = AGENT.TOOL_REGISTRY.get(tool.name)
            self.assertEqual(tool.metadata["risk"], spec.risk.value)
            self.assertFalse(tool.metadata["side_effect"])
        tools["get_database_health"].args_schema["properties"]["injected"] = {
            "type": "string"
        }
        self.assertNotIn(
            "injected",
            AGENT.TOOL_REGISTRY.get("get_database_health").json_schema["properties"],
        )

    def test_tool_adapter_cannot_redirect_via_hidden_name_argument(self):
        dispatched = []
        tools = langchain_tools(
            AGENT.TOOL_REGISTRY, lambda name, args: dispatched.append((name, args))
        )
        tools["get_database_health"].invoke({"_name": "propose_terminate_backend"})
        self.assertEqual(dispatched[0][0], "get_database_health")

    def test_existing_provider_preserves_reasoning_and_null_content(self):
        class ReasoningProvider(FakeProvider):
            @staticmethod
            def assistant_message_to_dict(message):
                result = FakeProvider.assistant_message_to_dict(message)
                result["reasoning_content"] = "Vendor reasoning extension"
                return result

        provider = ReasoningProvider(
            [
                response(
                    calls=[tool_call("health", "get_database_health", {})],
                    finish_reason="tool_calls",
                ),
                response(content="Evidence [ev-0001]."),
            ]
        )
        result = AGENT.run_agent(
            "Inspect", provider=provider, use_memory=False, capture_experience=False
        )
        self.assertEqual(result["status"], "completed")
        assistant = next(
            m for m in provider.message_batches[1] if m["role"] == "assistant"
        )
        self.assertIsNone(assistant["content"])
        self.assertEqual(assistant["reasoning_content"], "Vendor reasoning extension")

    def test_provider_adapter_exposes_standard_finish_and_token_metadata(self):
        provider = FakeProvider(
            [
                response(
                    content="Partial",
                    finish_reason="length",
                    usage=SimpleNamespace(
                        prompt_tokens=3,
                        completion_tokens=2,
                        total_tokens=5,
                    ),
                )
            ]
        )
        model = ProviderChatModel(provider=provider)
        message = model.bind_tools([]).invoke("Inspect")
        self.assertEqual(message.response_metadata["finish_reason"], "length")
        self.assertEqual(message.usage_metadata["total_tokens"], 5)

    def test_no_framework_retry_after_provider_error(self):
        result, model = self.run_native(
            [TimeoutError("secret endpoint"), AIMessage(content="Must not run")]
        )
        self.assertEqual(result["stop_reason"], "provider_error")
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn("secret endpoint", json.dumps(result))

    def test_inherited_tracing_and_model_callbacks_do_not_export_payloads(self):
        events = []

        class Capture(BaseCallbackHandler):
            def on_chat_model_start(self, *args, **kwargs):
                events.append(args)

        model = ScriptedChatModel(
            responses=[AIMessage(content="Complete.")], callbacks=[Capture()]
        )
        with patch.dict(
            os.environ, {"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true"}
        ):
            result = AGENT.run_agent(
                "Inspect private SQL",
                chat_model=model,
                use_memory=False,
                capture_experience=False,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(events, [])
        self.assertFalse(model.requests[0]["tracing"])

    def test_ambient_global_model_cache_does_not_reuse_diagnoses(self):
        previous = get_llm_cache()
        try:
            set_llm_cache(InMemoryCache())
            model = ScriptedChatModel(
                responses=[AIMessage(content="First"), AIMessage(content="Second")]
            )
            provider = LangChainProvider(chat_model=model)
            request = {"messages": [{"role": "user", "content": "Same incident"}]}
            first = provider.complete(**request).choices[0].message.content
            second = provider.complete(**request).choices[0].message.content
            self.assertEqual((first, second), ("First", "Second"))
            self.assertEqual(len(model.requests), 2)
        finally:
            set_llm_cache(previous)

    def test_chatopenai_actual_binding_with_local_mock_transport(self):
        requests = []
        replies = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "health",
                        "type": "function",
                        "function": {"name": "get_database_health", "arguments": "{}"},
                    }
                ],
            },
            {"role": "assistant", "content": "Health evidence [ev-0001]."},
        ]

        def transport(request):
            requests.append(json.loads(request.content))
            message = replies.pop(0)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-alignment",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "mock-compatible",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": (
                                "tool_calls" if message.get("tool_calls") else "stop"
                            ),
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            )

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            model = ChatOpenAI(
                model="mock-compatible",
                api_key="offline-test-only",
                base_url="https://model.invalid/v1",
                http_client=client,
                max_retries=0,
                use_responses_api=False,
            )
            result = AGENT.run_agent(
                "Inspect", chat_model=model, use_memory=False, capture_experience=False
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["total_tokens"], 14)
        self.assertEqual(len(requests), 2)
        expected = AGENT.TOOL_REGISTRY.to_chat_completions_tools()
        self.assertEqual(
            requests[0]["tools"],
            [t for t in expected if not t["function"]["name"].startswith("propose_")],
        )
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "health")

    def test_execution_review_accepts_native_chat_model_without_tools(self):
        model = ScriptedChatModel(
            responses=[AIMessage(content="The executor rejected the change.")]
        )
        answer = AGENT.review_execution_result(
            {"type": "CREATE_INDEX"}, {"status": "REJECTED"}, chat_model=model
        )
        self.assertEqual(answer, "The executor rejected the change.")
        self.assertNotIn("tools", model.requests[0]["options"])


if __name__ == "__main__":
    unittest.main()
