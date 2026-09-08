"""Pure deterministic PostgreSQL naming and identifier predicates."""

import hashlib
import uuid


def build_index_name(table: str, column: str) -> str:
    """Build a stable PostgreSQL identifier no longer than 63 bytes."""
    base = f"idx_{table}_{column}"
    if len(base.encode("utf-8")) <= 63:
        return base
    suffix = "_" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:10]
    byte_budget = 63 - len(suffix.encode("utf-8"))
    prefix_bytes = base.encode("utf-8")[:byte_budget]
    while True:
        try:
            prefix = prefix_bytes.decode("utf-8")
            break
        except UnicodeDecodeError:
            prefix_bytes = prefix_bytes[:-1]
    return prefix + suffix


def is_valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def is_positive_int(value: object) -> bool:
    return isinstance(value, int) and (not isinstance(value, bool)) and (value > 0)
