"""Read-only observation dependencies, supplied by the public DB facade.

These are internal composition contracts, not a replacement for database roles
or OS isolation. No deployment config, credentials or write operations live here.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ObservationDependencies:
    """One invocation's settings and read-only callbacks; no global state."""

    connect: Callable[[], AbstractContextManager[Any]]
    redact_query: Callable[[str | None], str | None]
    health: Callable[[], dict]
    now: Callable[[], datetime]
    database_name: str | None
    max_rows: int
    long_query_seconds: float
    long_transaction_seconds: float
