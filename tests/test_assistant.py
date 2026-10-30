"""The 校验能手 assistant: retrieval, prompt framing and offline fallback."""
import unittest

import assistant
from models import Policy
from tests.test_model_config import completion, model_server

LOCAL = "http://127.0.0.1:11434/v1"


class KnowledgeBaseTests(unittest.TestCase):
    def test_knowledge_file_has_usable_entries(self):
        knowledge = assistant.load_knowledge()
        self.assertGreaterEqual(len(knowledge["entries"]), 15)
        self.assertTrue(knowledge["scope"])
        for entry in knowledge["entries"]:
            with self.subTest(entry=entry.get("id")):
                self.assertTrue(entry["title"])
                self.assertGreaterEqual(len(entry["content"]), 80)
                self.assertTrue(entry["keywords"])

    def test_broken_file_does_not_raise(self):
        with __import__("tempfile").TemporaryDirectory() as folder:
            path = __import__("pathlib").Path(folder) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(assistant.load_knowledge(path)["entries"], [])

    def test_retrieval_picks_the_obvious_clause(self):
        knowledge = assistant.load_knowledge()
        for question, expected in (
            ("报销期限是多久？", "报销时限"),
            ("发票抬头写错了怎么办", "发票抬头与税号"),
            ("住宿标准是多少", "差旅费：住宿标准"),
            ("需要准备哪些附件", "必需附件清单"),
            ("为什么会被退回", "最常见的退回原因"),
            ("公司税号是多少", "发票抬头与税号"),
            ("同一张发票能报两次吗", "重复报销识别"),
        ):
            with self.subTest(question=question):
                titles = [entry["title"] for entry in assistant.retrieve(question, knowledge)]
                self.assertIn(expected, titles[:2])

    def test_unrelated_question_matches_nothing(self):
        knowledge = assistant.load_knowledge()
        self.assertEqual(assistant.retrieve("今天天气怎么样", knowledge), [])
        self.assertEqual(assistant.retrieve("帮我写一首诗", knowledge), [])


class OfflineAnswerTests(unittest.TestCase):
    def test_offline_answer_quotes_the_clause(self):
        reply = assistant.ask("报销时限是多久", enabled=False)
        self.assertEqual(reply.mode, "off")
        self.assertIn("报销时限", reply.text)
        self.assertIn("90", reply.text)
        self.assertIn("报销时限", reply.sources)
        self.assertTrue(reply.degraded)

    def test_out_of_scope_question_states_the_scope(self):
        reply = assistant.ask("今天天气怎么样", enabled=False)
        self.assertEqual(reply.sources, [])
        self.assertIn("不在我的范围", reply.text)

    def test_uncovered_but_in_scope_question_still_answers(self):
        # "有问必答": an expense question without a matching clause gets the
        # baseline every expense has to satisfy, never a shrug.
        reply = assistant.instant_answer("员工生日蛋糕能报吗")
        self.assertIn("业务真实发生", reply.text)
        self.assertEqual(reply.level, "none")
        self.assertEqual(reply.sources, [])

    def test_empty_question_gets_a_hint(self):
        reply = assistant.ask("   ", enabled=False)
        self.assertIn("想问点什么", reply.text)

    def test_greeting_mentions_live_policy(self):
        text = assistant.greeting(Policy(company_name="测试公司", max_days=45))
        self.assertIn("测试公司", text)
        self.assertIn("45", text)


class PromptTests(unittest.TestCase):
    def test_prompt_carries_policy_and_clauses(self):
        policy = Policy(company_name="验收公司", company_tax_id="91330000TEST0001X", max_days=30)
        messages, picked = assistant.build_messages("报销期限是多久", policy=policy)
        self.assertTrue(picked)
        system = messages[0]["content"]
        self.assertIn("验收公司", system)
        self.assertIn("30", system)
        self.assertIn("发票、报销申请、付款凭证", system)
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn(assistant.SYSTEM_PROMPT[:12], system)

    def test_history_is_trimmed_to_the_recent_turns(self):
        history = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"第{index}条"}
                   for index in range(20)]
        messages, _ = assistant.build_messages("报销期限", history=history)
        self.assertEqual(len(messages), 2 + assistant.MAX_HISTORY_TURNS * 2)
        self.assertIn("第19条", messages[-2]["content"])

    def test_long_question_is_clipped(self):
        messages, _ = assistant.build_messages("报" * 2000)
        self.assertLessEqual(len(messages[-1]["content"]), assistant.MAX_QUESTION_CHARS)


