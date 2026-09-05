from typing import Any

from threading import Lock, local
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CIRCUIT_COOLDOWN_SECONDS,
    LLM_CIRCUIT_FAILURE_THRESHOLD,
    LLM_FALLBACK_API_KEY,
    LLM_FALLBACK_BASE_URL,
    LLM_FALLBACK_MODEL,
    LLM_FALLBACK_PROVIDER,
    LLM_FALLBACK_REASONING_ENABLED,
    LLM_MAX_COMPLETION_TOKENS,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_REASONING_EFFORT,
    LLM_REASONING_ENABLED,
    LLM_TIMEOUT_SECONDS,
)


DEEPSEEK_DEFAULT_BASE_URL = (
    "https://api.deepseek.com"
)


class OpenAICompatibleProvider:
    """
    LLM provider adapter for APIs using
    the OpenAI Chat Completions interface.

    Provider-specific request behavior
    belongs here, not in agent.py.
    """

    def __init__(
        self,
        provider_name: str,
        api_key: str | None,
        model: str,
        base_url: str | None,
        reasoning_enabled: bool,
        reasoning_effort: str,
        timeout_seconds: float,
        max_retries: int,
        max_completion_tokens: int = 2_500,
    ) -> None:

        self.provider_name = (
            provider_name
        )

        self.model = model

        self.reasoning_enabled = (
            reasoning_enabled
        )

        self.reasoning_effort = (
            reasoning_effort
        )


        self.timeout_seconds = (
        timeout_seconds
            )

        self.max_retries = (
            max_retries
            )

        self.max_completion_tokens = (
            max_completion_tokens
        )


        if not api_key:

            raise RuntimeError(
                "No LLM API key configured. "
                "Set SAFEDBA_LLM_API_KEY "
                "or the provider-specific "
                "API key environment variable."
            )

        if not self.model:

            raise RuntimeError(
                "No LLM model configured. "
                "Set SAFEDBA_LLM_MODEL."
            )

        resolved_base_url = (
            self._resolve_base_url(
                base_url
            )
        )

        client_kwargs = {
        "api_key": api_key,
        "timeout": self.timeout_seconds,
        "max_retries": self.max_retries,
        }

        if resolved_base_url:

            client_kwargs[
                "base_url"
            ] = resolved_base_url

        self.client = OpenAI(
            **client_kwargs
        )

    def _resolve_base_url(
        self,
        configured_base_url: (
            str | None
        ),
    ) -> str | None:

        if configured_base_url:

            return configured_base_url

        if (
            self.provider_name
            == "deepseek"
        ):

            return (
                DEEPSEEK_DEFAULT_BASE_URL
            )

        if (
            self.provider_name
            == "openai_compatible"
        ):

            raise RuntimeError(
                "SAFEDBA_LLM_BASE_URL "
                "is required when "
                "SAFEDBA_LLM_PROVIDER="
                "openai_compatible."
            )

        raise RuntimeError(
            "Unsupported LLM provider: "
            f"{self.provider_name}"
        )

    def _apply_provider_options(
        self,
        request_kwargs: dict,
    ) -> None:

        # ------------------------------------
        # DeepSeek-specific options
        # ------------------------------------

        if (
            self.provider_name
            == "deepseek"
        ):

            request_kwargs[
                "extra_body"
            ] = {
                "thinking": {
                    "type": (
                        "enabled"
                        if (
                            self.reasoning_enabled
                        )
                        else "disabled"
                    )
                }
            }

            if self.reasoning_enabled:

                request_kwargs[
                    "reasoning_effort"
                ] = (
                    self.reasoning_effort
                )

            return

        # ------------------------------------
        # Generic OpenAI-compatible endpoint
        # ------------------------------------
        #
        # There is no universal reasoning
        # parameter shared by every provider.
        # Fail closed instead of sending a
        # provider-specific parameter blindly.

        if (
            self.provider_name
            == "openai_compatible"
            and self.reasoning_enabled
        ):

            raise RuntimeError(
                "Reasoning mode is not "
                "configured for the generic "
                "openai_compatible provider. "
                "Disable reasoning or add a "
                "provider-specific adapter."
            )

    def complete(
        self,
        *,
        messages: list,
        tools: list | None = None,
        tool_choice: str | None = None,
    ):

        request_kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": (
                self.max_completion_tokens
            ),
        }

        if tools is not None:

            request_kwargs[
                "tools"
            ] = tools

        if tool_choice is not None:

            request_kwargs[
                "tool_choice"
            ] = tool_choice

        self._apply_provider_options(
            request_kwargs
        )

        return (
            self.client
            .chat
            .completions
            .create(
                **request_kwargs
            )
        )

    @staticmethod
    def assistant_message_to_dict(
        message: Any,
    ) -> dict:
        """
        Serialize an assistant response back
        into conversation context.

        Provider-specific reasoning state is
        preserved when present, but is not
        exposed to the user or audit log.
        """

        message_dict = (
            message.model_dump(
                exclude_none=True
            )
        )

        reasoning_content = (
            getattr(
                message,
                "reasoning_content",
                None,
            )
        )

        if reasoning_content is not None:

            message_dict[
                "reasoning_content"
            ] = reasoning_content

        return message_dict


