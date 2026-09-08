from state_database import open_state_database
import json
import re
import sqlite3
import uuid

from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable


SCHEMA_VERSION = 1

MEMORY_KINDS = {"turn", "episode"}
RUNNING_RUN_STATES = {"RUNNING", "PAUSED"}
TERMINAL_RUN_STATES = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}

_SECRET_KEY_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|"
    r"access[_-]?token|refresh[_-]?token|authorization|token)"
    r"\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_SECRET = re.compile(
    r"(?i)\bbearer\s+[a-z0-9._~+\-/=]+"
)
_URI_PASSWORD = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/\s]+:)"
    r"([^@/\s]+)(@)"
)
_API_TOKEN = re.compile(
    r"\b(?:sk|pk)-[a-zA-Z0-9_-]{12,}\b"
)
_TERM_PATTERN = re.compile(
    r"[a-zA-Z0-9_]+|[\u3400-\u9fff]"
)


class AgentMemoryError(RuntimeError):
    pass


class MemoryValidationError(AgentMemoryError):
    pass


class MemoryStorageError(AgentMemoryError):
    pass


class RunNotFound(AgentMemoryError):
    pass


class RunVersionConflict(AgentMemoryError):
    pass


class RunStateError(AgentMemoryError):
    pass


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise MemoryValidationError(
            "Memory timestamps must be timezone-aware."
        )
    return value.astimezone(timezone.utc).isoformat()


def _redact_text(value: str) -> str:
    redacted = _BEARER_SECRET.sub(
        "Bearer [REDACTED]",
        value,
    )
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}[REDACTED]"
        ),
        redacted,
    )
    redacted = _URI_PASSWORD.sub(
        r"\1[REDACTED]\3",
        redacted,
    )
    return _API_TOKEN.sub(
        "[REDACTED_API_TOKEN]",
        redacted,
    )


