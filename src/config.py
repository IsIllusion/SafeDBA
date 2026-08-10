import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)


# Load local development configuration.
#
# Existing operating-system environment
# variables take precedence over .env.
load_dotenv(
    PROJECT_ROOT / ".env"
)


def env_int(
    name: str,
    default: int,
) -> int:

    value = os.getenv(
        name
    )

    if value is None:
        return default

    return int(
        value
    )


def env_float(
    name: str,
    default: float,
) -> float:

    value = os.getenv(
        name
    )

    if value is None:
        return default

    return float(
        value
    )


# ----------------------------------------
# PostgreSQL
# ----------------------------------------

DB_CONFIG = {
    "host": os.getenv(
        "SAFEDBA_DB_HOST",
        "127.0.0.1",
    ),

    #A port without crash
    "port": env_int(
        "SAFEDBA_DB_PORT",
        15432,
    ),

    "dbname": os.getenv(
        "SAFEDBA_DB_NAME",
        "benchmark",
    ),

    "user": os.getenv(
        "SAFEDBA_DB_USER",
        "safedba",
    ),

    "password": os.getenv(
        "SAFEDBA_DB_PASSWORD",
        "",
    ),
}


# ----------------------------------------
# LLM
# ----------------------------------------


def env_bool(
    name: str,
    default: bool,
) -> bool:

    value = os.getenv(name)

    if value is None:
        return default

    normalized = (
        value.strip().lower()
    )

    if normalized in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }:
        return False

    raise ValueError(
        f"Invalid boolean value "
        f"for {name}: {value}"
    )


LLM_PROVIDER = os.getenv(
    "SAFEDBA_LLM_PROVIDER",
    "deepseek",
).strip().lower()


_default_llm_model = (
    "deepseek-v4-flash"
    if LLM_PROVIDER == "deepseek"
    else ""
)


LLM_MODEL = os.getenv(
    "SAFEDBA_LLM_MODEL",
    _default_llm_model,
).strip()


LLM_BASE_URL = os.getenv(
    "SAFEDBA_LLM_BASE_URL"
)


def resolve_llm_api_key(
) -> str | None:

    generic_key = os.getenv(
        "SAFEDBA_LLM_API_KEY"
    )

    if generic_key:
        return generic_key

    if LLM_PROVIDER == "deepseek":

        return os.getenv(
            "DEEPSEEK_API_KEY"
        )

    return None


LLM_API_KEY = (
    resolve_llm_api_key()
)


LLM_REASONING_ENABLED = env_bool(
    "SAFEDBA_LLM_REASONING_ENABLED",
    False,
)


LLM_REASONING_EFFORT = os.getenv(
    "SAFEDBA_LLM_REASONING_EFFORT",
    "high",
).strip().lower()


LLM_TIMEOUT_SECONDS = env_float(
    "SAFEDBA_LLM_TIMEOUT_SECONDS",
    120.0,
)


LLM_MAX_RETRIES = env_int(
    "SAFEDBA_LLM_MAX_RETRIES",
    1,
)

# ----------------------------------------
# SafeDBA policy
# ----------------------------------------

MIN_IMPROVEMENT_PCT = env_float(
    "SAFEDBA_MIN_IMPROVEMENT_PCT",
    10.0,
)

MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO = (
    env_float(
        "SAFEDBA_MAX_CARDINALITY_ERROR_RATIO",
        2.0,
    )
)

HEALTH_LONG_QUERY_SECONDS = env_float(
    "SAFEDBA_HEALTH_LONG_QUERY_SECONDS",
    30.0,
)

HEALTH_LONG_TRANSACTION_SECONDS = env_float(
    "SAFEDBA_HEALTH_LONG_TRANSACTION_SECONDS",
    60.0,
)


# ----------------------------------------
# Audit
# ----------------------------------------

_audit_log_value = os.getenv(
    "SAFEDBA_AUDIT_LOG_PATH",
    "logs/audit.jsonl",
)

AUDIT_LOG_PATH = (
    PROJECT_ROOT
    / Path(
        _audit_log_value
    )
)