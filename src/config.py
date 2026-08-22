import os
import math
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

    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(
            f"{name} must be a finite number."
        )
    return parsed


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
        "safedba_observer",
    ),

    "password": os.getenv(
        "SAFEDBA_DB_PASSWORD",
        "local-observer-only",
    ),

    "connect_timeout": env_int(
        "SAFEDBA_DB_CONNECT_TIMEOUT_SECONDS",
        10,
    ),
}


# Controlled mutations can use a separate, more privileged database
# identity. Production deployments should configure both identities
# explicitly and keep the observer role read-only.
EXECUTOR_DB_CONFIG = {
    "host": os.getenv(
        "SAFEDBA_EXECUTOR_DB_HOST",
        DB_CONFIG["host"],
    ),
    "port": env_int(
        "SAFEDBA_EXECUTOR_DB_PORT",
        DB_CONFIG["port"],
    ),
    "dbname": os.getenv(
        "SAFEDBA_EXECUTOR_DB_NAME",
        DB_CONFIG["dbname"],
    ),
    "user": os.getenv(
        "SAFEDBA_EXECUTOR_DB_USER",
        "safedba_executor",
    ),
    "password": os.getenv(
        "SAFEDBA_EXECUTOR_DB_PASSWORD",
        "local-executor-only",
    ),
    "connect_timeout": env_int(
        "SAFEDBA_EXECUTOR_DB_CONNECT_TIMEOUT_SECONDS",
        DB_CONFIG["connect_timeout"],
    ),
}


# Session termination is isolated from DDL/table-maintenance privileges.
# This role needs only visibility into current activity plus
# pg_signal_backend; it must not own application objects.
TERMINATOR_DB_CONFIG = {
    "host": os.getenv(
        "SAFEDBA_TERMINATOR_DB_HOST",
        DB_CONFIG["host"],
    ),
    "port": env_int(
        "SAFEDBA_TERMINATOR_DB_PORT",
        DB_CONFIG["port"],
    ),
    "dbname": os.getenv(
        "SAFEDBA_TERMINATOR_DB_NAME",
        DB_CONFIG["dbname"],
    ),
    "user": os.getenv(
        "SAFEDBA_TERMINATOR_DB_USER",
        "safedba_terminator",
    ),
    "password": os.getenv(
        "SAFEDBA_TERMINATOR_DB_PASSWORD",
        "local-terminator-only",
    ),
    "connect_timeout": env_int(
        "SAFEDBA_TERMINATOR_DB_CONNECT_TIMEOUT_SECONDS",
        DB_CONFIG["connect_timeout"],
    ),
}


DB_STATEMENT_TIMEOUT_MS = env_int(
    "SAFEDBA_DB_STATEMENT_TIMEOUT_MS",
    30_000,
)

DB_LOCK_TIMEOUT_MS = env_int(
    "SAFEDBA_DB_LOCK_TIMEOUT_MS",
    3_000,
)

DB_MAX_QUERY_LENGTH = env_int(
    "SAFEDBA_DB_MAX_QUERY_LENGTH",
    50_000,
)

DB_MAX_EXPLAIN_TOTAL_COST = env_float(
    "SAFEDBA_DB_MAX_EXPLAIN_TOTAL_COST",
    1_000_000.0,
)

DB_MAX_OBSERVATION_ROWS = env_int(
    "SAFEDBA_DB_MAX_OBSERVATION_ROWS",
    50,
)

DB_MAX_OBSERVED_QUERY_CHARS = env_int(
    "SAFEDBA_DB_MAX_OBSERVED_QUERY_CHARS",
    2_000,
)


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


