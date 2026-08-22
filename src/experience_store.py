"""Controlled, offline experience collection and candidate gating.

This module deliberately does not train models, edit prompts, or change policy.
It stores sanitized run summaries and explicit human feedback, exports reviewed
candidate datasets, and records deterministic promotion-gate decisions.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


SCHEMA_VERSION = 1
DATASET_SCHEMA_VERSION = 1

_ALLOWED_DATASET_PURPOSES = {
    "training",
    "evaluation",
}
_ALLOWED_METRIC_DIRECTIONS = {
    "higher",
    "lower",
}
_SECRET_KEY_PARTS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
}
_FINGERPRINT_KEYS = {
    "conversation",
    "dsn",
    "messages",
    "original_query",
    "prompt",
    "query",
    "request_headers",
    "rewritten_query",
    "system_prompt",
    "tool_arguments",
    "tool_output",
    "tool_result",
}
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
_IPV4_RE = re.compile(
    r"(?<!\d)(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?!\d)"
)
_BEARER_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    flags=re.IGNORECASE,
)
_INLINE_SECRET_RE = re.compile(
    r"\b(api[_-]?key|authorization|password|secret|token)"
    r"\s*[:=]\s*[^\s,;]+",
    flags=re.IGNORECASE,
)
_CREDENTIAL_URI_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://[^:/\s]+:)"
    r"[^@\s/]+(@)",
)
_VERSION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)


class ExperienceStoreError(RuntimeError):
    pass


class ExperienceValidationError(ExperienceStoreError):
    pass


class RunNotFound(ExperienceStoreError):
    pass


class DatasetVersionExists(ExperienceStoreError):
    pass


class DatasetNotFound(ExperienceStoreError):
    pass


@dataclass(frozen=True)
class DatasetExportCriteria:
    """Explicit, reviewable filters for a candidate dataset export."""

    purpose: str
    allowed_labels: tuple[str, ...]
    allowed_outcomes: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    required_tags: tuple[str, ...] = ()
    min_rating: int | None = None

    def validate(self) -> None:
        if self.purpose not in _ALLOWED_DATASET_PURPOSES:
            raise ExperienceValidationError(
                "Dataset purpose must be 'training' or 'evaluation'."
            )
        if not self.allowed_labels:
            raise ExperienceValidationError(
                "At least one allowed human label is required."
            )
        _validate_string_collection(
            self.allowed_labels,
            field="allowed_labels",
        )
        _validate_string_collection(
            self.allowed_outcomes,
            field="allowed_outcomes",
        )
        _validate_string_collection(
            self.task_types,
            field="task_types",
        )
        _validate_string_collection(
            self.required_tags,
            field="required_tags",
        )
        if self.min_rating is not None and not (
            isinstance(self.min_rating, int)
            and not isinstance(self.min_rating, bool)
            and 1 <= self.min_rating <= 5
        ):
            raise ExperienceValidationError(
                "min_rating must be an integer from 1 through 5."
            )

    def as_dict(self) -> dict:
        return {
            "purpose": self.purpose,
            "allowed_labels": sorted(set(self.allowed_labels)),
            "allowed_outcomes": sorted(set(self.allowed_outcomes)),
            "task_types": sorted(set(self.task_types)),
            "required_tags": sorted(set(self.required_tags)),
            "min_rating": self.min_rating,
            "requires_explicit_human_use_approval": True,
            "feedback_selection": "latest_feedback_per_run",
        }


@dataclass(frozen=True)
class PromotionApproval:
    """An explicit human decision; omission always fails the gate."""

    actor: str
    decision: str
    rationale: str
    approved_at: str | None = None


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ExperienceValidationError(
            "Experience timestamps must be timezone-aware."
        )
    return current.astimezone(timezone.utc).isoformat()


def _parse_utc_timestamp(value: str, *, field: str) -> str:
    _require_text(value, field=field)
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ExperienceValidationError(
            f"{field} must be an ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None:
        raise ExperienceValidationError(
            f"{field} must include a timezone."
        )
    return parsed.astimezone(timezone.utc).isoformat()


def _require_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperienceValidationError(
            f"{field} must be a non-empty string."
        )
    if len(value) > 512:
        raise ExperienceValidationError(
            f"{field} exceeds the 512 character limit."
        )
    return value.strip()


def _validate_string_collection(
    values: Iterable[str],
    *,
    field: str,
) -> tuple[str, ...]:
    normalized = []
    for value in values:
        normalized.append(
            _require_text(value, field=field)
        )
    return tuple(sorted(set(normalized)))


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExperienceValidationError(
            "Experience values must be strict JSON data."
        ) from exc


def _fingerprint(value: object) -> dict:
    serialized = _canonical_json(value)
    return {
        "redacted": True,
        "sha256": hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest(),
        "characters": len(serialized),
    }


def sanitize_experience_value(
    value: object,
    *,
    key: str | None = None,
) -> object:
    """Remove common secrets/identifiers and fingerprint raw Agent content."""

    normalized_key = key.lower() if isinstance(key, str) else None
    if normalized_key and any(
        part in normalized_key
        for part in _SECRET_KEY_PARTS
    ):
        return "[REDACTED]"

    if normalized_key in _FINGERPRINT_KEYS:
        return _fingerprint(value)

    if isinstance(value, dict):
        return {
            str(child_key): sanitize_experience_value(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            sanitize_experience_value(item)
            for item in value
        ]

    if isinstance(value, str):
        sanitized = _BEARER_RE.sub(
            "Bearer [REDACTED]",
            value,
        )
        sanitized = _INLINE_SECRET_RE.sub(
            lambda match: (
                f"{match.group(1)}=[REDACTED]"
            ),
            sanitized,
        )
        sanitized = _CREDENTIAL_URI_RE.sub(
            r"\1[REDACTED]\2",
            sanitized,
        )
        sanitized = _EMAIL_RE.sub(
            "[EMAIL_REDACTED]",
            sanitized,
        )
        return _IPV4_RE.sub(
            "[IP_REDACTED]",
            sanitized,
        )

    if value is None or isinstance(value, (bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExperienceValidationError(
                "Non-finite numbers cannot be stored."
            )
        return value

    raise ExperienceValidationError(
        "Experience values must contain only JSON-compatible data."
    )


def _validated_metrics(metrics: dict) -> dict[str, float]:
    if not isinstance(metrics, dict):
        raise ExperienceValidationError(
            "metrics must be a JSON object."
        )
    normalized = {}
    for name, value in metrics.items():
        metric_name = _require_text(
            name,
            field="metric name",
        )
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise ExperienceValidationError(
                f"Metric {metric_name!r} must be finite and numeric."
            )
        normalized[metric_name] = float(value)
    return normalized


class SQLiteExperienceStore:
    """SQLite-backed, offline-only store for reviewed Agent experience."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.clock = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experience_runs (
                    run_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS human_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    label TEXT NOT NULL,
                    rationale_json TEXT NOT NULL,
                    rating INTEGER,
                    approved_uses_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id)
                        REFERENCES experience_runs(run_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_run_created
                ON human_feedback(run_id, created_at);

                CREATE TABLE IF NOT EXISTS dataset_exports (
                    dataset_version TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    data_path TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promotion_audit (
                    audit_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    dataset_version TEXT NOT NULL,
                    promoted INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    gate_input_json TEXT NOT NULL,
                    approval_json TEXT,
                    checked_at TEXT NOT NULL,
                    FOREIGN KEY (dataset_version)
                        REFERENCES dataset_exports(dataset_version)
                        ON DELETE RESTRICT
                );
                """
            )
            connection.commit()

    def record_run_summary(
        self,
        *,
        run_id: str,
        task_type: str,
        outcome: str,
        summary: dict,
        metrics: dict | None = None,
        tags: Iterable[str] = (),
        created_at: datetime | None = None,
    ) -> dict:
        run_id = _require_text(run_id, field="run_id")
        task_type = _require_text(task_type, field="task_type")
        outcome = _require_text(outcome, field="outcome")
        if not isinstance(summary, dict):
            raise ExperienceValidationError(
                "summary must be a JSON object, not a raw transcript."
            )

        sanitized_summary = sanitize_experience_value(summary)
        if len(_canonical_json(sanitized_summary)) > 65_536:
            raise ExperienceValidationError(
                "Sanitized run summary exceeds 65,536 characters."
            )
        normalized_metrics = _validated_metrics(metrics or {})
        normalized_tags = _validate_string_collection(
            tags,
            field="tag",
        )
        timestamp = _utc_iso(created_at or self.clock())

        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT INTO experience_runs (
                        run_id, task_type, outcome, summary_json,
                        metrics_json, tags_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_type,
                        outcome,
                        _canonical_json(sanitized_summary),
                        _canonical_json(normalized_metrics),
                        _canonical_json(normalized_tags),
                        timestamp,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ExperienceValidationError(
                f"Run {run_id!r} already exists."
            ) from exc

        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        run_id = _require_text(run_id, field="run_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT run_id, task_type, outcome, summary_json,
                       metrics_json, tags_json, created_at
                FROM experience_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise RunNotFound(f"Run {run_id!r} was not found.")
        return {
            "run_id": row["run_id"],
            "task_type": row["task_type"],
            "outcome": row["outcome"],
            "summary": json.loads(row["summary_json"]),
            "metrics": json.loads(row["metrics_json"]),
            "tags": json.loads(row["tags_json"]),
            "created_at": row["created_at"],
        }

    def add_human_feedback(
        self,
        *,
        feedback_id: str,
        run_id: str,
        actor: str,
        label: str,
        rationale: dict,
        rating: int | None = None,
        approved_uses: Iterable[str] = (),
        created_at: datetime | None = None,
    ) -> dict:
        feedback_id = _require_text(
            feedback_id,
            field="feedback_id",
        )
        run_id = _require_text(run_id, field="run_id")
        actor = str(sanitize_experience_value(
            _require_text(actor, field="actor")
        ))
        label = _require_text(label, field="label")
        if not isinstance(rationale, dict):
            raise ExperienceValidationError(
                "rationale must be a JSON object."
            )
        if rating is not None and not (
            isinstance(rating, int)
            and not isinstance(rating, bool)
            and 1 <= rating <= 5
        ):
            raise ExperienceValidationError(
                "rating must be an integer from 1 through 5."
            )

        uses = _validate_string_collection(
            approved_uses,
            field="approved use",
        )
        if not set(uses).issubset(
            _ALLOWED_DATASET_PURPOSES
        ):
            raise ExperienceValidationError(
                "approved_uses may contain only 'training' and 'evaluation'."
            )
        sanitized_rationale = sanitize_experience_value(rationale)
        if len(_canonical_json(sanitized_rationale)) > 16_384:
            raise ExperienceValidationError(
                "Sanitized feedback rationale exceeds 16,384 characters."
            )
        timestamp = _utc_iso(created_at or self.clock())

        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT INTO human_feedback (
                        feedback_id, run_id, actor, label,
                        rationale_json, rating, approved_uses_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        run_id,
                        actor,
                        label,
                        _canonical_json(sanitized_rationale),
                        rating,
                        _canonical_json(uses),
                        timestamp,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            if not self._run_exists(run_id):
                raise RunNotFound(
                    f"Run {run_id!r} was not found."
                ) from exc
            raise ExperienceValidationError(
                f"Feedback {feedback_id!r} already exists."
            ) from exc

        return {
            "feedback_id": feedback_id,
            "run_id": run_id,
            "actor": actor,
            "label": label,
            "rationale": sanitized_rationale,
            "rating": rating,
            "approved_uses": list(uses),
            "created_at": timestamp,
        }

    def _run_exists(self, run_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM experience_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return row is not None

    def export_candidate_dataset(
        self,
        *,
        output_directory: str | Path,
        dataset_version: str,
        criteria: DatasetExportCriteria,
    ) -> dict:
        dataset_version = _require_text(
            dataset_version,
            field="dataset_version",
        )
        if not _VERSION_RE.fullmatch(dataset_version):
            raise ExperienceValidationError(
                "dataset_version contains unsupported characters."
            )
        if not isinstance(criteria, DatasetExportCriteria):
            raise ExperienceValidationError(
                "criteria must be DatasetExportCriteria."
            )
        criteria.validate()
        if self._dataset_exists(dataset_version):
            raise DatasetVersionExists(
                f"Dataset version {dataset_version!r} already exists."
            )

        output_path = Path(output_directory).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        data_path = output_path / f"{dataset_version}.jsonl"
        manifest_path = output_path / f"{dataset_version}.manifest.json"
        if data_path.exists() or manifest_path.exists():
            raise DatasetVersionExists(
                "Dataset output files already exist; exports are immutable."
            )

        records = self._select_export_records(criteria)
        payload = "".join(
            _canonical_json(record) + "\n"
            for record in records
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        timestamp = _utc_iso(self.clock())
        manifest = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "dataset_version": dataset_version,
            "purpose": criteria.purpose,
            "created_at": timestamp,
            "data_file": data_path.name,
            "sha256": digest,
            "record_count": len(records),
            "filter_criteria": criteria.as_dict(),
        }

        temporary_data = data_path.with_suffix(
            data_path.suffix + ".tmp"
        )
        temporary_manifest = manifest_path.with_suffix(
            manifest_path.suffix + ".tmp"
        )
        try:
            temporary_data.write_bytes(payload)
            temporary_manifest.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                ) + "\n",
                encoding="utf-8",
            )
            temporary_data.replace(data_path)
            temporary_manifest.replace(manifest_path)
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT INTO dataset_exports (
                        dataset_version, purpose, data_path,
                        manifest_path, manifest_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dataset_version,
                        criteria.purpose,
                        str(data_path),
                        str(manifest_path),
                        _canonical_json(manifest),
                        timestamp,
                    ),
                )
                connection.commit()
        except Exception:
            for path in (
                temporary_data,
                temporary_manifest,
                data_path,
                manifest_path,
            ):
                if path.exists():
                    path.unlink()
            raise

        return manifest

    def _select_export_records(
        self,
        criteria: DatasetExportCriteria,
    ) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.run_id, r.task_type, r.outcome,
                       r.summary_json, r.metrics_json,
                       r.tags_json, r.created_at AS run_created_at,
                       f.feedback_id, f.label, f.rationale_json,
                       f.rating, f.approved_uses_json,
                       f.created_at AS feedback_created_at,
                       f.rowid AS feedback_sequence
                FROM experience_runs AS r
                JOIN human_feedback AS f ON f.run_id = r.run_id
                ORDER BY r.run_id ASC,
                         f.created_at DESC,
                         f.rowid DESC
                """
            ).fetchall()

        latest_by_run = {}
        for row in rows:
            latest_by_run.setdefault(row["run_id"], row)

        filters = criteria.as_dict()
        allowed_labels = set(filters["allowed_labels"])
        allowed_outcomes = set(filters["allowed_outcomes"])
        task_types = set(filters["task_types"])
        required_tags = set(filters["required_tags"])
        records = []
        for run_id in sorted(latest_by_run):
            row = latest_by_run[run_id]
            approved_uses = set(
                json.loads(row["approved_uses_json"])
            )
            tags = set(json.loads(row["tags_json"]))
            if criteria.purpose not in approved_uses:
                continue
            if row["label"] not in allowed_labels:
                continue
            if allowed_outcomes and row["outcome"] not in allowed_outcomes:
                continue
            if task_types and row["task_type"] not in task_types:
                continue
            if not required_tags.issubset(tags):
                continue
            if (
                criteria.min_rating is not None
                and (
                    row["rating"] is None
                    or row["rating"] < criteria.min_rating
                )
            ):
                continue

            records.append({
                "schema_version": DATASET_SCHEMA_VERSION,
                "run_id": row["run_id"],
                "task_type": row["task_type"],
                "outcome": row["outcome"],
                "summary": json.loads(row["summary_json"]),
                "metrics": json.loads(row["metrics_json"]),
                "tags": sorted(tags),
                "run_created_at": row["run_created_at"],
                "human_feedback": {
                    "feedback_id": row["feedback_id"],
                    "label": row["label"],
                    "rationale": json.loads(row["rationale_json"]),
                    "rating": row["rating"],
                    "created_at": row["feedback_created_at"],
                },
            })
        return records

    def _dataset_exists(self, dataset_version: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM dataset_exports WHERE dataset_version = ?",
                (dataset_version,),
            ).fetchone()
        return row is not None

    def _load_dataset_manifest(
        self,
        dataset_version: str,
    ) -> dict:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT manifest_json
                FROM dataset_exports
                WHERE dataset_version = ?
                """,
                (dataset_version,),
            ).fetchone()
        if row is None:
            raise DatasetNotFound(
                f"Dataset {dataset_version!r} was not found."
            )
        return json.loads(row["manifest_json"])

    def assess_candidate_promotion(
        self,
        *,
        candidate_id: str,
        dataset_version: str,
        baseline_metrics: dict,
        candidate_metrics: dict,
        metric_directions: dict[str, str],
        safety_results: dict[str, bool],
        approval: PromotionApproval | None,
    ) -> dict:
        """Evaluate and audit a gate; never applies the candidate itself."""

        candidate_id = _require_text(
            candidate_id,
            field="candidate_id",
        )
        dataset_version = _require_text(
            dataset_version,
            field="dataset_version",
        )
        dataset_manifest = self._load_dataset_manifest(
            dataset_version
        )

        baseline = _validated_metrics(baseline_metrics)
        candidate = _validated_metrics(candidate_metrics)
        if not baseline:
            raise ExperienceValidationError(
                "At least one benchmark metric is required."
            )
        if not isinstance(metric_directions, dict):
            raise ExperienceValidationError(
                "metric_directions must be a JSON object."
            )
        if not isinstance(safety_results, dict) or not safety_results:
            raise ExperienceValidationError(
                "At least one safety result is required."
            )

        reasons = []
        if dataset_manifest["record_count"] <= 0:
            reasons.append("dataset_empty")
        for metric_name, baseline_value in sorted(baseline.items()):
            direction = metric_directions.get(metric_name)
            if direction not in _ALLOWED_METRIC_DIRECTIONS:
                reasons.append(
                    f"metric_direction_missing_or_invalid:{metric_name}"
                )
                continue
            if metric_name not in candidate:
                reasons.append(
                    f"candidate_metric_missing:{metric_name}"
                )
                continue
            candidate_value = candidate[metric_name]
            if (
                direction == "higher"
                and candidate_value < baseline_value
            ):
                reasons.append(
                    f"benchmark_regression:{metric_name}"
                )
            if (
                direction == "lower"
                and candidate_value > baseline_value
            ):
                reasons.append(
                    f"benchmark_regression:{metric_name}"
                )

        normalized_safety = {}
        for name, passed in safety_results.items():
            check_name = _require_text(
                name,
                field="safety check name",
            )
            if not isinstance(passed, bool):
                raise ExperienceValidationError(
                    f"Safety result {check_name!r} must be boolean."
                )
            normalized_safety[check_name] = passed
            if not passed:
                reasons.append(
                    f"safety_check_failed:{check_name}"
                )

        approval_record = None
        if approval is None:
            reasons.append("explicit_approval_missing")
        elif not isinstance(approval, PromotionApproval):
            raise ExperienceValidationError(
                "approval must be PromotionApproval."
            )
        else:
            actor = str(sanitize_experience_value(
                _require_text(
                    approval.actor,
                    field="approval actor",
                )
            ))
            rationale = _require_text(
                approval.rationale,
                field="approval rationale",
            )
            decision = _require_text(
                approval.decision,
                field="approval decision",
            ).upper()
            approved_at = (
                _parse_utc_timestamp(
                    approval.approved_at,
                    field="approved_at",
                )
                if approval.approved_at is not None
                else _utc_iso(self.clock())
            )
            if decision != "APPROVE":
                reasons.append("explicit_approval_not_granted")
            approval_record = {
                "actor": actor,
                "decision": decision,
                "rationale": sanitize_experience_value(rationale),
                "approved_at": approved_at,
            }

        promoted = not reasons
        checked_at = _utc_iso(self.clock())
        audit_id = str(uuid.uuid4())
        gate_input = {
            "baseline_metrics": baseline,
            "candidate_metrics": candidate,
            "metric_directions": metric_directions,
            "safety_results": normalized_safety,
        }
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO promotion_audit (
                    audit_id, candidate_id, dataset_version,
                    promoted, reasons_json, gate_input_json,
                    approval_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    candidate_id,
                    dataset_version,
                    int(promoted),
                    _canonical_json(reasons),
                    _canonical_json(gate_input),
                    (
                        _canonical_json(approval_record)
                        if approval_record is not None
                        else None
                    ),
                    checked_at,
                ),
            )
            connection.commit()

        return {
            "audit_id": audit_id,
            "candidate_id": candidate_id,
            "dataset_version": dataset_version,
            "promoted": promoted,
            "reasons": reasons,
            "checked_at": checked_at,
            "applied_automatically": False,
        }

    def list_promotion_audit(
        self,
        *,
        candidate_id: str | None = None,
    ) -> list[dict]:
        arguments: tuple = ()
        where = ""
        if candidate_id is not None:
            candidate_id = _require_text(
                candidate_id,
                field="candidate_id",
            )
            where = "WHERE candidate_id = ?"
            arguments = (candidate_id,)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT audit_id, candidate_id, dataset_version,
                       promoted, reasons_json, gate_input_json,
                       approval_json, checked_at
                FROM promotion_audit
                {where}
                ORDER BY checked_at ASC, audit_id ASC
                """,
                arguments,
            ).fetchall()
        return [
            {
                "audit_id": row["audit_id"],
                "candidate_id": row["candidate_id"],
                "dataset_version": row["dataset_version"],
                "promoted": bool(row["promoted"]),
                "reasons": json.loads(row["reasons_json"]),
                "gate_input": json.loads(row["gate_input_json"]),
                "approval": (
                    json.loads(row["approval_json"])
                    if row["approval_json"] is not None
                    else None
                ),
                "checked_at": row["checked_at"],
            }
            for row in rows
        ]