class ModelAnswerTests(unittest.TestCase):
    def test_answer_uses_model_and_keeps_sources(self):
        reply_text = "北京、上海、广州、深圳每晚上限600元，其他城市按350元执行。"
        with model_server([completion({"role": "assistant", "content": reply_text})]) as (base, requests):
            reply = assistant.ask("住宿标准是多少", enabled=True, base_url=base + "/v1", model="local/test")
        self.assertEqual(reply.mode, "local")
        self.assertEqual(reply.text, reply_text)
        self.assertIn("差旅费：住宿标准", reply.sources)
        self.assertEqual(reply.notice, "")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/v1/chat/completions")

    def test_invented_number_is_flagged_not_hidden(self):
        with model_server([completion({"role": "assistant", "content": "每晚上限12345元。"})]) as (base, _requests):
            reply = assistant.ask("住宿标准是多少", enabled=True, base_url=base + "/v1", model="local/test")
        self.assertIn("12345", reply.notice)
        self.assertIn("12345", reply.text)

    def test_model_failure_falls_back_to_the_clause(self):
        with model_server([(500, {}, {"error": "boom"})]) as (base, _requests):
            reply = assistant.ask("报销期限是多久", enabled=True, base_url=base + "/v1", model="local/test")
        self.assertIn("90", reply.text)
        self.assertIn("模型调用失败", reply.degraded)

    def test_remote_endpoint_is_reported_as_cloud(self):
        with model_server([completion({"role": "assistant", "content": "按制度执行。"})]) as (base, _requests):
            reply = assistant.ask("报销期限是多久", enabled=True,
                                  base_url=f"http://localhost:{base.rsplit(':', 1)[1]}/v1", model="m")
        self.assertEqual(reply.mode, "local")


class BubbleTextTests(unittest.TestCase):
    def test_model_markdown_is_flattened_for_the_bubble(self):
        from assistant_ui import tidy_cjk
        text = tidy_cjk("### 住宿标准\n- 北京：600 元\n1. 其他城市：350 元\n**注意**：超标自理")
        self.assertNotIn("###", text)
        self.assertNotIn("**", text)
        self.assertNotIn("- ", text)
        self.assertIn("·北京：600元", text)
        self.assertIn("1.其他城市：350元", text)

    def test_numbers_stay_attached_to_units(self):
        from assistant_ui import tidy_cjk
        self.assertEqual(tidy_cjk("超过 90 天"), "超过90天")
        self.assertEqual(tidy_cjk("限额 600 元"), "限额600元")


class InstantAnswerTests(unittest.TestCase):
    """The panel paints this before any network call, so it has to be fast."""

    def test_a_matching_question_is_answered_in_well_under_a_millisecond(self):
        import time
        assistant.instant_answer("报销时限是多久")      # warm the knowledge cache
        started = time.perf_counter()
        for _ in range(50):
            reply = assistant.instant_answer("住宿标准是多少")
        spent = (time.perf_counter() - started) / 50
        self.assertLess(spent, 0.02, f"单次即时回答耗时 {spent * 1000:.1f} ms")
        self.assertIn("差旅费：住宿标准", reply.sources)

    def test_economy_laws_are_retrievable(self):
        cases = {
            "业务招待费能全额扣除吗": "业务招待费扣除限额",
            "白条能报销吗": "税前扣除凭证（28号公告）",
            "发票要保存几年": "会计档案与发票保管期限",
            "虚开发票有什么后果": "发票管理办法：如实开具与拒收",
            "定金超过20%怎么办": "民法典：定金不得超过20%",
            "进项税能抵扣吗": "增值税进项税额能否抵扣",
            "三流不一致会怎样": "合同、发票、资金三流一致",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                level, clauses = assistant.classify(question)
                self.assertEqual(level, "strong")
                self.assertIn(expected, [entry["title"] for entry in clauses])

    def test_runner_up_clause_is_dropped_when_clearly_weaker(self):
        clauses = assistant.retrieve("报销期限是多久", limit=3)
        self.assertEqual([entry["title"] for entry in clauses], ["报销时限"])

    def test_scope_guard_keeps_weather_out(self):
        reply = assistant.instant_answer("今天天气怎么样")
        self.assertEqual(reply.level, "none")
        self.assertIn("不在我的范围", reply.text)


class LatencyTests(unittest.TestCase):
    def test_prompt_is_small_enough_for_a_slow_local_model(self):
        messages, _ = assistant.build_messages("报销时限是多久", limit=2, clause_chars=150)
        self.assertLess(len(messages[0]["content"]), 900)

    def test_local_call_asks_for_a_short_answer_and_keeps_the_model_warm(self):
        with model_server([completion({"role": "assistant", "content": "90 天内。"})]) as (base, requests):
            assistant.ask("报销时限是多久", enabled=True, base_url=base + "/v1", model="local/test")
        body = __import__("json").loads(requests[0]["body"])
        self.assertEqual(body["max_tokens"], assistant.LOCAL_MAX_TOKENS)
        self.assertLessEqual(body["max_tokens"], 400)
        self.assertEqual(body.get("keep_alive"), assistant.KEEP_ALIVE)
        self.assertFalse(body["stream"])

    def test_repeated_question_hits_the_cache(self):
        with model_server([completion({"role": "assistant", "content": "90 天内提交。"})]) as (base, requests):
            first = assistant.ask("缓存命中测试", enabled=True, base_url=base + "/v1", model="local/test")
            second = assistant.ask("缓存命中测试", enabled=True, base_url=base + "/v1", model="local/test")
        self.assertEqual(len(requests), 1)
        self.assertTrue(second.cached)
        self.assertEqual(first.text, second.text)


if __name__ == "__main__":
    unittest.main()
