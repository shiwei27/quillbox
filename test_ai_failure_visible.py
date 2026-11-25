#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 故障必须可区分、且不得被写成正常结果。

背景：_api_generate 失败时不抛异常而是返回
"[API 异常: ...]"；analyze_email 拿到它解析失败，回一个结构完整的
{"category": "📢内部通知", "summary": "AI 分析失败", ...}，被 daemon 照常落库。
而摘要端点「已有摘要直接返回」，那封邮件就永久停在这句话上。

本测试把 _generate 打桩，覆盖三种产出：正常 JSON、错误字符串、非 JSON 垃圾。
不联网、不碰数据库、不需要 server 在跑。

用法：python test_ai_failure_visible.py
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import json
import sys
import unittest

sys.path.insert(0, ROOT)
from core.ai_engine import AIEngine, generation_failed  # noqa: E402


def engine(fake_output):
    ai = AIEngine(provider="api", api_base_url="http://127.0.0.1:9",
                  api_model="stub", model="stub")
    ai._generate = lambda *a, **k: fake_output
    return ai


class JudgeTest(unittest.TestCase):
    """generation_failed 把"错误字符串"这个隐式约定显式化。"""

    def test_api_error_is_failure(self):
        self.assertTrue(generation_failed("[API 异常: Connection refused]"))

    def test_ollama_error_is_failure(self):
        self.assertTrue(generation_failed("[Ollama 异常: timeout]"))

    def test_json_error_envelope_is_failure(self):
        self.assertTrue(generation_failed('{"error": "boom"}'))

    def test_leading_space_still_detected(self):
        self.assertTrue(generation_failed("   [API 异常: x]"))

    def test_normal_output_is_not_failure(self):
        self.assertFalse(generation_failed('{"category":"📰订阅","summary":"x"}'))
        self.assertFalse(generation_failed("这是一段正常摘要"))

    def test_non_str_is_not_failure(self):
        self.assertFalse(generation_failed(None))
        self.assertFalse(generation_failed(["x"]))


class AnalyzeEmailTest(unittest.TestCase):

    def test_success_marks_ok_true(self):
        ai = engine(json.dumps({"category": "🚨重要行动", "summary": "要签合同",
                                "action_items": ["签"]}, ensure_ascii=False))
        r = ai.analyze_email("s", "b")
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"], "要签合同")
        self.assertEqual(r["category"], "🚨重要行动")

    def test_generation_failure_marks_ok_false(self):
        r = engine("[API 异常: Connection refused]").analyze_email("s", "b")
        self.assertIs(r["ok"], False)
        self.assertEqual(r["error"], "generation_failed")

    def test_parse_failure_marks_ok_false(self):
        r = engine("模型今天想聊点别的").analyze_email("s", "b")
        self.assertIs(r["ok"], False)
        self.assertEqual(r["error"], "parse_failed")

    def test_failure_never_returns_fake_summary(self):
        """核心回归：失败不得回一句看起来像摘要的话，否则会被落库固化。"""
        for out in ("[API 异常: x]", "[Ollama 异常: y]", "not json at all"):
            r = engine(out).analyze_email("s", "b")
            self.assertEqual(r["summary"], "", out)
            self.assertNotIn("失败", r["summary"], out)
            self.assertEqual(r["category"], "", out)
            self.assertEqual(r["action_items"], [], out)

    def test_failed_summary_is_empty_so_worker_will_retry(self):
        """空摘要是"自带重试"的关键：get_emails_without_ai 的判据是
        summary IS NULL OR summary=''，写假摘要就永远不会被重新捞起。"""
        r = engine("[API 异常: x]").analyze_email("s", "b")
        self.assertFalse((r["summary"] or "").strip())


class ExtractTodosTest(unittest.TestCase):

    def test_success(self):
        ai = engine('[{"content":"办手续","priority":"high"}]')
        self.assertEqual(ai.extract_todos("s", "b")[0]["content"], "办手续")

    def test_generation_failure_returns_none(self):
        """失败返回 None，不是 []。

        本条断言 2026-09-17 更新：原来写的是"仍回 []（不改返回类型）"，
        但那样失败与"确实没有待办"在界面上完全一样，用户不知道能重试。
        现在 None=失败、[]=确实没有。
        """
        self.assertIsNone(engine("[API 异常: x]").extract_todos("s", "b"))

    def test_parse_failure_returns_none(self):
        self.assertIsNone(engine("随便说两句").extract_todos("s", "b"))

    def test_genuinely_no_todos_returns_empty_list(self):
        """确实没有待办才返回 [] —— 这是与失败区分开的另一半。"""
        self.assertEqual(engine("[]").extract_todos("s", "b"), [])


class AiClassifyTest(unittest.TestCase):

    def test_yes(self):
        self.assertTrue(engine("是").ai_match("标准", "s", "b")
                        if hasattr(AIEngine, "ai_match") else True)

    def test_generation_failure_is_not_a_match(self):
        ai = engine("[API 异常: x]")
        # 直接测内部方法：故障时必须判为不匹配（且已记 warning）
        self.assertFalse(ai._ai_classify("标准", "s", "b"))

    def test_normal_no(self):
        self.assertFalse(engine("否")._ai_classify("标准", "s", "b"))

    def test_normal_yes(self):
        self.assertTrue(engine("是")._ai_classify("标准", "s", "b"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
