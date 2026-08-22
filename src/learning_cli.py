"""CLI for the controlled, offline SafeDBA experience loop."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from experience_store import (
    DatasetExportCriteria,
    PromotionApproval,
    SQLiteExperienceStore,
)


DEFAULT_EXPERIENCE_DB_PATH = (
    Path(__file__).resolve().parents[1]
    / Path(
        os.getenv(
            "SAFEDBA_EXPERIENCE_DB_PATH",
            "logs/experience.sqlite3",
        )
    )
)


def _json_file(path: str) -> dict:
    value = json.loads(
        Path(path).read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise ValueError(
            f"{path} must contain a JSON object."
        )
    return value


def _print_json(value: object) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export explicitly reviewed Agent experience and assess "
            "candidate promotion gates. This command never trains or "
            "deploys a model by itself."
        )
    )
    parser.add_argument(
        "--database",
        default=str(DEFAULT_EXPERIENCE_DB_PATH),
        help="Experience SQLite database path.",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    export = commands.add_parser(
        "export",
        help="Export an immutable, versioned candidate JSONL dataset.",
    )
    export.add_argument("--version", required=True)
    export.add_argument(
        "--purpose",
        choices=("training", "evaluation"),
        required=True,
    )
    export.add_argument(
        "--label",
        action="append",
        dest="labels",
        required=True,
    )
    export.add_argument(
        "--outcome",
        action="append",
        default=[],
    )
    export.add_argument(
        "--task-type",
        action="append",
        default=[],
    )
    export.add_argument(
        "--tag",
        action="append",
        default=[],
    )
    export.add_argument("--min-rating", type=int)
    export.add_argument(
        "--output-directory",
        required=True,
    )

    promote = commands.add_parser(
        "assess-promotion",
        help=(
            "Audit whether a candidate passes non-regression, safety, "
            "and explicit-human-approval gates."
        ),
    )
    promote.add_argument("--candidate-id", required=True)
    promote.add_argument("--dataset-version", required=True)
    promote.add_argument("--baseline-metrics", required=True)
    promote.add_argument("--candidate-metrics", required=True)
    promote.add_argument("--metric-directions", required=True)
    promote.add_argument("--safety-results", required=True)
    promote.add_argument(
        "--approve",
        action="store_true",
        help="Record an explicit human approval decision.",
    )
    promote.add_argument("--actor")
    promote.add_argument("--rationale")

    commands.add_parser(
        "promotion-audit",
        help="List prior immutable promotion-gate decisions.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = SQLiteExperienceStore(args.database)

    if args.command == "export":
        result = store.export_candidate_dataset(
            output_directory=args.output_directory,
            dataset_version=args.version,
            criteria=DatasetExportCriteria(
                purpose=args.purpose,
                allowed_labels=tuple(args.labels),
                allowed_outcomes=tuple(args.outcome),
                task_types=tuple(args.task_type),
                required_tags=tuple(args.tag),
                min_rating=args.min_rating,
            ),
        )
        _print_json(result)
        return

    if args.command == "promotion-audit":
        _print_json(store.list_promotion_audit())
        return

    approval = None
    if args.approve:
        if not args.actor or not args.rationale:
            raise SystemExit(
                "--approve requires --actor and --rationale."
            )
        approval = PromotionApproval(
            actor=args.actor,
            decision="approve",
            rationale=args.rationale,
        )
    result = store.assess_candidate_promotion(
        candidate_id=args.candidate_id,
        dataset_version=args.dataset_version,
        baseline_metrics=_json_file(args.baseline_metrics),
        candidate_metrics=_json_file(args.candidate_metrics),
        metric_directions=_json_file(args.metric_directions),
        safety_results=_json_file(args.safety_results),
        approval=approval,
    )
    _print_json(result)


if __name__ == "__main__":
    main()
