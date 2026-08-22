"""Deterministic SQL policy for diagnostic query execution.

This module is intentionally dependency free so the policy can be unit
tested without a database.  It is a defence-in-depth layer: production
deployments must still use a least-privilege observer role and PostgreSQL
read-only transactions.
"""

from __future__ import annotations

import re


class QuerySafetyError(ValueError):
    """Raised when SQL falls outside SafeDBA's diagnostic policy."""


_DOLLAR_QUOTE_START = re.compile(
    r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$"
)


# These built-ins either change server/session state, signal another
# backend, deliberately wait, touch server files, advance sequences, or
# can execute work outside the local read-only query.  This cannot cover
# arbitrary extension/UDF side effects; database privileges remain the
# primary boundary.
BLOCKED_FUNCTIONS = frozenset({
    "dblink",
    "dblink_connect",
    "dblink_disconnect",
    "dblink_exec",
    "lo_create",
    "lo_export",
    "lo_import",
    "lo_unlink",
    "nextval",
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_advisory_xact_lock",
    "pg_advisory_xact_lock_shared",
    "pg_backup_start",
    "pg_backup_stop",
    "pg_cancel_backend",
    "pg_create_logical_replication_slot",
    "pg_create_physical_replication_slot",
    "pg_create_restore_point",
    "pg_drop_replication_slot",
    "pg_log_backend_memory_contexts",
    "pg_notify",
    "pg_promote",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_reload_conf",
    "pg_replication_origin_advance",
    "pg_replication_origin_create",
    "pg_replication_origin_drop",
    "pg_replication_origin_session_reset",
    "pg_replication_origin_session_setup",
    "pg_rotate_logfile",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_start_backup",
    "pg_stat_file",
    "pg_stop_backup",
    "pg_switch_wal",
    "pg_terminate_backend",
    "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared",
    "pg_try_advisory_xact_lock",
    "pg_try_advisory_xact_lock_shared",
    "pg_wal_replay_pause",
    "pg_wal_replay_resume",
    "set_config",
    "setval",
})


_LOCKING_CLAUSE = re.compile(
    r"\bFOR\s+(?:(?:NO\s+)?KEY\s+)?(?:UPDATE|SHARE)\b",
    re.IGNORECASE,
)


def _mask_non_code(source: str) -> str:
    """Return SQL with literals/comments masked but code preserved.

    The output has the same length as the input.  Quoted identifiers are
    normalized into identifier-like text so a quoted dangerous function
    cannot bypass the deny list, while punctuation inside an identifier
    cannot be mistaken for a statement separator.
    """

    output = list(source)
    length = len(source)
    index = 0

    while index < length:
        char = source[index]

        if char == "'":
            start = index
            escape_string = (
                index > 0
                and source[index - 1] in {"E", "e"}
                and (
                    index == 1
                    or not (
                        source[index - 2].isalnum()
                        or source[index - 2] in {"_", "$"}
                    )
                )
            )
            index += 1

            while index < length:
                if escape_string and source[index] == "\\":
                    # Backslash escaping applies to E'...' strings.  All
                    # SafeDBA connections force standard_conforming_strings
                    # on, so an ordinary string never receives this rule.
                    index = min(index + 2, length)
                    continue

                if source[index] == "'":
                    if (
                        index + 1 < length
                        and source[index + 1] == "'"
                    ):
                        index += 2
                        continue

                    index += 1
                    break

                index += 1
            else:
                raise QuerySafetyError(
                    "Unterminated SQL string literal."
                )

            for position in range(start, index):
                output[position] = " "
            continue

        if char == '"':
            start = index
            index += 1
            decoded: list[str] = []

            while index < length:
                if source[index] == '"':
                    if (
                        index + 1 < length
                        and source[index + 1] == '"'
                    ):
                        decoded.append('"')
                        index += 2
                        continue

                    index += 1
                    break

                decoded.append(source[index])
                index += 1
            else:
                raise QuerySafetyError(
                    "Unterminated quoted SQL identifier."
                )

            for position in range(start, index):
                output[position] = " "

            normalized_identifier = "".join(
                character
                if (
                    character.isalnum()
                    or character in {"_", "$"}
                )
                else "_"
                for character in decoded
            )

            for offset, character in enumerate(
                normalized_identifier[: index - start]
            ):
                output[start + offset] = character
            continue

        if source.startswith("--", index):
            start = index
            newline = source.find("\n", index + 2)
            index = length if newline == -1 else newline

            for position in range(start, index):
                output[position] = " "
            continue

        if source.startswith("/*", index):
            start = index
            index += 2
            depth = 1

            while index < length and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1

            if depth:
                raise QuerySafetyError(
                    "Unterminated SQL block comment."
                )

            for position in range(start, index):
                if output[position] not in {"\r", "\n"}:
                    output[position] = " "
            continue

        if char == "$":
            match = _DOLLAR_QUOTE_START.match(
                source,
                index,
            )

            if match:
                delimiter = match.group(0)
                start = index
                closing = source.find(
                    delimiter,
                    match.end(),
                )

                if closing == -1:
                    raise QuerySafetyError(
                        "Unterminated dollar-quoted SQL string."
                    )

                index = closing + len(delimiter)
                for position in range(start, index):
                    if output[position] not in {"\r", "\n"}:
                        output[position] = " "
                continue

        index += 1

    return "".join(output)


