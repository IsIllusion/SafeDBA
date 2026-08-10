from typing import Any

from openai import OpenAI

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
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


_provider = None


def get_llm_provider(
) -> OpenAICompatibleProvider:
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

        _provider = (
            OpenAICompatibleProvider(
                provider_name=LLM_PROVIDER,
                api_key=LLM_API_KEY,
                model=LLM_MODEL,
                base_url=LLM_BASE_URL,
                reasoning_enabled=(
                    LLM_REASONING_ENABLED
                ),
                reasoning_effort=(
                    LLM_REASONING_EFFORT
                ),
                timeout_seconds=(
                    LLM_TIMEOUT_SECONDS
                ),
                max_retries=(
                    LLM_MAX_RETRIES
                ),
            )
        )

    return _provider