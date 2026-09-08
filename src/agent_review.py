"""Read-only explanation of a deterministic executor result."""

import json
from agent_prompts import EXECUTION_REVIEW_INSTRUCTIONS
from langchain_bridge import LangChainProvider
from runtime_policy import RuntimePolicyError


def review_execution_result(
    proposal: dict,
    execution_result: dict,
    *,
    chat_model=None,
    get_provider,
    require_operation
) -> str:
    try:
        require_operation("AGENT_RUN")
    except RuntimePolicyError:
        return "LLM review skipped by runtime policy. Refer to the deterministic execution result; no additional review request was sent."
    evidence = {"proposal": proposal, "execution_result": execution_result}
    provider = LangChainProvider(
        provider=get_provider() if chat_model is None else None, chat_model=chat_model
    )
    response = provider.complete(
        messages=[
            {"role": "system", "content": EXECUTION_REVIEW_INSTRUCTIONS},
            {
                "role": "user",
                "content": json.dumps(
                    evidence, ensure_ascii=False, default=str, indent=2
                ),
            },
        ]
    )
    return response.choices[0].message.content or ""