def _redact_value(value, *, key: str | None = None):
    normalized_key = (
        key.casefold().replace("-", "_")
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

    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            str(child_key): _redact_value(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_value(item)
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _redact_value(item)
            for item in value
        ]
    return value


def _strict_json_dump(
    value,
    *,
    field: str,
    max_chars: int,
) -> str:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MemoryValidationError(
            f"{field} must be strict JSON-safe data."
        ) from exc
    if len(serialized) > max_chars:
        raise MemoryValidationError(
            f"{field} exceeds {max_chars} serialized characters."
        )
    return serialized


def _json_load(value: str, *, field: str):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MemoryStorageError(
            f"Stored {field} is not valid JSON."
        ) from exc


def _terms(value: str) -> Counter:
    return Counter(
        _TERM_PATTERN.findall(value.casefold())
    )


def _validate_identifier(
    value: object,
    *,
    field: str,
    max_chars: int = 256,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryValidationError(
            f"{field} must be a non-empty string."
        )
    normalized = value.strip()
    if len(normalized) > max_chars:
        raise MemoryValidationError(
            f"{field} exceeds {max_chars} characters."
        )
    return normalized


def _validate_limit(value: object, *, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise MemoryValidationError(
            f"{field} must be a positive integer."
        )
    return value


class SQLiteAgentMemory:
    """A bounded, scoped, secret-redacting Agent memory store."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        default_ttl_seconds: int | None = 30 * 24 * 60 * 60,
        max_entries_per_thread: int = 1000,
        max_content_chars: int = 8000,
        max_json_chars: int = 16000,
        redact_secrets: bool = True,
    ) -> None:
        if str(path) == ":memory:":
            raise MemoryValidationError(
                "SQLiteAgentMemory requires a file-backed database."
            )
        self.path = Path(path)
        self.clock = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self.default_ttl_seconds = self._validate_ttl(
            default_ttl_seconds,
            allow_none=True,
        )
        self.max_entries_per_thread = _validate_limit(
            max_entries_per_thread,
            field="max_entries_per_thread",
        )
        self.max_content_chars = _validate_limit(
            max_content_chars,
            field="max_content_chars",
        )
        self.max_json_chars = _validate_limit(
            max_json_chars,
            field="max_json_chars",
        )
        if not isinstance(redact_secrets, bool):
            raise MemoryValidationError(
                "redact_secrets must be a boolean."
            )
        self.redact_secrets = redact_secrets

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._initialize()

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise MemoryValidationError(
                "clock must return a datetime."
            )
        _utc_iso(value)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_ttl(
        value: object,
        *,
        allow_none: bool,
    ) -> int | None:
        if value is None and allow_none:
            return None
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise MemoryValidationError(
                "ttl_seconds must be a positive integer or None."
            )
        return value

    def _connect(self) -> sqlite3.Connection:
        return open_state_database(self.path)

    @contextmanager
    def _transaction(
        self,
        connection: sqlite3.Connection,
    ):
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "PRAGMA journal_mode = WAL"
            )
            with self._transaction(connection):
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_memory_schema_meta (
                        schema_version INTEGER NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT schema_version "
                    "FROM agent_memory_schema_meta"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO agent_memory_schema_meta("
                        "schema_version) "
                        "VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                elif row["schema_version"] != SCHEMA_VERSION:
                    raise MemoryStorageError(
                        "Unsupported Agent memory schema version."
                    )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        memory_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        memory_kind TEXT NOT NULL CHECK (
                            memory_kind IN ('turn', 'episode')
                        ),
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_memories_session_recent
                    ON memories(
                        thread_id,
                        session_id,
                        created_at DESC
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_memories_thread_expiry
                    ON memories(thread_id, expires_at)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_runs (
                        run_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'RUNNING',
                                'PAUSED',
                                'COMPLETED',
                                'FAILED',
                                'CANCELLED'
                            )
                        ),
                        version INTEGER NOT NULL CHECK (version > 0),
                        checkpoint_json TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_runs_session
                    ON agent_runs(thread_id, session_id, updated_at DESC)
                    """
                )

    def _prepare_json_object(
        self,
        value: object | None,
        *,
        field: str,
    ) -> str:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise MemoryValidationError(
                f"{field} must be a JSON object."
            )
        prepared = (
            _redact_value(value)
            if self.redact_secrets
            else value
        )
        return _strict_json_dump(
            prepared,
            field=field,
            max_chars=self.max_json_chars,
        )

    def _prepare_provenance(self, value: object) -> str:
        if not isinstance(value, dict):
            raise MemoryValidationError(
                "provenance must be a JSON object."
            )
        source = value.get("source")
        _validate_identifier(
            source,
            field="provenance.source",
        )
        return self._prepare_json_object(
            value,
            field="provenance",
        )

    def _prepare_content(self, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MemoryValidationError(
                "content must be a non-empty string."
            )
        if len(value) > self.max_content_chars:
            raise MemoryValidationError(
                "content exceeds "
                f"{self.max_content_chars} characters."
            )
        return (
            _redact_text(value)
            if self.redact_secrets
            else value
        )

    def _save_memory(
        self,
        *,
        thread_id: str,
        session_id: str,
        memory_kind: str,
        role: str,
        content: str,
        provenance: dict,
        metadata: dict | None,
        ttl_seconds: int | None,
    ) -> dict:
        normalized_thread = _validate_identifier(
            thread_id,
            field="thread_id",
        )
        normalized_session = _validate_identifier(
            session_id,
            field="session_id",
        )
        normalized_role = _validate_identifier(
            role,
            field="role",
            max_chars=64,
        )
        if memory_kind not in MEMORY_KINDS:
            raise MemoryValidationError(
                "Unsupported memory_kind."
            )
        safe_content = self._prepare_content(content)
        provenance_json = self._prepare_provenance(provenance)
        metadata_json = self._prepare_json_object(
            metadata,
            field="metadata",
        )
        effective_ttl = (
            self.default_ttl_seconds
            if ttl_seconds is None
            else self._validate_ttl(
                ttl_seconds,
                allow_none=False,
            )
        )
        now = self._now()
        expires_at = (
            _utc_iso(
                now + timedelta(seconds=effective_ttl)
            )
            if effective_ttl is not None
            else None
        )
        memory_id = str(uuid.uuid4())

        with closing(self._connect()) as connection:
            with self._transaction(connection):
                connection.execute(
                    """
                    DELETE FROM memories
                    WHERE thread_id = ?
                      AND expires_at IS NOT NULL
                      AND expires_at <= ?
                    """,
                    (
                        normalized_thread,
                        _utc_iso(now),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memories(
                        memory_id,
                        thread_id,
                        session_id,
                        memory_kind,
                        role,
                        content,
                        provenance_json,
                        metadata_json,
                        created_at,
                        expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        normalized_thread,
                        normalized_session,
                        memory_kind,
                        normalized_role,
                        safe_content,
                        provenance_json,
                        metadata_json,
                        _utc_iso(now),
                        expires_at,
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM memories
                    WHERE memory_id IN (
                        SELECT memory_id
                        FROM memories
                        WHERE thread_id = ?
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (
                        normalized_thread,
                        self.max_entries_per_thread,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()

        if row is None:
            raise MemoryStorageError(
                "New memory was removed by retention policy."
            )
        return self._row_to_memory(row)

    def save_turn(
        self,
        *,
        thread_id: str,
        session_id: str,
        role: str,
        content: str,
        provenance: dict,
        metadata: dict | None = None,
        ttl_seconds: int | None = None,
    ) -> dict:
        return self._save_memory(
            thread_id=thread_id,
            session_id=session_id,
            memory_kind="turn",
            role=role,
            content=content,
            provenance=provenance,
            metadata=metadata,
            ttl_seconds=ttl_seconds,
        )

    def save_episode(
        self,
        *,
        thread_id: str,
        session_id: str,
        content: str,
        provenance: dict,
        metadata: dict | None = None,
        ttl_seconds: int | None = None,
    ) -> dict:
        return self._save_memory(
            thread_id=thread_id,
            session_id=session_id,
            memory_kind="episode",
            role="memory",
            content=content,
            provenance=provenance,
            metadata=metadata,
            ttl_seconds=ttl_seconds,
        )

    def get_recent_session(
        self,
        *,
        thread_id: str,
        session_id: str,
        limit: int = 20,
    ) -> list[dict]:
        normalized_thread = _validate_identifier(
            thread_id,
            field="thread_id",
        )
        normalized_session = _validate_identifier(
            session_id,
            field="session_id",
        )
        safe_limit = _validate_limit(limit, field="limit")
        now = _utc_iso(self._now())
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT rowid AS memory_sequence, *
                    FROM memories
                    WHERE thread_id = ?
                      AND session_id = ?
                      AND (
                          expires_at IS NULL
                          OR expires_at > ?
                      )
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, memory_sequence ASC
                """,
                (
                    normalized_thread,
                    normalized_session,
                    now,
                    safe_limit,
                ),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def retrieve_relevant_experiences(
        self,
        *,
        thread_id: str,
        query: str,
        current_session_id: str | None = None,
        include_current_session: bool = False,
        limit: int = 5,
        kinds: Iterable[str] | None = None,
        candidate_limit: int = 500,
    ) -> list[dict]:
        normalized_thread = _validate_identifier(
            thread_id,
            field="thread_id",
        )
        if not isinstance(query, str) or not query.strip():
            raise MemoryValidationError(
                "query must be a non-empty string."
            )
        safe_limit = _validate_limit(limit, field="limit")
        safe_candidate_limit = _validate_limit(
            candidate_limit,
            field="candidate_limit",
        )
        normalized_session = None
        if current_session_id is not None:
            normalized_session = _validate_identifier(
                current_session_id,
                field="current_session_id",
            )
        if not isinstance(include_current_session, bool):
            raise MemoryValidationError(
                "include_current_session must be a boolean."
            )
        selected_kinds = tuple(
            sorted(set(kinds or MEMORY_KINDS))
        )
        if (
            not selected_kinds
            or any(
                kind not in MEMORY_KINDS
                for kind in selected_kinds
            )
        ):
            raise MemoryValidationError(
                "kinds contains an unsupported memory kind."
            )

        placeholders = ",".join(
            "?" for _ in selected_kinds
        )
        sql = (
            "SELECT * FROM memories "
            "WHERE thread_id = ? "
            "AND (expires_at IS NULL OR expires_at > ?) "
            f"AND memory_kind IN ({placeholders}) "
        )
        parameters: list[object] = [
            normalized_thread,
            _utc_iso(self._now()),
            *selected_kinds,
        ]
        if (
            normalized_session is not None
            and not include_current_session
        ):
            sql += "AND session_id <> ? "
            parameters.append(normalized_session)
        sql += (
            "ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?"
        )
        parameters.append(safe_candidate_limit)

        with closing(self._connect()) as connection:
            rows = connection.execute(
                sql,
                parameters,
            ).fetchall()

        query_terms = _terms(query)
        if not query_terms:
            return []
        query_count = sum(query_terms.values())
        scored = []
        for row in rows:
            memory_terms = _terms(row["content"])
            overlap = sum(
                min(count, memory_terms.get(term, 0))
                for term, count in query_terms.items()
            )
            if overlap == 0:
                continue
            memory_count = max(
                sum(memory_terms.values()),
                1,
            )
            coverage = overlap / query_count
            density = overlap / memory_count
            score = round(
                (0.75 * coverage) + (0.25 * density),
                6,
            )
            item = self._row_to_memory(row)
            item["relevance_score"] = score
            scored.append(item)

        scored.sort(
            key=lambda item: (
                item["relevance_score"],
                item["created_at"],
                item["memory_id"],
            ),
            reverse=True,
        )
        return scored[:safe_limit]

    def delete_session(
        self,
        *,
        thread_id: str,
        session_id: str,
    ) -> int:
        normalized_thread = _validate_identifier(
            thread_id,
            field="thread_id",
        )
        normalized_session = _validate_identifier(
            session_id,
            field="session_id",
        )
        with closing(self._connect()) as connection:
            with self._transaction(connection):
                cursor = connection.execute(
                    """
                    DELETE FROM memories
                    WHERE thread_id = ? AND session_id = ?
                    """,
                    (
                        normalized_thread,
                        normalized_session,
                    ),
                )
        return cursor.rowcount

    def cleanup_expired(self) -> int:
        now = _utc_iso(self._now())
        with closing(self._connect()) as connection:
            with self._transaction(connection):
                cursor = connection.execute(
                    """
                    DELETE FROM memories
                    WHERE expires_at IS NOT NULL
                      AND expires_at <= ?
                    """,
                    (now,),
                )
        return cursor.rowcount

    def start_run(
        self,
        *,
        thread_id: str,
        session_id: str,
        provenance: dict,
        checkpoint: dict | None = None,
        run_id: str | None = None,
    ) -> dict:
        normalized_thread = _validate_identifier(
            thread_id,
            field="thread_id",
        )
        normalized_session = _validate_identifier(
            session_id,
            field="session_id",
        )
        normalized_run = (
            _validate_identifier(run_id, field="run_id")
            if run_id is not None
            else str(uuid.uuid4())
        )
        checkpoint_json = self._prepare_json_object(
            checkpoint,
            field="checkpoint",
        )
        provenance_json = self._prepare_provenance(provenance)
        now = _utc_iso(self._now())
        try:
            with closing(self._connect()) as connection:
                with self._transaction(connection):
                    connection.execute(
                        """
                        INSERT INTO agent_runs(
                            run_id,
                            thread_id,
                            session_id,
                            status,
                            version,
                            checkpoint_json,
                            provenance_json,
                            created_at,
                            updated_at,
                            completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            normalized_run,
                            normalized_thread,
                            normalized_session,
                            "RUNNING",
                            1,
                            checkpoint_json,
                            provenance_json,
                            now,
                            now,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise RunVersionConflict(
                f"Run {normalized_run!r} already exists."
            ) from exc
        return self.get_run(normalized_run)

    def checkpoint_run(
        self,
        run_id: str,
        checkpoint: dict,
        *,
        expected_version: int,
        status: str = "RUNNING",
    ) -> dict:
        if status not in RUNNING_RUN_STATES:
            raise RunStateError(
                "checkpoint_run only accepts RUNNING or PAUSED."
            )
        return self._advance_run(
            run_id=run_id,
            checkpoint=checkpoint,
            expected_version=expected_version,
            status=status,
            terminal=False,
        )

    def complete_run(
        self,
        run_id: str,
        checkpoint: dict,
        *,
        expected_version: int,
        status: str = "COMPLETED",
    ) -> dict:
        if status not in TERMINAL_RUN_STATES:
            raise RunStateError(
                "complete_run requires a terminal status."
            )
        return self._advance_run(
            run_id=run_id,
            checkpoint=checkpoint,
            expected_version=expected_version,
            status=status,
            terminal=True,
        )

    def _advance_run(
        self,
        *,
        run_id: str,
        checkpoint: dict,
        expected_version: int,
        status: str,
        terminal: bool,
    ) -> dict:
        normalized_run = _validate_identifier(
            run_id,
            field="run_id",
        )
        safe_version = _validate_limit(
            expected_version,
            field="expected_version",
        )
        checkpoint_json = self._prepare_json_object(
            checkpoint,
            field="checkpoint",
        )
        now = _utc_iso(self._now())
        with closing(self._connect()) as connection:
            with self._transaction(connection):
                current = connection.execute(
                    "SELECT status, version FROM agent_runs "
                    "WHERE run_id = ?",
                    (normalized_run,),
                ).fetchone()
                if current is None:
                    raise RunNotFound(normalized_run)
                if current["status"] in TERMINAL_RUN_STATES:
                    raise RunStateError(
                        "A terminal run cannot be changed."
                    )
                if current["version"] != safe_version:
                    raise RunVersionConflict(
                        "Run version mismatch: expected "
                        f"{safe_version}, found "
                        f"{current['version']}."
                    )
                new_version = safe_version + 1
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?,
                        version = ?,
                        checkpoint_json = ?,
                        updated_at = ?,
                        completed_at = ?
                    WHERE run_id = ? AND version = ?
                    """,
                    (
                        status,
                        new_version,
                        checkpoint_json,
                        now,
                        now if terminal else None,
                        normalized_run,
                        safe_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RunVersionConflict(
                        "Concurrent run checkpoint update detected."
                    )
        return self.get_run(normalized_run)

    def get_run(self, run_id: str) -> dict:
        normalized_run = _validate_identifier(
            run_id,
            field="run_id",
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?",
                (normalized_run,),
            ).fetchone()
        if row is None:
            raise RunNotFound(normalized_run)
        return {
            "run_id": row["run_id"],
            "thread_id": row["thread_id"],
            "session_id": row["session_id"],
            "status": row["status"],
            "version": row["version"],
            "checkpoint": _json_load(
                row["checkpoint_json"],
                field="checkpoint",
            ),
            "provenance": _json_load(
                row["provenance_json"],
                field="provenance",
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> dict:
        return {
            "memory_id": row["memory_id"],
            "thread_id": row["thread_id"],
            "session_id": row["session_id"],
            "memory_kind": row["memory_kind"],
            "role": row["role"],
            "content": row["content"],
            "provenance": _json_load(
                row["provenance_json"],
                field="provenance",
            ),
            "metadata": _json_load(
                row["metadata_json"],
                field="metadata",
            ),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