class ProviderCircuitOpenError(RuntimeError):
    """Raised when a provider is temporarily unavailable by policy."""


class ProviderFallbackError(RuntimeError):
    """Raised when an explicitly configured fallback also cannot respond."""


def is_transient_provider_error(exc: Exception) -> bool:
    """Return whether an error is safe to route around automatically."""

    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
            InternalServerError,
        ),
    ):
        return True

    if isinstance(exc, APIStatusError):
        status_code = getattr(exc, "status_code", None)
        return (
            status_code in {408, 429}
            or (
                isinstance(status_code, int)
                and status_code >= 500
            )
        )

    return False


class _ProviderCircuit:
    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        clock,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False

    def try_acquire(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if (
                self._clock() - self._opened_at
                < self._cooldown_seconds
            ):
                return False
            if self._half_open_in_flight:
                return False
            self._half_open_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_in_flight = False

    def record_non_transient_response(self) -> None:
        # A deterministic 4xx or local request error is not an outage. It
        # propagates, but proves a prior transient outage is no longer a
        # reason to keep the circuit open.
        self.record_success()

    def record_transient_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._half_open_in_flight
                or self._consecutive_failures
                >= self._failure_threshold
            ):
                self._opened_at = self._clock()
            self._half_open_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if (
                self._clock() - self._opened_at
                >= self._cooldown_seconds
            ):
                return "half_open"
            return "open"


