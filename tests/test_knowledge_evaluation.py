from pathlib import Path
import unittest

from test_knowledge_base import SCOPE
from knowledge_evaluate import evaluate_fixture, evaluate_retrieval, NoKnowledgeBaseline
from knowledge_base import KnowledgeError


class KnowledgeEvaluationTests(unittest.TestCase):
    def test_labeled_bilingual_fixture_and_no_match_cases(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "benchmarks/retrieval/controlled_knowledge.json"
        )
        report = evaluate_fixture(path)
        self.assertFalse(report["live_llm_used"])
        self.assertTrue(report["retrieval"]["passed"])
        self.assertEqual(report["retrieval"]["positive_cases"], 8)
        self.assertEqual(report["retrieval"]["negative_cases"], 5)
        self.assertEqual(report["retrieval"]["hit_rate_at_k"], 1.0)
        self.assertEqual(report["retrieval"]["mean_reciprocal_rank_at_k"], 1.0)
        self.assertEqual(report["retrieval"]["no_match_accuracy"], 1.0)
        self.assertEqual(report["baseline_no_knowledge"]["hit_rate_at_k"], 0.0)

    def test_empty_or_invalid_labels_are_not_reported_as_success(self):
        for cases in ([], [{"query": "lock"}]):
            with self.assertRaises(KnowledgeError):
                evaluate_retrieval(NoKnowledgeBaseline(), cases, scope=SCOPE)

    def test_reciprocal_rank_counts_duplicate_chunks_before_the_hit(self):
        class Retriever:
            def search(self, query, **kwargs):
                return [{"document_id": name} for name in ("a", "a", "b")]

        result = evaluate_retrieval(
            Retriever(),
            [{"id": "rank", "query": "query", "relevant_document_ids": ["b"]}],
            scope=SCOPE,
        )
        self.assertEqual(result["mean_reciprocal_rank_at_k"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
