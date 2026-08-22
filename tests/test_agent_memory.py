import json
import sqlite3
import sys
import tempfile
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


from agent_memory import (  # noqa: E402
    MemoryValidationError,
    RunNotFound,
    RunStateError,
    RunVersionConflict,
    SQLiteAgentMemory,
)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class AgentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "memory.sqlite3"
        self.clock = MutableClock(
            datetime(
                2026,
                8,
                22,
                0,
                0,
                tzinfo=timezone.utc,
            )
        )
        self.store = SQLiteAgentMemory(
            self.path,
            clock=self.clock,
            default_ttl_seconds=3600,
            max_entries_per_thread=100,
        )
        self.provenance = {
            "source": "agent-loop",
            "source_id": "run-1",
        }

    def save(
        self,
        content,
        *,
        thread_id="thread-1",
        session_id="session-1",
        role="user",
        **kwargs,
    ):
        provenance = kwargs.pop(
            "provenance",
            self.provenance,
        )
        return self.store.save_turn(
            thread_id=thread_id,
            session_id=session_id,
            role=role,
            content=content,
            provenance=provenance,
            **kwargs,
        )

    def test_turn_round_trip_is_session_scoped_and_persistent(self):
        stored = self.save(
            "Investigate lock waits",
            metadata={"database": "benchmark"},
        )
        self.save(
            "Different thread",
            thread_id="thread-2",
        )

        reopened = SQLiteAgentMemory(
            self.path,
            clock=self.clock,
        )
        recent = reopened.get_recent_session(
            thread_id="thread-1",
            session_id="session-1",
        )

        self.assertEqual(len(recent), 1)
        self.assertEqual(
            recent[0]["memory_id"],
            stored["memory_id"],
        )
        self.assertEqual(
            recent[0]["provenance"]["source"],
            "agent-loop",
        )
        self.assertEqual(
            recent[0]["metadata"]["database"],
            "benchmark",
        )

    def test_recent_session_returns_last_items_in_dialogue_order(self):
        self.save("first")
        self.clock.advance(seconds=1)
        self.save("second")
        self.clock.advance(seconds=1)
        self.save("third")

        recent = self.store.get_recent_session(
            thread_id="thread-1",
            session_id="session-1",
            limit=2,
        )

        self.assertEqual(
            [item["content"] for item in recent],
            ["second", "third"],
        )

    def test_cross_session_retrieval_is_relevant_and_deterministic(self):
        self.store.save_episode(
            thread_id="thread-1",
            session_id="old-session",
            content=(
                "lock blocker resolved by terminating idle transaction"
            ),
            provenance=self.provenance,
        )
        self.save(
            "index bloat was handled by reindex",
            session_id="other-session",
        )
        self.save(
            "current lock blocker evidence",
            session_id="current-session",
        )
        self.save(
            "lock blocker belongs to another thread",
            thread_id="thread-2",
            session_id="old-session",
        )

        first = self.store.retrieve_relevant_experiences(
            thread_id="thread-1",
            query="lock blocker idle transaction",
            current_session_id="current-session",
        )
        second = self.store.retrieve_relevant_experiences(
            thread_id="thread-1",
            query="lock blocker idle transaction",
            current_session_id="current-session",
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(
            first[0]["session_id"],
            "old-session",
        )
        self.assertGreater(first[0]["relevance_score"], 0)

        with_current = self.store.retrieve_relevant_experiences(
            thread_id="thread-1",
            query="lock blocker",
            current_session_id="current-session",
            include_current_session=True,
        )
        self.assertEqual(len(with_current), 2)

    def test_kind_filter_selects_only_episodes(self):
        self.save(
            "lock diagnosis turn",
            session_id="old-1",
        )
        self.store.save_episode(
            thread_id="thread-1",
            session_id="old-2",
            content="lock diagnosis episode",
            provenance=self.provenance,
        )

        found = self.store.retrieve_relevant_experiences(
            thread_id="thread-1",
            query="lock diagnosis",
            kinds=["episode"],
        )

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["memory_kind"], "episode")

    def test_expiry_is_excluded_and_cleanup_is_exact(self):
        self.save("short lived", ttl_seconds=10)
        self.save("long lived", ttl_seconds=20)
        self.clock.advance(seconds=10)

        recent = self.store.get_recent_session(
            thread_id="thread-1",
            session_id="session-1",
        )
        self.assertEqual(
            [item["content"] for item in recent],
            ["long lived"],
        )
        self.assertEqual(self.store.cleanup_expired(), 1)
        self.assertEqual(self.store.cleanup_expired(), 0)

    def test_retention_keeps_newest_thread_entries(self):
        store = SQLiteAgentMemory(
            Path(self.temporary.name) / "bounded.sqlite3",
            clock=self.clock,
            max_entries_per_thread=2,
        )
        for content in ("one", "two", "three"):
            store.save_turn(
                thread_id="thread-1",
                session_id="session-1",
                role="user",
                content=content,
                provenance=self.provenance,
            )

        recent = store.get_recent_session(
            thread_id="thread-1",
            session_id="session-1",
        )
        self.assertEqual(
            [item["content"] for item in recent],
            ["two", "three"],
        )

    def test_default_secret_redaction_covers_text_and_json(self):
        stored = self.save(
            "password=hunter2 Authorization: Bearer abc.def.ghi "
            "postgres://dba:dbpass@localhost/db "
            "sk-abcdefghijklmnop",
            metadata={
                "api_key": "top-secret",
                "nested": {"token": "another-secret"},
            },
            provenance={
                "source": "tool",
                "authorization": "Bearer secret-value",
            },
        )

        serialized = json.dumps(
            stored,
            ensure_ascii=False,
        )
        for secret in (
            "hunter2",
            "abc.def.ghi",
            "dbpass",
            "abcdefghijklmnop",
            "top-secret",
            "another-secret",
            "secret-value",
        ):
            self.assertNotIn(secret, serialized)

        with closing(sqlite3.connect(self.path)) as connection:
            raw = " ".join(
                str(value)
                for value in connection.execute(
                    "SELECT content, provenance_json, metadata_json "
                    "FROM memories"
                ).fetchone()
            )
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("top-secret", raw)

    def test_invalid_json_length_and_provenance_are_rejected(self):
        small = SQLiteAgentMemory(
            Path(self.temporary.name) / "small.sqlite3",
            max_content_chars=4,
        )
        with self.assertRaises(MemoryValidationError):
            small.save_turn(
                thread_id="thread",
                session_id="session",
                role="user",
                content="12345",
                provenance=self.provenance,
            )
        with self.assertRaises(MemoryValidationError):
            self.save(
                "bad metadata",
                metadata={"value": float("nan")},
            )
        with self.assertRaises(MemoryValidationError):
            self.store.save_turn(
                thread_id="thread",
                session_id="session",
                role="user",
                content="missing source",
                provenance={},
            )

    def test_delete_session_is_scoped_and_sql_injection_safe(self):
        self.save("keep", session_id="session-keep")
        self.save("delete", session_id="session-delete")
        self.save(
            "strange but safe",
            session_id="'; DELETE FROM memories; --",
        )

        deleted = self.store.delete_session(
            thread_id="thread-1",
            session_id="session-delete",
        )

        self.assertEqual(deleted, 1)
        self.assertEqual(
            len(
                self.store.get_recent_session(
                    thread_id="thread-1",
                    session_id="session-keep",
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                self.store.get_recent_session(
                    thread_id="thread-1",
                    session_id="'; DELETE FROM memories; --",
                )
            ),
            1,
        )

    def test_independent_connections_support_concurrent_saves(self):
        def save_number(number):
            return self.save(
                f"turn {number}",
                session_id=f"session-{number % 3}",
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            saved = list(pool.map(save_number, range(20)))

        self.assertEqual(len(saved), 20)
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]
        self.assertEqual(count, 20)


class AgentRunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteAgentMemory(
            Path(self.temporary.name) / "runs.sqlite3"
        )
        self.provenance = {"source": "agent-loop"}

    def start(self, *, run_id="run-1"):
        return self.store.start_run(
            run_id=run_id,
            thread_id="thread-1",
            session_id="session-1",
            checkpoint={"step": 0},
            provenance=self.provenance,
        )

    def test_run_start_checkpoint_pause_resume_and_complete(self):
        started = self.start()
        self.assertEqual(started["status"], "RUNNING")
        self.assertEqual(started["version"], 1)

        paused = self.store.checkpoint_run(
            "run-1",
            {"step": 1},
            expected_version=1,
            status="PAUSED",
        )
        self.assertEqual(paused["version"], 2)
        self.assertEqual(paused["status"], "PAUSED")

        resumed = self.store.checkpoint_run(
            "run-1",
            {"step": 2},
            expected_version=2,
        )
        self.assertEqual(resumed["status"], "RUNNING")

        complete = self.store.complete_run(
            "run-1",
            {"step": 3, "result": "ok"},
            expected_version=3,
        )
        self.assertEqual(complete["status"], "COMPLETED")
        self.assertEqual(complete["version"], 4)
        self.assertIsNotNone(complete["completed_at"])
        self.assertEqual(
            self.store.get_run("run-1")["checkpoint"]["result"],
            "ok",
        )

    def test_stale_or_backward_version_is_rejected(self):
        self.start()
        self.store.checkpoint_run(
            "run-1",
            {"step": 1},
            expected_version=1,
        )

        with self.assertRaises(RunVersionConflict):
            self.store.checkpoint_run(
                "run-1",
                {"step": "stale"},
                expected_version=1,
            )
        self.assertEqual(
            self.store.get_run("run-1")["checkpoint"],
            {"step": 1},
        )

    def test_terminal_run_and_illegal_terminal_api_are_rejected(self):
        self.start()
        with self.assertRaises(RunStateError):
            self.store.checkpoint_run(
                "run-1",
                {"step": 1},
                expected_version=1,
                status="COMPLETED",
            )
        with self.assertRaises(RunStateError):
            self.store.complete_run(
                "run-1",
                {"step": 1},
                expected_version=1,
                status="RUNNING",
            )

        self.store.complete_run(
            "run-1",
            {"step": 1},
            expected_version=1,
            status="FAILED",
        )
        with self.assertRaises(RunStateError):
            self.store.checkpoint_run(
                "run-1",
                {"step": 2},
                expected_version=2,
            )
        with self.assertRaises(RunStateError):
            self.store.complete_run(
                "run-1",
                {"step": 2},
                expected_version=2,
            )

    def test_duplicate_and_unknown_run_are_rejected(self):
        self.start()
        with self.assertRaises(RunVersionConflict):
            self.start()
        with self.assertRaises(RunNotFound):
            self.store.get_run("unknown")

    def test_run_checkpoint_is_json_safe_and_secret_redacted(self):
        run = self.store.start_run(
            run_id="secure-run",
            thread_id="thread-1",
            session_id="session-1",
            checkpoint={
                "password": "should-not-persist",
                "message": "token=also-secret",
            },
            provenance={
                "source": "agent-loop",
                "api_key": "source-secret",
            },
        )
        serialized = json.dumps(run)
        self.assertNotIn("should-not-persist", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertNotIn("source-secret", serialized)

        with self.assertRaises(MemoryValidationError):
            self.store.checkpoint_run(
                "secure-run",
                {"bad": float("nan")},
                expected_version=1,
            )


if __name__ == "__main__":
    unittest.main()