class ResilientProvider:
    """Explicit transient-error failover with per-route circuit breakers."""

    def __init__(
        self,
        primary,
        fallback=None,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive.")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive.")
        self.primary = primary
        self.fallback = fallback
        self.model = primary.model
        self.provider_name = primary.provider_name
        self._primary_circuit = _ProviderCircuit(
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            clock=clock,
        )
        self._fallback_circuit = (
            _ProviderCircuit(
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                clock=clock,
            )
            if fallback is not None
            else None
        )
        self._call_state = local()

    @property
    def last_call_metadata(self) -> dict:
        return dict(
            getattr(self._call_state, "metadata", {})
        )

    def _save_metadata(self, metadata: dict) -> None:
        metadata["primary_circuit_state"] = (
            self._primary_circuit.state
        )
        metadata["fallback_circuit_state"] = (
            self._fallback_circuit.state
            if self._fallback_circuit is not None
            else "not_configured"
        )
        self._call_state.metadata = dict(metadata)

    @staticmethod
    def _route_identity(provider) -> dict:
        return {
            "selected_provider": getattr(
                provider,
                "provider_name",
                None,
            ),
            "selected_model": getattr(provider, "model", None),
        }

    def _call_fallback(
        self,
        metadata: dict,
        *,
        messages: list,
        tools: list | None,
        tool_choice: str | None,
    ):
        if (
            self.fallback is None
            or self._fallback_circuit is None
        ):
            self._save_metadata(metadata)
            raise ProviderCircuitOpenError(
                "Primary provider circuit is open and no fallback is "
                "configured."
            )
        metadata["fallback_used"] = True
        if not self._fallback_circuit.try_acquire():
            metadata["fallback_error_type"] = (
                "ProviderCircuitOpenError"
            )
            self._save_metadata(metadata)
            raise ProviderFallbackError(
                "The configured fallback provider circuit is open."
            )

        try:
            response = self.fallback.complete(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )
        except Exception as exc:
            metadata["fallback_error_type"] = type(exc).__name__
            if is_transient_provider_error(exc):
                self._fallback_circuit.record_transient_failure()
            else:
                self._fallback_circuit.record_non_transient_response()
            self._save_metadata(metadata)
            raise ProviderFallbackError(
                "Primary and fallback provider routes both failed."
            ) from exc

        self._fallback_circuit.record_success()
        metadata.update(self._route_identity(self.fallback))
        self._save_metadata(metadata)
        return response

    def complete(
        self,
        *,
        messages: list,
        tools: list | None = None,
        tool_choice: str | None = None,
    ):
        metadata = {
            "fallback_configured": self.fallback is not None,
            "fallback_used": False,
        }

        if not self._primary_circuit.try_acquire():
            metadata["failover_reason"] = "primary_circuit_open"
            metadata["primary_error_type"] = (
                "ProviderCircuitOpenError"
            )
            return self._call_fallback(
                metadata,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )

        try:
            response = self.primary.complete(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )
        except Exception as exc:
            metadata["primary_error_type"] = type(exc).__name__
            if not is_transient_provider_error(exc):
                self._primary_circuit.record_non_transient_response()
                self._save_metadata(metadata)
                raise

            self._primary_circuit.record_transient_failure()
            metadata["failover_reason"] = "primary_transient_error"
            if self.fallback is None:
                self._save_metadata(metadata)
                raise
            return self._call_fallback(
                metadata,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )

        self._primary_circuit.record_success()
        metadata.update(self._route_identity(self.primary))
        self._save_metadata(metadata)
        return response

    @staticmethod
    def assistant_message_to_dict(message: Any) -> dict:
        return OpenAICompatibleProvider.assistant_message_to_dict(
            message
        )


_provider = None


def get_llm_provider(
) -> ResilientProvider:
    """
    Lazily construct the configured provider.

    Lazy initialization prevents importing
    SafeDBA modules from immediately requiring
    an API key.
    """

    global _provider

    if _provider is None:

        if LLM_PROVIDER not in {
            "deepseek",
            "openai_compatible",
        }:

            raise RuntimeError(
                "Unsupported LLM provider: "
                f"{LLM_PROVIDER}"
            )

        primary = OpenAICompatibleProvider(
            provider_name=LLM_PROVIDER,
            api_key=LLM_API_KEY,
            model=LLM_MODEL,
            base_url=LLM_BASE_URL,
            reasoning_enabled=LLM_REASONING_ENABLED,
            reasoning_effort=LLM_REASONING_EFFORT,
            timeout_seconds=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
            max_completion_tokens=LLM_MAX_COMPLETION_TOKENS,
        )

        fallback = None
        if LLM_FALLBACK_PROVIDER is not None:
            fallback = OpenAICompatibleProvider(
                provider_name=LLM_FALLBACK_PROVIDER,
                api_key=LLM_FALLBACK_API_KEY,
                model=LLM_FALLBACK_MODEL,
                base_url=LLM_FALLBACK_BASE_URL,
                reasoning_enabled=(
                    LLM_FALLBACK_REASONING_ENABLED
                ),
                reasoning_effort=LLM_REASONING_EFFORT,
                timeout_seconds=LLM_TIMEOUT_SECONDS,
                max_retries=LLM_MAX_RETRIES,
                max_completion_tokens=(
                    LLM_MAX_COMPLETION_TOKENS
                ),
            )

        _provider = ResilientProvider(
            primary,
            fallback,
            failure_threshold=(
                LLM_CIRCUIT_FAILURE_THRESHOLD
            ),
            cooldown_seconds=(
                LLM_CIRCUIT_COOLDOWN_SECONDS
            ),
        )

    return _provider
