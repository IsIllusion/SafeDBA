import json

import hashlib
import errno
import math
import os
import time

from contextlib import contextmanager
from pathlib import Path

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

AUDIT_CHAIN_VERSION = 1
_LOCK_TIMEOUT_SECONDS = 5.0


class AuditIntegrityError(RuntimeError):
    pass


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


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _chain_hash(
    record: dict,
    metadata: dict,
) -> str:
    return hashlib.sha256(
        _canonical_json({
            "metadata": metadata,
            "record": record,
        }).encode("utf-8")
    ).hexdigest()


def _raw_line_hash(raw_line: bytes) -> str:
    return hashlib.sha256(raw_line).hexdigest()


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def _exclusive_audit_lock(path: Path):
    """Local OS lock: a slow live writer cannot be evicted by file age.

    Keep the sidecar inode in place. Removing it would let another process
    lock a different inode while a writer still owns the original one.
    Process exit (including a crash) releases the kernel lock.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    with _lock_path(path).open("a+b") as lock_file:
        # Windows byte-range locking needs a stable first byte. Concurrent
        # initializers may append an extra byte; all contenders lock byte 0.
        if os.fstat(lock_file.fileno()).st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        if os.name == "nt":
            import msvcrt

            def acquire():
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise AuditIntegrityError(
                        "Timed out waiting for the audit append lock."
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            release()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in audit record.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number in audit record.")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number in audit record.")
    return number


def _verification_result(
    *,
    valid: bool,
    records: int,
    legacy_records: int,
    chained_records: int,
    head_hash: str | None,
    error_type: str | None = None,
    line: int | None = None,
) -> dict:
    return {
        "valid": valid,
        "records": records,
        "legacy_records": legacy_records,
        "chained_records": chained_records,
        "head_hash": head_hash,
        "error": (
            {"type": error_type, "line": line}
            if error_type is not None
            else None
        ),
    }


def verify_audit_log(
    path: str | Path = AUDIT_LOG_PATH,
) -> dict:
    """Verify JSON syntax, sequence continuity, and every chained digest."""

    audit_path = Path(path)
    if not audit_path.parent.exists():
        return _verify_audit_log_unlocked(audit_path)
    # Readers use the same lock so a partially flushed append is not
    # incorrectly reported as corruption.
    with _exclusive_audit_lock(audit_path):
        return _verify_audit_log_unlocked(audit_path)


def _verify_audit_log_unlocked(audit_path: Path) -> dict:

    if not audit_path.exists():
        return _verification_result(
            valid=True,
            records=0,
            legacy_records=0,
            chained_records=0,
            head_hash=None,
        )

    records = 0
    legacy_records = 0
    chained_records = 0
    previous_hash = None
    previous_raw_line = None
    chain_started = False

    with audit_path.open("rb") as stream:
        for line_number, physical_line in enumerate(stream, start=1):
            if not physical_line.endswith(b"\n"):
                return _verification_result(
                    valid=False, records=records, legacy_records=legacy_records,
                    chained_records=chained_records, head_hash=previous_hash,
                    error_type="INCOMPLETE_AUDIT_LINE", line=line_number,
                )
            raw_line = physical_line.rstrip(b"\r\n")
            if not raw_line:
                return _verification_result(
                    valid=False,
                    records=records,
                    legacy_records=legacy_records,
                    chained_records=chained_records,
                    head_hash=previous_hash,
                    error_type="BLANK_AUDIT_LINE",
                    line=line_number,
                )
            try:
                decoded = json.loads(
                    raw_line.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                    parse_float=_finite_float,
                )
            except (UnicodeDecodeError, ValueError):
                return _verification_result(
                    valid=False,
                    records=records,
                    legacy_records=legacy_records,
                    chained_records=chained_records,
                    head_hash=previous_hash,
                    error_type="MALFORMED_AUDIT_RECORD",
                    line=line_number,
                )
            records += 1
            if not isinstance(decoded, dict):
                return _verification_result(
                    valid=False,
                    records=records,
                    legacy_records=legacy_records,
                    chained_records=chained_records,
                    head_hash=previous_hash,
                    error_type="AUDIT_RECORD_NOT_OBJECT",
                    line=line_number,
                )

            envelope = decoded.get("_audit")
            if "_audit" in decoded and (
                not isinstance(envelope, dict)
                or set(envelope) != {
                    "schema_version", "sequence", "previous_hash",
                    "anchor", "record_hash",
                }
                or type(envelope.get("schema_version")) is not int
                or type(envelope.get("sequence")) is not int
            ):
                return _verification_result(
                    valid=False, records=records, legacy_records=legacy_records,
                    chained_records=chained_records, head_hash=previous_hash,
                    error_type="MALFORMED_AUDIT_ENVELOPE", line=line_number,
                )
            if not isinstance(envelope, dict):
                if chain_started:
                    return _verification_result(
                        valid=False,
                        records=records,
                        legacy_records=legacy_records,
                        chained_records=chained_records,
                        head_hash=previous_hash,
                        error_type="UNCHAINED_RECORD_AFTER_CHAIN",
                        line=line_number,
                    )
                legacy_records += 1
                previous_raw_line = raw_line
                continue

            chain_started = True
            sequence = envelope.get("sequence")
            expected_sequence = chained_records + 1
            anchor = envelope.get("anchor")
            expected_previous = previous_hash
            if chained_records == 0:
                if previous_raw_line is None:
                    expected_previous = None
                    expected_anchor = "genesis"
                else:
                    expected_previous = _raw_line_hash(previous_raw_line)
                    expected_anchor = "legacy_tail_sha256"
            else:
                expected_anchor = None

            metadata = {
                "schema_version": envelope.get("schema_version"),
                "sequence": sequence,
                "previous_hash": envelope.get("previous_hash"),
                "anchor": anchor,
            }
            record = {
                key: value
                for key, value in decoded.items()
                if key != "_audit"
            }
            expected_hash = _chain_hash(record, metadata)
            error_type = None
            if envelope.get("schema_version") != AUDIT_CHAIN_VERSION:
                error_type = "UNSUPPORTED_AUDIT_CHAIN_VERSION"
            elif sequence != expected_sequence:
                error_type = "AUDIT_SEQUENCE_MISMATCH"
            elif envelope.get("previous_hash") != expected_previous:
                error_type = "AUDIT_PREVIOUS_HASH_MISMATCH"
            elif anchor != expected_anchor:
                error_type = "AUDIT_ANCHOR_MISMATCH"
            elif envelope.get("record_hash") != expected_hash:
                error_type = "AUDIT_RECORD_HASH_MISMATCH"
            if error_type is not None:
                return _verification_result(
                    valid=False,
                    records=records,
                    legacy_records=legacy_records,
                    chained_records=chained_records,
                    head_hash=previous_hash,
                    error_type=error_type,
                    line=line_number,
                )

            chained_records += 1
            previous_hash = expected_hash

    return _verification_result(
        valid=True,
        records=records,
        legacy_records=legacy_records,
        chained_records=chained_records,
        head_hash=previous_hash,
    )


def write_audit_log(record: dict) -> None:
    AUDIT_LOG_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    sanitized = sanitize_audit_value(record)
    if not isinstance(sanitized, dict):
        raise TypeError("Audit records must be objects.")
    sanitized.pop("_audit", None)

    with _exclusive_audit_lock(AUDIT_LOG_PATH):
        verification = _verify_audit_log_unlocked(AUDIT_LOG_PATH)
        if not verification["valid"]:
            error = verification["error"] or {}
            raise AuditIntegrityError(
                "Refusing to append to an invalid audit chain: "
                f"{error.get('type', 'UNKNOWN')}"
            )

        sequence = verification["chained_records"] + 1
        previous_hash = verification["head_hash"]
        if previous_hash is None and verification["legacy_records"]:
            with AUDIT_LOG_PATH.open("rb") as existing:
                legacy_tail = None
                for physical_line in existing:
                    raw_line = physical_line.rstrip(b"\r\n")
                    if raw_line:
                        legacy_tail = raw_line
            if legacy_tail is None:
                raise AuditIntegrityError(
                    "Legacy audit records could not be anchored."
                )
            previous_hash = _raw_line_hash(legacy_tail)
            anchor = "legacy_tail_sha256"
        else:
            anchor = "genesis" if sequence == 1 else None

        metadata = {
            "schema_version": AUDIT_CHAIN_VERSION,
            "sequence": sequence,
            "previous_hash": previous_hash,
            "anchor": anchor,
        }
        envelope = {
            **metadata,
            "record_hash": _chain_hash(sanitized, metadata),
        }
        serialized = json.dumps(
            {"_audit": envelope, **sanitized},
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
