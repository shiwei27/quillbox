#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""待办提取的失败可见性（离线，纳入回归）。

核心判据：**「AI 失败」与「这封邮件确实没有待办」必须能区分**。
改前两者都返回 `[]`，端点都回 `ok: true, count: 0`，界面都显示
「未从该邮件提取到待办」—— 用户无从判断该不该重试。
这与之前修"AI 摘要失败被伪装成正常结果"是同一类问题。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import ast
import io
import os
import sys
import unittest
from unittest import mock

os.chdir(ROOT)
sys.path.insert(0, ROOT)


def read(p):
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


class TestExtractTodosReturnContract(unittest.TestCase):
    """None = 失败，[] = 确实没有。两者不能混。"""

    def _fn(self):
        tree = ast.parse(read("core/ai_engine.py"))
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "extract_todos":
                return n
        self.fail("找不到 extract_todos")

    def test_failure_branches_return_none(self):
        fn = self._fn()
        rets = [r for r in ast.walk(fn) if isinstance(r, ast.Return)]
        nones = [r for r in rets
                 if isinstance(r.value, ast.Constant) and r.value.value is None]
        self.assertEqual(len(nones), 2,
                         "应有两条失败分支（生成失败、解析失败）返回 None，实际 %d" % len(nones))

    def test_no_empty_list_return_left(self):
        """不能再有 `return []` —— 那正是把失败伪装成"没有待办"的写法。"""
        fn = self._fn()
        empties = [r for r in ast.walk(fn) if isinstance(r, ast.Return)
                   and isinstance(r.value, ast.List) and not r.value.elts]
        self.assertEqual(
            empties, [],
            "仍有 return []，失败会被伪装成「没有待办」")

    def test_generation_failure_yields_none(self):
        """真跑一遍：生成失败时必须是 None，不是 []。"""
        from core import ai_engine
        eng = ai_engine.AIEngine.__new__(ai_engine.AIEngine)
        with mock.patch.object(ai_engine.AIEngine, "_custom", return_value=""), \
             mock.patch.object(ai_engine.AIEngine, "_generate", return_value=""):
            out = ai_engine.AIEngine.extract_todos(eng, "主题", "正文")
        self.assertIsNone(out, "生成失败应返回 None，实际 %r" % (out,))

    def test_unparseable_yields_none(self):
        from core import ai_engine
        eng = ai_engine.AIEngine.__new__(ai_engine.AIEngine)
        with mock.patch.object(ai_engine.AIEngine, "_custom", return_value=""), \
             mock.patch.object(ai_engine.AIEngine, "_generate",
                               return_value="这不是 JSON，随便说点什么"):
            out = ai_engine.AIEngine.extract_todos(eng, "主题", "正文")
        self.assertIsNone(out, "解析失败应返回 None，实际 %r" % (out,))

    def test_genuinely_empty_yields_empty_list(self):
        """确实没有待办 -> []，必须与失败区分开。"""
        from core import ai_engine
        eng = ai_engine.AIEngine.__new__(ai_engine.AIEngine)
        with mock.patch.object(ai_engine.AIEngine, "_custom", return_value=""), \
             mock.patch.object(ai_engine.AIEngine, "_generate", return_value="[]"):
            out = ai_engine.AIEngine.extract_todos(eng, "主题", "正文")
        self.assertEqual(out, [], "确实没有待办应返回 []，实际 %r" % (out,))

    def test_normal_result_passes_through(self):
        from core import ai_engine
        eng = ai_engine.AIEngine.__new__(ai_engine.AIEngine)
        raw = '```json\n[{"content":"回复报价","priority":"high"}]\n```'
        with mock.patch.object(ai_engine.AIEngine, "_custom", return_value=""), \
             mock.patch.object(ai_engine.AIEngine, "_generate", return_value=raw):
            out = ai_engine.AIEngine.extract_todos(eng, "主题", "正文")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], "回复报价")


class TestEndpointReportsFailure(unittest.TestCase):
    """端点必须把失败报成 ok=False，而不是 ok=True/count=0。"""

    def setUp(self):
        self.src = read("server.py")

    def _endpoint_body(self):
        """按路由装饰器定位单封端点，再取它的函数体。

        不能直接 find("if items is None:") —— 批量路径里也有一处且在文件里更靠前，
        会取错段（第一版就这么假失败了）。
        """
        i = self.src.find('@app.post("/api/emails/{email_id}/extract-todos")')
        self.assertGreater(i, 0, "找不到单封提取端点")
        j = self.src.find("\n@app.", i + 10)
        return self.src[i:j if j > 0 else i + 4000]

    def test_single_endpoint_checks_none(self):
        self.assertIn("if items is None:", self._endpoint_body())

    def test_single_endpoint_returns_not_ok(self):
        body = self._endpoint_body()
        i = body.find("if items is None:")
        seg = body[i:i + 400]
        self.assertIn('"ok": False', seg, "items 为 None 时必须回 ok=False")

    def test_marked_retryable(self):
        """标成可重试，用户才知道再点一次可能就好了。"""
        self.assertIn('"retryable": True', self.src)


class TestBatchReportsFailure(unittest.TestCase):
    """批量路径原来把失败完全吞掉，报告只说"扫了 N 封加了 M 条"。"""

    def setUp(self):
        self.src = read("server.py")

    def test_counts_ai_failures(self):
        self.assertIn('"ai_failed"', self.src)

    def test_counts_per_email_errors(self):
        self.assertIn('_todo_batch["errors"]', self.src)

    def test_keeps_last_error(self):
        self.assertIn('"last_error"', self.src)

    def test_no_bare_except_pass_in_batch(self):
        """`except Exception: pass` 连痕迹都不留，是这类问题的根源写法。"""
        self.assertNotIn("                except Exception:\n                    pass",
                         self.src)


class TestNoOtherSilentSwallows(unittest.TestCase):
    def test_extract_todos_logs_before_returning_none(self):
        """返回 None 之前要有日志 —— 用户看界面，排障看日志，两边都得有。"""
        src = read("core/ai_engine.py")
        i = src.find("def extract_todos")
        seg = src[i:i + 1600]
        self.assertGreaterEqual(seg.count("log.warning"), 2,
                                "两条失败分支都应有日志")


if __name__ == "__main__":
    unittest.main(verbosity=2)
