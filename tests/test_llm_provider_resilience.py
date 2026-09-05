from collections import deque
import httpx
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from openai import (
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import llm_provider
from llm_provider import (
    ProviderCircuitOpenError,
    ProviderFallbackError,
    ResilientProvider,
    is_transient_provider_error,
)


class FakeBackend:
    def __init__(self, name, model, outcomes):
        self.provider_name = name
        self.model = model
        self.outcomes = deque(outcomes)
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def complete(provider):
    return provider.complete(
        messages=[{"role": "user", "content": "test"}],
        tools=[],
        tool_choice="auto",
    )


class ResilientProviderTests(unittest.TestCase):
    def test_factory_builds_separate_primary_and_fallback_routes(self):
        constructed = []

        def provider_factory(**kwargs):
            constructed.append(kwargs)
            return FakeBackend(
                kwargs["provider_name"],
                kwargs["model"],
                [object()],
            )

        old_provider = llm_provider._provider
        try:
            llm_provider._provider = None
            with patch.multiple(
                llm_provider,
                LLM_PROVIDER="deepseek",
                LLM_API_KEY="primary-key",
                LLM_MODEL="primary-model",
                LLM_BASE_URL=None,
                LLM_FALLBACK_PROVIDER="openai_compatible",
                LLM_FALLBACK_API_KEY="fallback-key",
                LLM_FALLBACK_MODEL="fallback-model",
                LLM_FALLBACK_BASE_URL="https://fallback.test/v1",
                LLM_FALLBACK_REASONING_ENABLED=False,
                LLM_CIRCUIT_FAILURE_THRESHOLD=4,
                LLM_CIRCUIT_COOLDOWN_SECONDS=45.0,
            ), patch.object(
                llm_provider,
                "OpenAICompatibleProvider",
                side_effect=provider_factory,
            ):
                provider = llm_provider.get_llm_provider()
        finally:
            llm_provider._provider = old_provider

        self.assertIsInstance(provider, ResilientProvider)
        self.assertEqual(len(constructed), 2)
        self.assertEqual(constructed[0]["api_key"], "primary-key")
        self.assertEqual(constructed[1]["api_key"], "fallback-key")
        self.assertNotEqual(
            constructed[0]["api_key"],
            constructed[1]["api_key"],
        )

    def test_only_transient_errors_are_routeable(self):
        self.assertTrue(is_transient_provider_error(TimeoutError()))
        self.assertTrue(is_transient_provider_error(ConnectionError()))
        self.assertFalse(is_transient_provider_error(ValueError("bad input")))
        self.assertFalse(is_transient_provider_error(RuntimeError("bad key")))
        request = httpx.Request("POST", "https://provider.test/chat")

        def status_error(error_type, status_code):
            return error_type(
                "provider error",
                response=httpx.Response(
                    status_code,
                    request=request,
                ),
                body=None,
            )

        self.assertTrue(is_transient_provider_error(
            status_error(RateLimitError, 429)
        ))
        self.assertTrue(is_transient_provider_error(
            status_error(InternalServerError, 503)
        ))
        self.assertFalse(is_transient_provider_error(
            status_error(BadRequestError, 400)
        ))
        self.assertFalse(is_transient_provider_error(
            status_error(AuthenticationError, 401)
        ))

    def test_transient_primary_failure_uses_explicit_fallback(self):
        primary = FakeBackend(
            "primary",
            "model-a",
            [TimeoutError("network unavailable")],
        )
        fallback_response = object()
        fallback = FakeBackend(
            "fallback",
            "model-b",
            [fallback_response],
        )
        provider = ResilientProvider(primary, fallback)

        result = complete(provider)

        self.assertIs(result, fallback_response)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(
            provider.last_call_metadata["selected_model"],
            "model-b",
        )
        self.assertTrue(
            provider.last_call_metadata["fallback_used"]
        )
        self.assertEqual(
            provider.last_call_metadata["failover_reason"],
            "primary_transient_error",
        )

    def test_non_transient_primary_error_never_uses_fallback(self):
        primary_error = ValueError("invalid model request")
        primary = FakeBackend(
            "primary",
            "model-a",
            [primary_error],
        )
        fallback = FakeBackend(
            "fallback",
            "model-b",
            [object()],
        )
        provider = ResilientProvider(primary, fallback)

        with self.assertRaises(ValueError) as raised:
            complete(provider)

        self.assertIs(raised.exception, primary_error)
        self.assertEqual(fallback.calls, 0)
        self.assertFalse(
            provider.last_call_metadata["fallback_used"]
        )

    def test_open_primary_circuit_skips_repeated_network_calls(self):
        primary = FakeBackend(
            "primary",
            "model-a",
            [TimeoutError(), TimeoutError()],
        )
        fallback_responses = [object(), object(), object()]
        fallback = FakeBackend(
            "fallback",
            "model-b",
            fallback_responses,
        )
        provider = ResilientProvider(
            primary,
            fallback,
            failure_threshold=2,
            cooldown_seconds=60,
        )

        self.assertIs(complete(provider), fallback_responses[0])
        self.assertIs(complete(provider), fallback_responses[1])
        self.assertIs(complete(provider), fallback_responses[2])

        self.assertEqual(primary.calls, 2)
        self.assertEqual(fallback.calls, 3)
        self.assertEqual(
            provider.last_call_metadata["failover_reason"],
            "primary_circuit_open",
        )
        self.assertEqual(
            provider.last_call_metadata["primary_circuit_state"],
            "open",
        )

    def test_half_open_probe_recovers_primary_after_cooldown(self):
        clock = FakeClock()
        recovered = object()
        primary = FakeBackend(
            "primary",
            "model-a",
            [TimeoutError(), recovered],
        )
        fallback = FakeBackend(
            "fallback",
            "model-b",
            [object()],
        )
        provider = ResilientProvider(
            primary,
            fallback,
            failure_threshold=1,
            cooldown_seconds=10,
            clock=clock,
        )

        complete(provider)
        clock.value = 11
        result = complete(provider)

        self.assertIs(result, recovered)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(fallback.calls, 1)
        self.assertFalse(
            provider.last_call_metadata["fallback_used"]
        )
        self.assertEqual(
            provider.last_call_metadata["primary_circuit_state"],
            "closed",
        )

    def test_no_fallback_fails_fast_while_primary_circuit_is_open(self):
        primary = FakeBackend(
            "primary",
            "model-a",
            [TimeoutError()],
        )
        provider = ResilientProvider(
            primary,
            failure_threshold=1,
            cooldown_seconds=60,
        )

        with self.assertRaises(TimeoutError):
            complete(provider)
        with self.assertRaises(ProviderCircuitOpenError):
            complete(provider)

        self.assertEqual(primary.calls, 1)
        self.assertFalse(
            provider.last_call_metadata["fallback_used"]
        )

    def test_fallback_failure_is_reported_without_error_message_leakage(self):
        primary = FakeBackend(
            "primary",
            "model-a",
            [TimeoutError("primary secret")],
        )
        fallback = FakeBackend(
            "fallback",
            "model-b",
            [TimeoutError("fallback secret")],
        )
        provider = ResilientProvider(primary, fallback)

        with self.assertRaisesRegex(
            ProviderFallbackError,
            "both failed",
        ) as raised:
            complete(provider)

        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(
            provider.last_call_metadata["fallback_error_type"],
            "TimeoutError",
        )


if __name__ == "__main__":
    unittest.main()
