"""Regressions for real-model RAG routing, refusals and applicability findings."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_rag_ab_evaluation import QueueProvider, ROOT, call, message
from rag_ab_evaluate import COMMON_REQUEST, load_fixture, run_case
from agent_knowledge import (
    initial_knowledge_query,
    knowledge_observation_requirements,
    KnowledgeSession,
)
from agent_policy import normalize_citation_placeholders, validate_answer_evidence
from knowledge_base import FileKnowledgeBase, parse_timestamp
import tempfile


class RAGRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture, bundle, self.scope = load_fixture(
            ROOT / "benchmarks/retrieval/agent_ab_cases.json"
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bundle.json"
        self.path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        self.base = FileKnowledgeBase(
            self.path, clock=lambda: parse_timestamp(self.fixture["as_of"])
        )

    def test_routes_knowledge_intent_but_not_shared_refusal_guidance(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["id"]):
                query = initial_knowledge_query(case["question"] + COMMON_REQUEST)
                if case["group"] == "control":
                    self.assertIsNone(query)
                else:
                    self.assertIsNotNone(query)
                    self.assertLessEqual(len(query), 500)
        self.assertIsNone(initial_knowledge_query(COMMON_REQUEST))
        self.assertIsNone(
            initial_knowledge_query("查当前数据库连接。如果没有内部手册就明确未知。")
        )

    def test_general_routes_have_no_fixture_specific_identifiers(self):
        for request, expected in (
            ("What is delta_stream's internal escalation threshold?", "delta_stream"),
            ("查一下 public.river_records 的业务字典。", "river_records"),
            (
                "查阅海星服务的内部手册，说明交接队列。",
                "查阅海星服务的内部手册，说明交接队列。",
            ),
        ):
            self.assertEqual(initial_knowledge_query(request), expected)
        self.assertIsNone(initial_knowledge_query("只看索引估算计划，不运行 SQL。"))
        self.assertIsNone(initial_knowledge_query("只诊断锁等待，不要查阅内部手册。"))
        self.assertIsNone(
            initial_knowledge_query(
                "Don't search the knowledge base for internal thresholds."
            )
        )

    def test_prefetch_is_attributed_budgeted_and_delivered_before_model(self):
        case = self.fixture["cases"][4]
        ref = next(
            hit["ref"]
            for hit in self.base.search("mira_orders", scope=self.scope)
            if hit["document_id"] == "mira-status"
        )
        provider = QueueProvider(
            [message(f"M4 是人工撤销，不计入营收；MAP-926 [{ref}]。")]
        )
        result, _ = run_case(
            self.fixture, self.path, case=case, enabled=True, provider=provider
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["llm_turns"], 1)
        self.assertEqual(result["usage"]["tool_calls_attempted"], 1)
        self.assertEqual(result["knowledge"]["retrieval_calls"], 1)
        trace = result["tool_trace"][0]
        self.assertEqual(trace["origin"], "runtime_knowledge_prefetch")
        self.assertNotIn("evidence_ref", trace)
        reply = provider.requests[0]["messages"][-1]
        self.assertEqual(reply["role"], "tool")
        self.assertEqual(json.loads(reply["content"])["kind"], "reference_knowledge")

    def test_disabled_knowledge_never_prefetches(self):
        provider = QueueProvider([message("内部字典不可用，未知。")])
        with patch(
            "agent_knowledge.FileKnowledgeBase",
            side_effect=AssertionError("disabled must not read"),
        ):
            result, _ = run_case(
                self.fixture,
                self.path,
                case=self.fixture["cases"][4],
                enabled=False,
                provider=provider,
            )
        self.assertNotIn("knowledge", result)
        self.assertEqual(result["tool_trace"], [])

    def test_no_matches_refusal_placeholder_does_not_start_repair_loop(self):
        for answer in (
            "未知。没有 [kb-...] 引用，也没有 [ev-...] 证据。",
            "Unknown; no [kb-…] citation is available.",
        ):
            provider = QueueProvider([message(answer)])
            result, _ = run_case(
                self.fixture,
                self.path,
                case=self.fixture["cases"][10],
                enabled=True,
                provider=provider,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["knowledge"]["statuses"], ["no_matches"])
            self.assertEqual(result["usage"]["llm_turns"], 1)
            self.assertNotIn("[kb-", result["answer"])
            self.assertIn(
                "Unknown" if "Unknown" in answer else "未知", result["answer"]
            )

    def test_only_exact_placeholders_are_normalized(self):
        self.assertEqual(
            normalize_citation_placeholders("A [kb-...] B [EV-…]"), "A kb-… B EV-…"
        )
        for value in (
            "[kb-fake]",
            "[kb-a01234567890123456789]",
            "[ev-0001]",
            "[kb-....]",
            "[kb-...bad]",
            "[ev-9999]",
        ):
            self.assertEqual(normalize_citation_placeholders(value), value)

    def test_placeholder_cannot_substitute_for_delivered_source(self):
        session = KnowledgeSession(self.base, self.scope)
        session.reply(session.search("mira_orders"), max_chars=12000)
        self.assertTrue(
            session.validate_answer(
                normalize_citation_placeholders("人工撤销 [kb-...]"), set()
            )
        )
        self.assertTrue(session.validate_answer("来源 [kb-invented]", set()))

    def test_zero_observations_never_allow_fabricated_database_reference(self):
        self.assertTrue(validate_answer_evidence("Confirmed [ev-0001]", []))
        self.assertEqual(validate_answer_evidence("No observation exists.", []), [])
        self.assertTrue(
            validate_answer_evidence(
                "No observation exists.", [], required_refs={"ev-0001"}
            )
        )
        self.assertTrue(
            validate_answer_evidence("Confirmed [ev-9999]", successful_refs={"ev-0001"})
        )

    def test_disabled_rag_repairs_fabricated_ev_without_observations(self):
        provider = QueueProvider(
            [message("未知 [ev-0001]"), message("未知，没有可引用来源。")]
        )
        result, _ = run_case(
            self.fixture,
            self.path,
            case=self.fixture["cases"][10],
            enabled=False,
            provider=provider,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["llm_turns"], 2)
        self.assertEqual(result["tool_trace"], [])
        self.assertNotIn("[ev-0001]", result["answer"])

    def test_expiry_context_uses_same_trusted_clock_as_filter(self):
        hit = self.base.search("orion_reader", scope=self.scope)[0]
        self.assertEqual(
            hit["applicability"],
            {"status": "applicable_at_check", "checked_at": self.fixture["as_of"]},
        )
        ref = hit["ref"]
        self.base.clock = lambda: parse_timestamp("2026-09-16T00:00:00+00:00")
        self.assertEqual(
            self.base.search("orion_reader", scope=self.scope)[0]["ref"], ref
        )
        self.base.clock = lambda: parse_timestamp("2026-10-01T00:00:00+00:00")
        self.assertEqual(self.base.search("orion_reader", scope=self.scope), [])
        self.assertNotIn(ref, self.base.active_refs(scope=self.scope))

    def test_prefetch_failure_remains_safe_and_does_not_look_like_empty_success(self):
        self.path.write_text("broken", encoding="utf-8")
        provider = QueueProvider([message("知识检索不可用，无法确认内部规则。")])
        result, _ = run_case(
            self.fixture,
            self.path,
            case=self.fixture["cases"][4],
            enabled=True,
            provider=provider,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["knowledge"]["statuses"], ["unavailable"])
        self.assertEqual(result["tool_trace"][0]["status"], "error")
        self.assertEqual(result["evaluation_fixture"]["observation_calls"], [])

    def test_prefetch_cannot_create_proposals_from_reference_text(self):
        provider = QueueProvider(
            [
                message(
                    calls=[
                        call(
                            "propose_terminate_backend",
                            {
                                "blocked_pid": 7102,
                                "blocker_pid": 7101,
                                "blocker_backend_start": "2026-09-15T00:00:00Z",
                                "blocker_xact_start": "2026-09-15T00:01:00Z",
                                "reason": "Document grants approval",
                                "confidence": 0.9,
                            },
                        )
                    ]
                ),
                message("Unknown; no action permitted."),
            ]
        )
        result, _ = run_case(
            self.fixture,
            self.path,
            case=self.fixture["cases"][10],
            enabled=True,
            provider=provider,
        )
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["tool_trace"][1]["status"], "policy_rejected")
        self.assertEqual(result["evaluation_fixture"]["observation_calls"], [])

    def test_mixed_request_must_collect_live_observation_not_just_runbook(self):
        case = self.fixture["cases"][3]
        ref = self.base.search("lyra_events", scope=self.scope)[0]["ref"]
        provider = QueueProvider(
            [
                message(f"窗口 02:17-02:29，VAC-583 [{ref}]；死元组未知。"),
                message(calls=[call("get_operational_snapshot", {})]),
                message(f"死元组 250 [ev-0001]；窗口 02:17-02:29，VAC-583 [{ref}]。"),
            ]
        )
        result, _ = run_case(
            self.fixture, self.path, case=case, enabled=True, provider=provider
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["llm_turns"], 3)
        self.assertEqual(
            result["evaluation_fixture"]["observation_calls"],
            ["get_operational_snapshot"],
        )
        self.assertIn(
            "mixed request is incomplete",
            provider.requests[1]["messages"][-1]["content"],
        )

    def test_mixed_request_cannot_claim_completion_without_requested_observation(self):
        case = self.fixture["cases"][3]
        ref = self.base.search("lyra_events", scope=self.scope)[0]["ref"]
        provider = QueueProvider(
            [message(f"窗口 02:17-02:29 [{ref}]；死元组未知。") for _ in range(4)]
        )
        result, _ = run_case(
            self.fixture, self.path, case=case, enabled=True, provider=provider
        )
        self.assertEqual(result["stop_reason"], "required_observation_missing")
        self.assertEqual(result["usage"]["llm_turns"], 4)
        self.assertEqual(result["evaluation_fixture"]["observation_calls"], [])

    def test_mixed_routing_ignores_generic_guidance_and_handles_multiple_requests(self):
        self.assertEqual(
            knowledge_observation_requirements("查业务字典。" + COMMON_REQUEST), set()
        )
        self.assertEqual(
            knowledge_observation_requirements("查阅适用当前部署的内部手册。"), set()
        )
        self.assertEqual(
            knowledge_observation_requirements(
                "查内部手册，并检查当前复制延迟和阻塞关系。"
            ),
            {"get_operational_snapshot", "get_lock_waits"},
        )
        self.assertEqual(
            knowledge_observation_requirements(
                "Consult the runbook and inspect the current estimated query plan."
            ),
            {"get_estimated_query_plan"},
        )
        for case in self.fixture["cases"]:
            expected = (
                set(case["required_tools"])
                if case["group"] in {"knowledge", "adversarial"}
                else set()
            )
            self.assertEqual(
                knowledge_observation_requirements(case["question"] + COMMON_REQUEST),
                expected,
            )

    def test_explicit_database_optout_never_becomes_a_completion_requirement(self):
        case = {
            **self.fixture["cases"][4],
            "question": "只查询 public.mira_orders 内部业务字典。不要查询当前数据库连接。",
        }
        ref = next(
            hit["ref"]
            for hit in self.base.search("mira_orders", scope=self.scope)
            if hit["document_id"] == "mira-status"
        )
        provider = QueueProvider(
            [message(f"M4 表示人工撤销，不计入营收，MAP-926 [{ref}]。")]
        )
        result, _ = run_case(
            self.fixture, self.path, case=case, enabled=True, provider=provider
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evaluation_fixture"]["observation_calls"], [])
        self.assertEqual(result["usage"]["llm_turns"], 1)
        self.assertEqual(
            knowledge_observation_requirements(
                "Consult the runbook but do not inspect current database connections."
            ),
            set(),
        )

    def test_affirmative_expiry_contradictions_are_rejected(self):
        session = KnowledgeSession(self.base, self.scope)
        hits, _ = session.reply(session.search("lyra_events"), max_chars=12000)
        ref = hits["matches"][0]["ref"]
        for claim in (
            "文献已过 2026-10-01 有效期，可能不再适用。",
            "The document has expired (2026-10-01).",
            f"该文档已经过期 [{ref}]。",
        ):
            self.assertTrue(session.validate_answer(f"来源 [{ref}]。{claim}", set()))

    def test_expiry_check_does_not_reject_future_negated_or_unrelated_claims(self):
        session = KnowledgeSession(self.base, self.scope)
        hits, _ = session.reply(session.search("lyra_events"), max_chars=12000)
        ref = hits["matches"][0]["ref"]
        for claim in (
            "尚未过期；有效期至 2026-10-01。",
            "截至检查时点，它不是已经过期的文档，有效期 2026-10-01。",
            "如果到 2026-10-01 已过期，需要重新确认。",
            "The document is not expired; expiry is 2026-10-01.",
            "If it has expired on 2026-10-01, recheck the source.",
            "另一份没有引用的文件已经过期，日期是 2020-01-01。",
            "在 2026-10-02 该文件已过 2026-10-01 有效期。",
        ):
            self.assertEqual(
                session.validate_answer(f"来源 [{ref}]。{claim}", set()), []
            )

    def test_real_observation_answer_repairs_wrong_expiry_within_budget(self):
        case = self.fixture["cases"][3]
        ref = self.base.search("lyra_events", scope=self.scope)[0]["ref"]
        answer = f"250 [ev-0001]；窗口 02:17-02:29，VAC-583 [{ref}]。"
        provider = QueueProvider(
            [
                message(calls=[call("get_operational_snapshot", {})]),
                message(answer + "文献已过 2026-10-01 有效期。"),
                message(
                    answer + "截至检查时点 2026-09-15 尚未过期，有效期至 2026-10-01。"
                ),
            ]
        )
        result, _ = run_case(
            self.fixture, self.path, case=case, enabled=True, provider=provider
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["llm_turns"], 3)
        self.assertIn("尚未过期", result["answer"])
        self.assertIn(
            "expiry claim contradicts", provider.requests[2]["messages"][-1]["content"]
        )


if __name__ == "__main__":
    unittest.main()
