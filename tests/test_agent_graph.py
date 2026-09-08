from concurrent.futures import ThreadPoolExecutor
import unittest

from test_agent_loop import AGENT, FakeProvider, response
from agent_graph import run_diagnostic_graph


class DiagnosticGraphTests(unittest.TestCase):
    def run_graph(self, *, turns=3, tools=True, stop=None):
        events = []

        def model(i):
            events.append(("model", i))
            return stop

        def tool_step(i):
            events.append(("tools", i))

        def answer(i):
            events.append(("answer", i))

        result = run_diagnostic_graph(
            model_step=model,
            tools_step=tool_step,
            answer_step=answer,
            has_tool_calls=lambda: tools,
            snapshot=lambda: {"messages": [], "usage": {}, "evidence_refs": []},
            exhausted=lambda: {"status": "stopped"},
            max_iterations=turns,
        )
        return events, result

    def test_graph_budget_does_not_truncate_32_model_turns(self):
        events, result = self.run_graph(turns=32)
        self.assertEqual(
            events, [(node, i) for i in range(32) for node in ("model", "tools")]
        )
        self.assertEqual(result["status"], "stopped")

    def test_answer_repairs_route_back_to_model_with_same_turn_budget(self):
        events, _ = self.run_graph(turns=3, tools=False)
        self.assertEqual(
            events, [(node, i) for i in range(3) for node in ("model", "answer")]
        )

    def test_terminal_model_result_does_not_execute_tools(self):
        events, result = self.run_graph(stop={"status": "failed"})
        self.assertEqual(events, [("model", 0)])
        self.assertEqual(result["status"], "failed")

    def test_graph_does_not_retry_failed_node(self):
        calls = []

        def fail(_):
            calls.append(1)
            raise RuntimeError("injected node failure")

        with self.assertRaises(RuntimeError):
            run_diagnostic_graph(
                model_step=fail,
                tools_step=fail,
                answer_step=fail,
                has_tool_calls=lambda: True,
                snapshot=lambda: {"messages": [], "usage": {}, "evidence_refs": []},
                exhausted=lambda: {},
                max_iterations=3,
            )
        self.assertEqual(calls, [1])

    def test_concurrent_runs_do_not_share_messages_or_results(self):
        def run(index):
            provider = FakeProvider([response(content=f"Result {index}")])
            result = AGENT.run_agent(
                f"Request {index}",
                provider=provider,
                use_memory=False,
                capture_experience=False,
            )
            return result["answer"], provider.message_batches[0]

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(8)))
        for index, (answer, messages) in enumerate(results):
            self.assertEqual(answer, f"Result {index}")
            self.assertEqual(
                [m["content"] for m in messages if m["role"] == "user"],
                [f"Request {index}"],
            )


if __name__ == "__main__":
    unittest.main()
