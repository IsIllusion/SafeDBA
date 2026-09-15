"""Offline labeled retrieval evaluation. Does not call a model or database."""

import argparse
import json
from pathlib import Path
import tempfile
import time

from knowledge_base import (
    FileKnowledgeBase,
    KnowledgeError,
    KnowledgeScope,
    parse_timestamp,
    read_json,
    reviewed_bundle,
)


def evaluate_retrieval(retriever, cases, *, scope, limit=3):
    if not isinstance(cases, list) or not cases or len(cases) > 200:
        raise KnowledgeError("Retrieval evaluation requires 1 to 200 labeled cases.")
    positives = hits = negatives = correct_rejections = 0
    reciprocal_rank = 0.0
    elapsed = []
    outcomes = []
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {"id", "query", "relevant_document_ids"}
            or not isinstance(case["id"], str)
            or not case["id"]
            or not isinstance(case["query"], str)
            or not isinstance(case["relevant_document_ids"], list)
            or any(not isinstance(item, str) for item in case["relevant_document_ids"])
        ):
            raise KnowledgeError("Invalid labeled retrieval case.")
        expected = set(case["relevant_document_ids"])
        started = time.perf_counter()
        results = retriever.search(case["query"], scope=scope, limit=limit)
        elapsed.append((time.perf_counter() - started) * 1000)
        documents = list(dict.fromkeys(item["document_id"] for item in results))
        rank = next(
            (i for i, item in enumerate(results, 1) if item["document_id"] in expected),
            None,
        )
        if expected:
            positives += 1
            hits += int(rank is not None)
            reciprocal_rank += 1 / rank if rank is not None else 0
            passed = rank is not None
        else:
            negatives += 1
            correct_rejections += int(not documents)
            passed = not documents
        outcomes.append(
            {
                "id": case["id"],
                "passed": passed,
                "returned_document_ids": documents,
                "first_relevant_rank": rank,
            }
        )
    ordered = sorted(elapsed)
    return {
        "cases": outcomes,
        "passed": all(case["passed"] for case in outcomes),
        "positive_cases": positives,
        "negative_cases": negatives,
        "top_k": limit,
        "hit_rate_at_k": hits / positives if positives else None,
        "mean_reciprocal_rank_at_k": reciprocal_rank / positives if positives else None,
        "no_match_accuracy": correct_rejections / negatives if negatives else None,
        "retrieval_p50_ms": round(ordered[(len(ordered) - 1) // 2], 3),
        "retrieval_max_ms": round(max(ordered), 3),
    }


class NoKnowledgeBaseline:
    def search(self, query, *, scope, limit=3):
        return []


def evaluate_fixture(path):
    fixture = read_json(path)
    if not isinstance(fixture, dict) or set(fixture) != {
        "scope",
        "as_of",
        "documents",
        "cases",
    }:
        raise KnowledgeError("Invalid retrieval fixture.")
    scope = KnowledgeScope(**fixture["scope"])
    as_of = parse_timestamp(fixture["as_of"])
    bundle = reviewed_bundle({"schema_version": 1, "documents": fixture["documents"]})
    with tempfile.TemporaryDirectory(prefix="safedba-retrieval-") as directory:
        bundle_path = Path(directory) / "bundle.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        measured = evaluate_retrieval(
            FileKnowledgeBase(bundle_path, clock=lambda: as_of),
            fixture["cases"],
            scope=scope,
        )
    return {
        "schema_version": 1,
        "dataset_kind": "synthetic_retrieval_fixture",
        "bundle_sha256": bundle["sha256"],
        "as_of": fixture["as_of"],
        "live_llm_used": False,
        "method": "lexical_bm25_english_cjk_bigrams",
        "scope": fixture["scope"],
        "baseline_no_knowledge": evaluate_retrieval(
            NoKnowledgeBaseline(), fixture["cases"], scope=scope
        ),
        "retrieval": measured,
        "limitations": "Retrieval-only fixture. Does not measure real-model diagnosis correctness, citation entailment, or production retrieval quality.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        default=str(
            Path(__file__).resolve().parents[1]
            / "benchmarks/retrieval/controlled_knowledge.json"
        ),
    )
    parser.add_argument("--report", help="Optional new report file (no overwrite)")
    args = parser.parse_args(argv)
    try:
        report = evaluate_fixture(args.fixture)
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.report:
            with Path(args.report).open("x", encoding="utf-8") as stream:
                stream.write(output + "\n")
        print(output)
        return 0 if report["retrieval"]["passed"] else 1
    except (KnowledgeError, OSError, TypeError, KeyError):
        print(
            '{"status":"failed","reason":"Invalid retrieval fixture or unavailable output."}'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
