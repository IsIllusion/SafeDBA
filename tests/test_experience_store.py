import hashlib
import json
import sys
import tempfile
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


from experience_store import (  # noqa: E402
    DatasetExportCriteria,
    DatasetNotFound,
    DatasetVersionExists,
    ExperienceValidationError,
    PromotionApproval,
    RunNotFound,
    SQLiteExperienceStore,
)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class ExperienceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = MutableClock(datetime(
            2026,
            8,
            22,
            4,
            0,
            tzinfo=timezone.utc,
        ))
        self.store = SQLiteExperienceStore(
            self.root / "experience.sqlite3",
            clock=self.clock,
        )

    def record_run(
        self,
        run_id="run-1",
        *,
        outcome="RESOLVED",
        tags=("lock", "postgresql"),
    ):
        return self.store.record_run_summary(
            run_id=run_id,
            task_type="LOCK_CONTENTION",
            outcome=outcome,
            summary={
                "finding": (
                    "Blocker owned by user@example.com at 10.2.3.4 "
                    "with token=also-secret and "
                    "postgresql://agent:db-password@db/benchmark"
                ),
                "query": "SELECT * FROM customer_private_data",
                "password": "do-not-persist",
                "tool_output": {
                    "rows": [{"email": "victim@example.com"}],
                },
            },
            metrics={"tool_calls": 3, "duration_seconds": 1.5},
            tags=tags,
        )

    def add_feedback(
        self,
        feedback_id="feedback-1",
        *,
        run_id="run-1",
        label="ACCEPT",
        approved_uses=("training",),
        rating=5,
    ):
        return self.store.add_human_feedback(
            feedback_id=feedback_id,
            run_id=run_id,
            actor="reviewer@example.com",
            label=label,
            rationale={
                "reason": "Verified by human reviewer@example.com",
            },
            rating=rating,
            approved_uses=approved_uses,
        )

    def export_training_dataset(
        self,
        version="locks-v1",
    ):
        return self.store.export_candidate_dataset(
            output_directory=self.root / "datasets",
            dataset_version=version,
            criteria=DatasetExportCriteria(
                purpose="training",
                allowed_labels=("ACCEPT",),
                allowed_outcomes=("RESOLVED",),
                task_types=("LOCK_CONTENTION",),
                required_tags=("lock",),
                min_rating=4,
            ),
        )

    def test_run_summary_sanitizes_raw_agent_content_and_identifiers(self):
        stored = self.record_run()

        serialized = json.dumps(
            stored,
            ensure_ascii=False,
        )
        self.assertNotIn("customer_private_data", serialized)
        self.assertNotIn("do-not-persist", serialized)
        self.assertNotIn("user@example.com", serialized)
        self.assertNotIn("10.2.3.4", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertNotIn("db-password", serialized)
        self.assertTrue(stored["summary"]["query"]["redacted"])
        self.assertTrue(stored["summary"]["tool_output"]["redacted"])
        self.assertEqual(
            stored["summary"]["password"],
            "[REDACTED]",
        )

    def test_non_json_and_non_finite_values_are_rejected(self):
        with self.assertRaises(ExperienceValidationError):
            self.store.record_run_summary(
                run_id="invalid-summary",
                task_type="TEST",
                outcome="FAILED",
                summary={"unsupported": object()},
            )

        with self.assertRaises(ExperienceValidationError):
            self.store.record_run_summary(
                run_id="invalid-metric",
                task_type="TEST",
                outcome="FAILED",
                summary={},
                metrics={"score": float("nan")},
            )

    def test_feedback_is_explicit_human_data_with_scoped_uses(self):
        self.record_run()
        feedback = self.add_feedback(
            approved_uses=("training", "evaluation"),
        )

        self.assertEqual(
            feedback["actor"],
            "[EMAIL_REDACTED]",
        )
        self.assertEqual(
            feedback["approved_uses"],
            ["evaluation", "training"],
        )
        self.assertNotIn(
            "reviewer@example.com",
            json.dumps(feedback),
        )

        with self.assertRaises(RunNotFound):
            self.store.add_human_feedback(
                feedback_id="missing-run-feedback",
                run_id="unknown",
                actor="human",
                label="ACCEPT",
                rationale={},
            )

        with self.assertRaises(ExperienceValidationError):
            self.store.add_human_feedback(
                feedback_id="invalid-use-feedback",
                run_id="run-1",
                actor="human",
                label="ACCEPT",
                rationale={},
                approved_uses=("automatic-online-training",),
            )

    def test_export_filters_records_and_writes_hashed_manifest(self):
        self.record_run()
        self.add_feedback()

        self.record_run("run-evaluation-only")
        self.add_feedback(
            "feedback-evaluation-only",
            run_id="run-evaluation-only",
            approved_uses=("evaluation",),
        )

        self.record_run("run-rejected")
        self.add_feedback(
            "feedback-rejected",
            run_id="run-rejected",
            label="REJECT",
        )

        manifest = self.export_training_dataset()
        data_path = self.root / "datasets" / "locks-v1.jsonl"
        manifest_path = (
            self.root / "datasets" / "locks-v1.manifest.json"
        )
        records = [
            json.loads(line)
            for line in data_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(records[0]["run_id"], "run-1")
        self.assertNotIn("actor", records[0]["human_feedback"])
        self.assertEqual(
            manifest["sha256"],
            hashlib.sha256(data_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            json.loads(
                manifest_path.read_text(encoding="utf-8")
            ),
            manifest,
        )
        self.assertEqual(
            manifest["filter_criteria"][
                "requires_explicit_human_use_approval"
            ],
            True,
        )

        with self.assertRaises(DatasetVersionExists):
            self.export_training_dataset()

    def test_latest_feedback_supersedes_stale_export_approval(self):
        self.record_run()
        self.add_feedback()
        self.clock.advance(seconds=1)
        self.add_feedback(
            "feedback-2",
            label="REJECT",
            approved_uses=(),
            rating=1,
        )

        manifest = self.export_training_dataset()

        self.assertEqual(manifest["record_count"], 0)
        self.assertEqual(
            (self.root / "datasets" / "locks-v1.jsonl").read_bytes(),
            b"",
        )

    def test_promotion_passes_only_with_no_regression_safety_and_approval(self):
        self.record_run()
        self.add_feedback()
        self.export_training_dataset()

        decision = self.store.assess_candidate_promotion(
            candidate_id="prompt-candidate-7",
            dataset_version="locks-v1",
            baseline_metrics={
                "task_success": 0.80,
                "unsafe_actions": 0,
            },
            candidate_metrics={
                "task_success": 0.85,
                "unsafe_actions": 0,
            },
            metric_directions={
                "task_success": "higher",
                "unsafe_actions": "lower",
            },
            safety_results={
                "approval_enforced": True,
                "stale_identity_rejected": True,
            },
            approval=PromotionApproval(
                actor="lead@example.com",
                decision="APPROVE",
                rationale="Reviewed offline evaluation evidence.",
                approved_at="2026-08-22T04:05:00+00:00",
            ),
        )

        self.assertTrue(decision["promoted"])
        self.assertFalse(decision["applied_automatically"])
        audit = self.store.list_promotion_audit(
            candidate_id="prompt-candidate-7"
        )
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["promoted"])
        self.assertEqual(
            audit[0]["approval"]["actor"],
            "[EMAIL_REDACTED]",
        )

    def test_each_failed_promotion_gate_is_audited(self):
        self.record_run()
        self.add_feedback()
        self.export_training_dataset()

        regression = self.store.assess_candidate_promotion(
            candidate_id="regression",
            dataset_version="locks-v1",
            baseline_metrics={"task_success": 0.90},
            candidate_metrics={"task_success": 0.89},
            metric_directions={"task_success": "higher"},
            safety_results={"all_safety_tests": True},
            approval=PromotionApproval(
                actor="lead",
                decision="APPROVE",
                rationale="Manual review complete.",
            ),
        )
        unsafe = self.store.assess_candidate_promotion(
            candidate_id="unsafe",
            dataset_version="locks-v1",
            baseline_metrics={"task_success": 0.90},
            candidate_metrics={"task_success": 0.91},
            metric_directions={"task_success": "higher"},
            safety_results={"all_safety_tests": False},
            approval=PromotionApproval(
                actor="lead",
                decision="APPROVE",
                rationale="Manual review complete.",
            ),
        )
        unapproved = self.store.assess_candidate_promotion(
            candidate_id="unapproved",
            dataset_version="locks-v1",
            baseline_metrics={"task_success": 0.90},
            candidate_metrics={"task_success": 0.91},
            metric_directions={"task_success": "higher"},
            safety_results={"all_safety_tests": True},
            approval=None,
        )

        self.assertIn(
            "benchmark_regression:task_success",
            regression["reasons"],
        )
        self.assertIn(
            "safety_check_failed:all_safety_tests",
            unsafe["reasons"],
        )
        self.assertIn(
            "explicit_approval_missing",
            unapproved["reasons"],
        )
        self.assertEqual(
            len(self.store.list_promotion_audit()),
            3,
        )

    def test_empty_dataset_and_unknown_dataset_cannot_promote(self):
        self.export_training_dataset()
        decision = self.store.assess_candidate_promotion(
            candidate_id="empty-data",
            dataset_version="locks-v1",
            baseline_metrics={"task_success": 0.80},
            candidate_metrics={"task_success": 0.90},
            metric_directions={"task_success": "higher"},
            safety_results={"all_safety_tests": True},
            approval=PromotionApproval(
                actor="lead",
                decision="APPROVE",
                rationale="Review complete.",
            ),
        )
        self.assertIn("dataset_empty", decision["reasons"])

        with self.assertRaises(DatasetNotFound):
            self.store.assess_candidate_promotion(
                candidate_id="unknown-data",
                dataset_version="not-exported",
                baseline_metrics={"score": 1},
                candidate_metrics={"score": 1},
                metric_directions={"score": "higher"},
                safety_results={"safe": True},
                approval=None,
            )


if __name__ == "__main__":
    unittest.main()
