import json

import hashlib
import os

from config import (
    AUDIT_INCLUDE_QUERY_TEXT,
    AUDIT_LOG_PATH,
)


_QUERY_KEYS = {
    "query",
    "original_query",
    "rewritten_query",
    "blocked_query",
    "blocker_query",
}

_SECRET_KEY_PARTS = {
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
}

_DATA_DISTRIBUTION_KEYS = {
    "histogram_bounds",
    "most_common_vals",
}


def _fingerprint_text(value: str) -> dict:
    return {
        "redacted": True,
        "sha256": hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest(),
        "characters": len(value),
    }


def sanitize_audit_value(
    value,
    *,
    key: str | None = None,
):
    normalized_key = (
        key.lower()
        if isinstance(key, str)
        else None
    )

    if (
        normalized_key
        and any(
            part in normalized_key
            for part in _SECRET_KEY_PARTS
        )
    ):
        return "[REDACTED]"

    if (
        normalized_key in _QUERY_KEYS
        and isinstance(value, str)
        and not AUDIT_INCLUDE_QUERY_TEXT
    ):
        return _fingerprint_text(value)

    if (
        normalized_key in _DATA_DISTRIBUTION_KEYS
        and value is not None
    ):
        return {
            "redacted": True,
            "sha256": hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
        }

    if isinstance(value, dict):
        return {
            str(child_key): sanitize_audit_value(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value
            in value.items()
        }

    if isinstance(value, list):
        return [
            sanitize_audit_value(item)
            for item in value
        ]

    return value


def write_audit_log(record: dict) -> None:
    AUDIT_LOG_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    sanitized = sanitize_audit_value(
        record
    )
    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"

    with AUDIT_LOG_PATH.open(
        "a",
        encoding="utf-8",
    ) as f:
        f.write(serialized)
        f.flush()
        os.fsync(f.fileno())