DB_INCLUDE_OBSERVED_QUERY_TEXT = env_bool(
    "SAFEDBA_DB_INCLUDE_OBSERVED_QUERY_TEXT",
    False,
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

LLM_MAX_COMPLETION_TOKENS = env_int(
    "SAFEDBA_LLM_MAX_COMPLETION_TOKENS",
    2_500,
)


AGENT_MAX_TOTAL_TOOL_CALLS = env_int(
    "SAFEDBA_AGENT_MAX_TOTAL_TOOL_CALLS",
    16,
)

AGENT_MAX_TOOL_CALLS_PER_TURN = env_int(
    "SAFEDBA_AGENT_MAX_TOOL_CALLS_PER_TURN",
    6,
)

AGENT_MAX_TOOL_OUTPUT_CHARS = env_int(
    "SAFEDBA_AGENT_MAX_TOOL_OUTPUT_CHARS",
    60_000,
)

AGENT_DEADLINE_SECONDS = env_float(
    "SAFEDBA_AGENT_DEADLINE_SECONDS",
    240.0,
)

AGENT_RUNTIME_EVIDENCE_TTL_SECONDS = env_float(
    "SAFEDBA_AGENT_RUNTIME_EVIDENCE_TTL_SECONDS",
    15.0,
)


# ----------------------------------------
# Agent runtime state and memory
# ----------------------------------------

_agent_state_db_value = os.getenv(
    "SAFEDBA_AGENT_STATE_DB_PATH",
    "logs/agent_state.sqlite3",
)

AGENT_STATE_DB_PATH = (
    PROJECT_ROOT
    / Path(
        _agent_state_db_value
    )
)

AGENT_MEMORY_ENABLED = env_bool(
    "SAFEDBA_AGENT_MEMORY_ENABLED",
    True,
)

AGENT_MEMORY_MAX_SESSION_TURNS = env_int(
    "SAFEDBA_AGENT_MEMORY_MAX_SESSION_TURNS",
    12,
)

AGENT_MEMORY_MAX_RELEVANT_EPISODES = env_int(
    "SAFEDBA_AGENT_MEMORY_MAX_RELEVANT_EPISODES",
    4,
)

AGENT_MEMORY_TTL_SECONDS = env_int(
    "SAFEDBA_AGENT_MEMORY_TTL_SECONDS",
    30 * 24 * 60 * 60,
)


# ----------------------------------------
# Controlled offline experience loop
# ----------------------------------------

_experience_db_value = os.getenv(
    "SAFEDBA_EXPERIENCE_DB_PATH",
    "logs/experience.sqlite3",
)

EXPERIENCE_DB_PATH = (
    PROJECT_ROOT
    / Path(
        _experience_db_value
    )
)

EXPERIENCE_CAPTURE_ENABLED = env_bool(
    "SAFEDBA_EXPERIENCE_CAPTURE_ENABLED",
    True,
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
# Durable incident workflow
# ----------------------------------------

_incident_state_db_value = os.getenv(
    "SAFEDBA_INCIDENT_STATE_DB_PATH",
    "logs/incidents.sqlite3",
)

INCIDENT_STATE_DB_PATH = (
    PROJECT_ROOT
    / Path(
        _incident_state_db_value
    )
)

INCIDENT_APPROVAL_TTL_SECONDS = env_float(
    "SAFEDBA_INCIDENT_APPROVAL_TTL_SECONDS",
    120.0,
)

INCIDENT_MAX_ACTIONS = env_int(
    "SAFEDBA_INCIDENT_MAX_ACTIONS",
    10,
)

INCIDENT_LEASE_SECONDS = env_float(
    "SAFEDBA_INCIDENT_LEASE_SECONDS",
    60.0,
)

INCIDENT_DEADLINE_SECONDS = env_float(
    "SAFEDBA_INCIDENT_DEADLINE_SECONDS",
    300.0,
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


AUDIT_INCLUDE_QUERY_TEXT = env_bool(
    "SAFEDBA_AUDIT_INCLUDE_QUERY_TEXT",
    False,
)


SAFEDBA_ENV = os.getenv(
    "SAFEDBA_ENV",
    "development",
).strip().lower()


def validate_settings() -> None:
    positive_values = {
        "SAFEDBA_DB_CONNECT_TIMEOUT_SECONDS": (
            DB_CONFIG["connect_timeout"]
        ),
        "SAFEDBA_EXECUTOR_DB_CONNECT_TIMEOUT_SECONDS": (
            EXECUTOR_DB_CONFIG["connect_timeout"]
        ),
        "SAFEDBA_TERMINATOR_DB_CONNECT_TIMEOUT_SECONDS": (
            TERMINATOR_DB_CONFIG["connect_timeout"]
        ),
        "SAFEDBA_DB_STATEMENT_TIMEOUT_MS": (
            DB_STATEMENT_TIMEOUT_MS
        ),
        "SAFEDBA_DB_LOCK_TIMEOUT_MS": (
            DB_LOCK_TIMEOUT_MS
        ),
        "SAFEDBA_DB_MAX_QUERY_LENGTH": (
            DB_MAX_QUERY_LENGTH
        ),
        "SAFEDBA_DB_MAX_EXPLAIN_TOTAL_COST": (
            DB_MAX_EXPLAIN_TOTAL_COST
        ),
        "SAFEDBA_DB_MAX_OBSERVATION_ROWS": (
            DB_MAX_OBSERVATION_ROWS
        ),
        "SAFEDBA_DB_MAX_OBSERVED_QUERY_CHARS": (
            DB_MAX_OBSERVED_QUERY_CHARS
        ),
        "SAFEDBA_LLM_TIMEOUT_SECONDS": (
            LLM_TIMEOUT_SECONDS
        ),
        "SAFEDBA_LLM_MAX_COMPLETION_TOKENS": (
            LLM_MAX_COMPLETION_TOKENS
        ),
        "SAFEDBA_AGENT_MAX_TOTAL_TOOL_CALLS": (
            AGENT_MAX_TOTAL_TOOL_CALLS
        ),
        "SAFEDBA_AGENT_MAX_TOOL_CALLS_PER_TURN": (
            AGENT_MAX_TOOL_CALLS_PER_TURN
        ),
        "SAFEDBA_AGENT_MAX_TOOL_OUTPUT_CHARS": (
            AGENT_MAX_TOOL_OUTPUT_CHARS
        ),
        "SAFEDBA_AGENT_DEADLINE_SECONDS": (
            AGENT_DEADLINE_SECONDS
        ),
        "SAFEDBA_AGENT_RUNTIME_EVIDENCE_TTL_SECONDS": (
            AGENT_RUNTIME_EVIDENCE_TTL_SECONDS
        ),
        "SAFEDBA_AGENT_MEMORY_MAX_SESSION_TURNS": (
            AGENT_MEMORY_MAX_SESSION_TURNS
        ),
        "SAFEDBA_AGENT_MEMORY_MAX_RELEVANT_EPISODES": (
            AGENT_MEMORY_MAX_RELEVANT_EPISODES
        ),
        "SAFEDBA_AGENT_MEMORY_TTL_SECONDS": (
            AGENT_MEMORY_TTL_SECONDS
        ),
        "SAFEDBA_MIN_IMPROVEMENT_PCT": (
            MIN_IMPROVEMENT_PCT
        ),
        "SAFEDBA_MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO": (
            MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO
        ),
        "SAFEDBA_HEALTH_LONG_QUERY_SECONDS": (
            HEALTH_LONG_QUERY_SECONDS
        ),
        "SAFEDBA_HEALTH_LONG_TRANSACTION_SECONDS": (
            HEALTH_LONG_TRANSACTION_SECONDS
        ),
        "SAFEDBA_INCIDENT_APPROVAL_TTL_SECONDS": (
            INCIDENT_APPROVAL_TTL_SECONDS
        ),
        "SAFEDBA_INCIDENT_MAX_ACTIONS": (
            INCIDENT_MAX_ACTIONS
        ),
        "SAFEDBA_INCIDENT_LEASE_SECONDS": (
            INCIDENT_LEASE_SECONDS
        ),
        "SAFEDBA_INCIDENT_DEADLINE_SECONDS": (
            INCIDENT_DEADLINE_SECONDS
        ),
    }

    invalid = [
        name
        for name, value in positive_values.items()
        if value <= 0
    ]

    if invalid:
        raise ValueError(
            "Configuration values must be positive: "
            + ", ".join(invalid)
        )

    upper_bounds = {
        "SAFEDBA_DB_CONNECT_TIMEOUT_SECONDS": 120,
        "SAFEDBA_EXECUTOR_DB_CONNECT_TIMEOUT_SECONDS": 120,
        "SAFEDBA_TERMINATOR_DB_CONNECT_TIMEOUT_SECONDS": 120,
        "SAFEDBA_DB_STATEMENT_TIMEOUT_MS": 300_000,
        "SAFEDBA_DB_LOCK_TIMEOUT_MS": 30_000,
        "SAFEDBA_DB_MAX_QUERY_LENGTH": 1_000_000,
        "SAFEDBA_DB_MAX_EXPLAIN_TOTAL_COST": 10_000_000,
        "SAFEDBA_DB_MAX_OBSERVATION_ROWS": 1_000,
        "SAFEDBA_DB_MAX_OBSERVED_QUERY_CHARS": 100_000,
        "SAFEDBA_LLM_TIMEOUT_SECONDS": 600,
        "SAFEDBA_LLM_MAX_COMPLETION_TOKENS": 100_000,
        "SAFEDBA_AGENT_MAX_TOTAL_TOOL_CALLS": 100,
        "SAFEDBA_AGENT_MAX_TOOL_CALLS_PER_TURN": 20,
        "SAFEDBA_AGENT_MAX_TOOL_OUTPUT_CHARS": 1_000_000,
        "SAFEDBA_AGENT_DEADLINE_SECONDS": 900,
        "SAFEDBA_AGENT_RUNTIME_EVIDENCE_TTL_SECONDS": 120,
        "SAFEDBA_AGENT_MEMORY_MAX_SESSION_TURNS": 100,
        "SAFEDBA_AGENT_MEMORY_MAX_RELEVANT_EPISODES": 20,
        "SAFEDBA_AGENT_MEMORY_TTL_SECONDS": 365 * 24 * 60 * 60,
        "SAFEDBA_MIN_IMPROVEMENT_PCT": 100,
        "SAFEDBA_MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO": 1_000_000,
        "SAFEDBA_HEALTH_LONG_QUERY_SECONDS": 86_400,
        "SAFEDBA_HEALTH_LONG_TRANSACTION_SECONDS": 86_400,
        "SAFEDBA_INCIDENT_APPROVAL_TTL_SECONDS": 3_600,
        "SAFEDBA_INCIDENT_MAX_ACTIONS": 100,
        "SAFEDBA_INCIDENT_LEASE_SECONDS": 600,
        "SAFEDBA_INCIDENT_DEADLINE_SECONDS": 3_600,
    }
    excessive = [
        name
        for name, maximum in upper_bounds.items()
        if positive_values[name] > maximum
    ]
    if excessive:
        raise ValueError(
            "Configuration values exceed safety bounds: "
            + ", ".join(excessive)
        )

    if AGENT_MAX_TOOL_OUTPUT_CHARS < 256:
        raise ValueError(
            "SAFEDBA_AGENT_MAX_TOOL_OUTPUT_CHARS must be at least 256."
        )

    runtime_users = {
        str(DB_CONFIG["user"]).strip().lower(),
        str(EXECUTOR_DB_CONFIG["user"]).strip().lower(),
        str(TERMINATOR_DB_CONFIG["user"]).strip().lower(),
    }
    if len(runtime_users) != 3:
        raise ValueError(
            "Observer, executor, and terminator database users "
            "must be three distinct identities."
        )

    if LLM_MAX_RETRIES < 0:
        raise ValueError(
            "SAFEDBA_LLM_MAX_RETRIES must be zero or greater."
        )
    if LLM_MAX_RETRIES > 10:
        raise ValueError(
            "SAFEDBA_LLM_MAX_RETRIES exceeds the safety bound of 10."
        )

    if MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO < 1:
        raise ValueError(
            "SAFEDBA_MAX_ACCEPTABLE_CARDINALITY_ERROR_RATIO "
            "must be at least 1."
        )

    for label, config in (
        ("observer", DB_CONFIG),
        ("executor", EXECUTOR_DB_CONFIG),
        ("terminator", TERMINATOR_DB_CONFIG),
    ):
        if not (1 <= config["port"] <= 65_535):
            raise ValueError(
                f"{label} database port must be between 1 and 65535."
            )

    if (
        AGENT_MAX_TOOL_CALLS_PER_TURN
        > AGENT_MAX_TOTAL_TOOL_CALLS
    ):
        raise ValueError(
            "Per-turn tool-call budget cannot exceed the total budget."
        )


validate_settings()