def ensure_read_only_query(
    query: str,
    *,
    max_length: int = 50_000,
) -> str:
    """Validate and normalize one diagnostic SELECT statement.

    Only a top-level ``SELECT`` is accepted.  ``WITH`` is deliberately
    excluded because PostgreSQL permits data-modifying CTEs.  A single
    trailing semicolon is removed; semicolons inside literals/comments
    do not count as statement separators.
    """

    if not isinstance(query, str):
        raise QuerySafetyError(
            "Diagnostic SQL must be a string."
        )

    normalized = query.strip()

    if not normalized:
        raise QuerySafetyError(
            "Diagnostic SQL must not be empty."
        )

    if len(normalized) > max_length:
        raise QuerySafetyError(
            "Diagnostic SQL exceeds the configured length limit."
        )

    if re.search(
        r"(?<![A-Za-z0-9_$])U\s*&\s*[\"']",
        normalized,
        re.IGNORECASE,
    ):
        raise QuerySafetyError(
            "PostgreSQL Unicode-escape strings and identifiers are not "
            "allowed in diagnostic SQL."
        )

    masked = _mask_non_code(normalized)
    semicolons = [
        position
        for position, character in enumerate(masked)
        if character == ";"
    ]

    if len(semicolons) > 1:
        raise QuerySafetyError(
            "Multiple SQL statements are not allowed."
        )

    if semicolons:
        separator = semicolons[0]

        if masked[separator + 1 :].strip():
            raise QuerySafetyError(
                "Multiple SQL statements are not allowed."
            )

        normalized = normalized[:separator].rstrip()
        masked = masked[:separator]

    if not re.match(
        r"^\s*SELECT\b",
        masked,
        re.IGNORECASE,
    ):
        raise QuerySafetyError(
            "SafeDBA diagnostic tools allow only SELECT statements."
        )

    if re.search(
        r"\bINTO\b",
        masked,
        re.IGNORECASE,
    ):
        raise QuerySafetyError(
            "SELECT INTO is not allowed in diagnostic queries."
        )

    if _LOCKING_CLAUSE.search(masked):
        raise QuerySafetyError(
            "Row-locking SELECT clauses are not allowed."
        )

    for function_name in sorted(BLOCKED_FUNCTIONS):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_$])"
            rf"(?:[A-Za-z_][A-Za-z0-9_$]*\s*\.\s*)?"
            rf"{re.escape(function_name)}\s*\(",
            re.IGNORECASE,
        )

        if pattern.search(masked):
            raise QuerySafetyError(
                "Function "
                f"'{function_name}' is not allowed in diagnostic SQL."
            )

    return normalized
