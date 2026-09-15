"""Retrieval quality, publication and reference-data boundary checks."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowledge_base import (
    CHUNK_CHARS,
    MAX_BUNDLE_BYTES,
    FileKnowledgeBase,
    KnowledgeError,
    KnowledgeScope,
    read_json,
    reviewed_bundle,
    terms,
)
from agent_knowledge import KnowledgeSession

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
SCOPE = KnowledgeScope("team-a", "development", 18)


def document(**changes):
    return {
        "id": "locks",
        "title": "Lock incident runbook",
        "source": "urn:safedba:test:locks",
        "revision": "v1",
        "reviewed_by": "test-fixture",
        "reviewed_at": "2026-09-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
        "scope_ids": ["team-a"],
        "environments": ["development"],
        "postgres_majors": [18],
        "text": "For lock incidents consult the database owner and gather fresh observations.",
        **changes,
    }


def payload(*documents):
    return {"schema_version": 1, "documents": list(documents) or [document()]}


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "published.json"
        self.base = FileKnowledgeBase(self.path, clock=lambda: NOW)

    def publish(self, data=None):
        self.path.write_text(
            json.dumps(reviewed_bundle(data or payload()), ensure_ascii=False),
            encoding="utf-8",
        )

    def test_rank_and_provenance_are_deterministic(self):
        self.publish(
            payload(
                document(),
                document(
                    id="capacity",
                    title="Connection capacity",
                    text="Connection pool saturation requires connection budget review.",
                ),
            )
        )
        first = self.base.search("connection pool", scope=SCOPE)
        self.assertEqual(first, self.base.search("connection pool", scope=SCOPE))
        self.assertEqual(first[0]["document_id"], "capacity")
        self.assertTrue(first[0]["ref"].startswith("kb-"))
        self.assertEqual(first[0]["revision"], "v1")
        self.assertEqual(first[0]["source"], "urn:safedba:test:locks")
        self.assertEqual(len(first[0]["content_sha256"]), 64)
        self.assertNotIn("_keywords", first[0])

    def test_scope_environment_and_version_are_filtered_before_ranking(self):
        self.publish(
            payload(
                document(),
                document(id="other-scope", scope_ids=["team-b"]),
                document(id="other-env", environments=["production"]),
                document(id="other-version", postgres_majors=[16]),
            )
        )
        self.assertEqual(
            [hit["document_id"] for hit in self.base.search("lock", scope=SCOPE)],
            ["locks"],
        )
        self.assertEqual(
            self.base.search(
                "lock", scope=KnowledgeScope("unknown", "development", 18)
            ),
            [],
        )

    def test_expired_and_not_yet_reviewed_sources_are_excluded(self):
        self.publish(
            payload(
                document(expires_at="2026-09-12T00:00:00Z"),
                document(id="future", reviewed_at="2026-12-01T00:00:00Z"),
            )
        )
        self.assertEqual(self.base.search("lock", scope=SCOPE), [])

    def test_empty_irrelevant_query_does_not_fabricate_sources(self):
        self.publish()
        for query in ("unrelatedxyz", "!!!"):
            self.assertEqual(self.base.search(query, scope=SCOPE), [])
        with self.assertRaises(KnowledgeError):
            self.base.search(" ", scope=SCOPE)

    def test_chinese_bigrams_and_curated_aliases(self):
        self.publish(
            payload(
                document(
                    title="连接容量",
                    text="连接池耗尽需要核对连接容量。",
                    keywords=["连接池打满", "connection pool"],
                )
            )
        )
        for query in ("连接池打满", "connection pool"):
            self.assertEqual(
                self.base.search(query, scope=SCOPE)[0]["document_id"], "locks"
            )
        self.assertNotIn("连", terms("连接池"))
        self.assertIn("连接", terms("连接池"))

    def test_chunk_refs_change_with_revision_or_content(self):
        self.publish(payload(document(text="lock " * 600)))
        hits = self.base.search("lock", scope=SCOPE, limit=8)
        self.assertGreater(len(hits), 1)
        self.assertTrue(all(len(hit["text"]) <= CHUNK_CHARS for hit in hits))
        refs = {hit["ref"] for hit in hits}
        self.publish(payload(document(text="lock " * 600, revision="v2")))
        self.assertFalse(refs & self.base.active_refs(scope=SCOPE))

    def test_only_published_checksum_valid_payloads_are_read(self):
        self.path.write_text(json.dumps(payload()), encoding="utf-8")
        with self.assertRaises(KnowledgeError):
            self.base.search("lock", scope=SCOPE)
        self.publish()
        bundle = json.loads(self.path.read_text())
        bundle["documents"][0]["text"] = "tampered lock guidance"
        self.path.write_text(json.dumps(bundle), encoding="utf-8")
        with self.assertRaises(KnowledgeError):
            self.base.search("lock", scope=SCOPE)

    def test_duplicate_keys_nonfinite_and_oversized_json_fail(self):
        for content in (
            '{"a":1,"a":2}',
            '{"x":NaN}',
            " " * (MAX_BUNDLE_BYTES + 1),
            "{broken",
        ):
            self.path.write_text(content, encoding="utf-8")
            with self.assertRaises(KnowledgeError):
                read_json(self.path)

    def test_document_metadata_is_strict(self):
        for change in (
            {"source": "file:///secret"},
            {"source": "https://user:secret@host/doc"},
            {"source": "https://host/doc?token=abc"},
            {"scope_ids": ["*"]},
            {"environments": ["prod"]},
            {"postgres_majors": [True]},
            {"expires_at": "2020-01-01T00:00:00Z"},
            {"reviewed_at": "2026-09-01"},
            {"unknown_field": 1},
            {"text": "x" * 40001},
            {"text": "\ud800"},
            {"reviewed_by": ""},
        ):
            with self.subTest(fields=list(change)):
                with self.assertRaises(KnowledgeError):
                    reviewed_bundle(payload(document(**change)))
        with self.assertRaises(KnowledgeError):
            reviewed_bundle(payload(document(), document()))

    def test_obvious_secrets_are_rejected_without_echoing_them(self):
        for secret in (
            "password=do-not-publish",
            "Bearer abcdefgh123456",
            "sk-abcdefgh123456789",
            "postgresql://name:do-not-publish@host/db",
        ):
            with self.assertRaises(KnowledgeError) as error:
                reviewed_bundle(payload(document(text=secret)))
            self.assertNotIn(secret, str(error.exception))

    def test_missing_file_error_does_not_expose_path(self):
        with self.assertRaises(KnowledgeError) as error:
            self.base.search("lock", scope=SCOPE)
        self.assertNotIn(str(self.path), str(error.exception))
        self.assertFalse(self.path.exists())

    def test_read_only_search_never_opens_a_url_or_writes_bundle(self):
        self.publish(
            payload(document(source="https://example.invalid/private-runbook"))
        )
        before = self.path.read_bytes()
        with patch(
            "socket.create_connection", side_effect=AssertionError("Network forbidden")
        ):
            self.assertTrue(self.base.search("lock", scope=SCOPE))
        self.assertEqual(before, self.path.read_bytes())

    def test_citation_validation_rejects_undelivered_and_fake_database_refs(self):
        self.publish()
        session = KnowledgeSession(self.base, SCOPE)
        result, _ = session.reply(session.search("lock"), max_chars=4000)
        ref = result["matches"][0]["ref"]
        self.assertEqual(session.validate_answer(f"Runbook [{ref}].", set()), [])
        for answer in (
            "No citation.",
            "Source [kb-unknown].",
            f"Runbook [{ref}], database [ev-0001].",
        ):
            self.assertTrue(session.validate_answer(answer, set()))
        self.assertNotIn("text", session.summary()["sources"][0])

    def test_revocation_and_expiry_are_checked_again_at_answer_time(self):
        self.publish()
        session = KnowledgeSession(self.base, SCOPE)
        result, _ = session.reply(session.search("lock"), max_chars=4000)
        answer = f"Reference [{result['matches'][0]['ref']}]."
        self.base.clock = lambda: datetime(2028, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(session.validate_answer(answer, set()))
        self.base.clock = lambda: NOW
        self.publish(payload(document(scope_ids=["team-b"])))
        self.assertTrue(session.validate_answer(answer, set()))

    def test_small_output_budget_does_not_register_undelivered_citations(self):
        self.publish()
        session = KnowledgeSession(self.base, SCOPE)
        result, encoded = session.reply(session.search("lock"), max_chars=256)
        self.assertLessEqual(len(encoded), 256)
        self.assertEqual(result["status"], "context_budget_exceeded")
        self.assertEqual(session.sources, {})

    def test_call_and_total_context_budgets(self):
        self.publish()
        session = KnowledgeSession(self.base, SCOPE)
        for _ in range(3):
            session.reply(session.search("lock"), max_chars=4000)
        self.assertEqual(session.search("lock")["status"], "call_budget_exceeded")
        session.remaining_chars = 10
        result, _ = session.reply(
            {"kind": "reference_knowledge", "status": "ok", "matches": []},
            max_chars=4000,
        )
        self.assertEqual(result["status"], "context_budget_exceeded")

    def test_cli_requires_attestation_and_never_overwrites_existing_file(self):
        source = Path(self.temp.name) / "input.json"
        source.write_text(json.dumps(payload()), encoding="utf-8")
        command = [
            sys.executable,
            "-B",
            str(ROOT / "src/knowledge_cli.py"),
            "publish",
            "--input",
            str(source),
            "--output",
            str(self.path),
        ]
        denied = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(self.path.exists())
        approved = subprocess.run(
            command + ["--approve-reviewed-content"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(approved.returncode, 0, approved.stderr)
        before = self.path.read_bytes()
        repeated = subprocess.run(
            command + ["--approve-reviewed-content"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
