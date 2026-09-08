"""Differential replay against the frozen, pre-migration Agent loop."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_agent_loop import AGENT, CALLS, FakeProvider, response, tool_call

_baseline_globals = dict(vars(AGENT))
_fixture = Path(__file__).parent / "fixtures" / "legacy_agent_loop.py"
exec(
    compile(_fixture.read_text(encoding="utf-8"), str(_fixture), "exec"),
    _baseline_globals,
)
legacy_run_agent = _baseline_globals["run_agent"]


class ScriptedProvider(FakeProvider):
    def complete(self, **kwargs):
        result = super().complete(**kwargs)
        if isinstance(result, Exception):
            raise result
        return result


def normalized(value):
    if isinstance(value, dict):
        return {
            k: normalized(v)
            for k, v in value.items()
            if k not in {"duration_ms", "elapsed_ms"}
        }
    if isinstance(value, (list, tuple)):
        return [normalized(v) for v in value]
    return value


def observation(call_id="health-1", name="get_database_health", arguments=None):
    return response(
        calls=[tool_call(call_id, name, arguments or {})], finish_reason="tool_calls"
    )


def cases():
    final = response(content="Database evidence reviewed [ev-0001].")
    unknown = observation(name="not_a_tool")
    invalid = observation()
    invalid.choices[0].message.tool_calls[0].function.arguments = "{broken"
    absent_id = observation()
    absent_id.choices[0].message.tool_calls[0].id = None
    absent_name = observation()
    absent_name.choices[0].message.tool_calls[0].function.name = None
    proposal = tool_call(
        "proposal-1",
        "propose_create_index",
        {
            "table": "orders",
            "column": "customer_id",
            "query": "SELECT * FROM orders WHERE customer_id = 1",
            "reason": "Observed scan",
            "confidence": 0.9,
        },
    )
    supported_proposal = [
        response(
            calls=[
                tool_call(
                    "plan",
                    "analyze_query",
                    {"query": "SELECT * FROM orders WHERE customer_id = 1"},
                ),
                tool_call("indexes", "get_indexes", {"table_name": "orders"}),
            ],
            finish_reason="tool_calls",
        ),
        response(calls=[proposal], finish_reason="tool_calls"),
        response(content="Index proposal grounded in [ev-0001] [ev-0002] [ev-0003]."),
    ]
    return {
        "final_answer": ([response(content="No database claim.")], {}),
        "observation_and_citation": ([observation(), final], {}),
        "duplicate_observation": ([observation(), observation("health-2"), final], {}),
        "citation_repair": (
            [observation(), response(content="Missing reference."), final],
            {},
        ),
        "citation_budget": (
            [observation(), response(content="Missing reference.")],
            {"max_iterations": 2},
        ),
        "model_budget": ([observation()], {"max_iterations": 1}),
        "unknown_tool": ([unknown, response(content="No observation available.")], {}),
        "invalid_json": (
            [invalid, response(content="Invalid arguments were rejected.")],
            {},
        ),
        "invalid_argument_type": (
            [
                observation(arguments={"unexpected": True}),
                response(content="Rejected."),
            ],
            {},
        ),
        "missing_tool_id": ([absent_id], {}),
        "missing_tool_name": ([absent_name], {}),
        "no_choices": ([SimpleNamespace(choices=[])], {}),
        "no_message": ([SimpleNamespace(choices=[SimpleNamespace(message=None)])], {}),
        "empty_answer": ([response()], {}),
        "provider_timeout": ([TimeoutError("do not leak endpoint credentials")], {}),
        "inconsistent_finish_reason": (
            [response(calls=[tool_call("x", "get_database_health", {})])],
            {},
        ),
        "truncated_calls": (
            [
                response(
                    calls=[tool_call("x", "get_database_health", {})],
                    finish_reason="length",
                )
            ],
            {},
        ),
        "filtered_calls": (
            [
                response(
                    calls=[tool_call("x", "get_database_health", {})],
                    finish_reason="content_filter",
                )
            ],
            {},
        ),
        "truncated_answer": (
            [response(content="Partial answer.", finish_reason="length")],
            {},
        ),
        "filtered_answer": (
            [response(content="Partial answer.", finish_reason="content_filter")],
            {},
        ),
        "per_turn_budget": (
            [
                response(
                    calls=[
                        tool_call(str(i), "get_database_health", {}) for i in range(3)
                    ],
                    finish_reason="tool_calls",
                )
            ],
            {"max_tool_calls_per_turn": 2},
        ),
        "total_budget": (
            [observation(), observation("health-2")],
            {"max_total_tool_calls": 1, "max_tool_calls_per_turn": 1},
        ),
        "proposal_not_exposed": (
            [
                response(calls=[proposal], finish_reason="tool_calls"),
                response(content="No approved proposal."),
            ],
            {},
        ),
        "proposal_without_evidence": (
            [
                response(calls=[proposal], finish_reason="tool_calls"),
                response(content="No supporting evidence."),
            ],
            {"mode": "propose"},
        ),
        "same_turn_evidence": (
            [
                response(
                    calls=[
                        tool_call(
                            "plan",
                            "analyze_query",
                            {"query": "SELECT * FROM orders WHERE customer_id = 1"},
                        ),
                        proposal,
                    ],
                    finish_reason="tool_calls",
                ),
                final,
            ],
            {"mode": "propose"},
        ),
        "supported_proposal": (supported_proposal, {"mode": "propose"}),
        "disallowed_action": (
            supported_proposal,
            {"mode": "propose", "allowed_actions": {"ANALYZE_TABLE"}},
        ),
        "token_accounting": (
            [
                response(
                    content="Complete.",
                    usage=SimpleNamespace(
                        prompt_tokens=13, completion_tokens=7, total_tokens=20
                    ),
                )
            ],
            {},
        ),
        "output_truncation": (
            [observation(name="get_operational_snapshot"), final],
            {"max_tool_output_chars": 256},
        ),
    }


class LangGraphAlignmentTests(unittest.TestCase):
    maxDiff = 5000

    def replay(self, runner, script, options):
        CALLS.clear()
        provider = ScriptedProvider(deepcopy(script))
        result = runner(
            "Investigate database evidence.",
            provider=provider,
            run_id="415486aa-74fc-4791-a140-c364f8d21cc0",
            use_memory=False,
            capture_experience=False,
            **options,
        )
        return normalized(
            {
                "result": result,
                "calls": deepcopy(CALLS),
                "messages": provider.message_batches,
                "tool_sets": provider.tool_sets,
            }
        )

    def test_memory_lifecycle_checkpoints_remain_compatible(self):
        from agent_memory import SQLiteAgentMemory

        outputs = []
        for runner in (legacy_run_agent, AGENT.run_agent):
            with tempfile.TemporaryDirectory() as directory:
                store = SQLiteAgentMemory(
                    Path(directory) / "memory.sqlite3",
                    clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc),
                )
                result = runner(
                    "Inspect database health",
                    provider=ScriptedProvider(
                        [observation(), response(content="Evidence [ev-0001].")]
                    ),
                    memory_store=store,
                    thread_id="tenant-1",
                    session_id="session-1",
                    run_id="415486aa-74fc-4791-a140-c364f8d21cc0",
                    capture_experience=False,
                )
                saved = store.get_run(result["run_id"])
                self.assertEqual(saved["status"], "COMPLETED")
                outputs.append(normalized({"result": result, "saved": saved}))
        self.assertEqual(*outputs)

    def test_deadline_after_model_prevents_tool_execution(self):
        outputs = []
        for runner in (legacy_run_agent, AGENT.run_agent):
            now = [0.0]

            class SlowModel(ScriptedProvider):
                def complete(self, **kwargs):
                    result = super().complete(**kwargs)
                    now[0] = 100.0
                    return result

            clock = SimpleNamespace(monotonic=lambda: now[0])
            CALLS.clear()
            with patch.object(AGENT, "time", clock), patch.dict(
                _baseline_globals, {"time": clock}
            ):
                result = runner(
                    "Inspect",
                    provider=SlowModel([observation()]),
                    run_id="415486aa-74fc-4791-a140-c364f8d21cc0",
                    use_memory=False,
                    capture_experience=False,
                )
            self.assertEqual(result["stop_reason"], "deadline_exceeded")
            self.assertEqual(CALLS, [])
            outputs.append(normalized(result))
        self.assertEqual(*outputs)


def alignment_test(script, options):
    def test(self):
        original = self.replay(legacy_run_agent, script, options)
        migrated = self.replay(AGENT.run_agent, script, options)
        self.assertEqual(original, migrated)

    return test


for _name, (_script, _options) in cases().items():
    setattr(LangGraphAlignmentTests, "test_" + _name, alignment_test(_script, _options))


if __name__ == "__main__":
    unittest.main()
