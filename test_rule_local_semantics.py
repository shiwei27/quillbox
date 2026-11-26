#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""规则的"移动"语义：界面必须说清只在本地生效；检查脚本必须只读。

## 背景

规则的"移动到文件夹"只调 `db.move_folder()`，**只改本地库、不动服务器**。
于是同一封邮件本地在规则的目标文件夹、服务器上仍在「收件箱」。
用户在 Outlook / 手机上看到的是服务器那套，就会认为"规则没生效"。

界面不写清楚，这个落差只能靠踩坑发现。所以把"说清楚"钉进测试。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import io
import os
import re
import sys
import unittest

os.chdir(ROOT)
sys.path.insert(0, ROOT)


def read(p):
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


class TestUiStatesLocalOnly(unittest.TestCase):
    """界面必须标明"仅 Quillbox 内"。"""

    def setUp(self):
        self.html = read("frontend/index.html")

    def test_badge_present(self):
        self.assertIn("仅 Quillbox 内", self.html)

    def test_hover_explains_consequence(self):
        """悬停说明要点出后果（别的客户端看到的不一样），不能只说"仅本地"。"""
        self.assertIn("不会移动邮箱服务器上的邮件", self.html)
        self.assertTrue(re.search(r"Outlook.*手机|手机.*Outlook", self.html))

    def test_rules_section_description_warns(self):
        i = self.html.find("邮件规则")
        seg = self.html[i:i + 3000]
        self.assertIn("只改 Quillbox 内的归类", seg + self.html[:i][-2000:],
                      "规则区说明里应提示移动只影响本地")


class TestMoveIsLocalOnlyByDesign(unittest.TestCase):
    """代码层面确认：规则的移动确实只动本地库。

    这条不是要求它必须只动本地 —— 而是把**现状**钉住：
    哪天有人加了"真的动服务器"的实现，这条会失败，从而提醒同步更新界面说明
    与一致性检查的结论。
    """

    def test_daemon_uses_db_move_only(self):
        d = read("daemon.py")
        i = d.find("def _apply_rules")
        body = d[i:d.find("\ndef ", i + 10)]
        self.assertIn("db.move_folder", body)
        for srv in ("move_to(", "COPY", "uid_copy", "item.move"):
            self.assertNotIn(srv, body,
                             "daemon 规则动作里出现了服务端写操作 %r，"
                             "若确要开这个能力，请同步更新界面说明与本测试" % srv)

    def test_server_apply_rules_uses_db_move_only(self):
        s = read("server.py")
        i = s.find("def apply_rules_existing")
        body = s[i:s.find("\n@app.", i + 10)]
        for srv in ("move_to(", "uid_copy", "item.move"):
            self.assertNotIn(srv, body)


@unittest.skipUnless(os.path.exists(os.path.join(ROOT, "_folder_consistency.py")), "ops script not shipped")
class TestConsistencyCheckerIsReadOnly(unittest.TestCase):
    """一致性检查脚本必须只读 —— 它的用途就是"先看清分歧"，不该顺手改东西。"""

    def setUp(self):
        src = read("_folder_consistency.py")
        # 剥掉注释与 docstring 再查：说明文字里会提到写操作
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(node)
                if doc:
                    src = src.replace(doc, "")
        self.code = re.sub(r"#.*", "", src)

    def test_no_db_writes(self):
        # 判据要带 SQL 上下文：光找 INSERT 会命中 sys.path.insert（第一版就误报了）
        for pat in (r"\bUPDATE\s+\w+\s+SET\b", r"\bINSERT\s+INTO\b",
                    r"\bDELETE\s+FROM\b", r"move_folder", r"\.commit\s*\("):
            self.assertIsNone(re.search(pat, self.code, re.I),
                              "检查脚本里出现写操作 %s" % pat)

    def test_no_server_writes(self):
        # 同理带上调用上下文：IMAP 的写是 conn.store()/uid('STORE',...)，
        # 光找裸词 STORE 会命中说明性文字
        for pat in (r"move_to\s*\(", r"\.save\s*\(", r"uid_copy\s*\(",
                    r"\.store\s*\(", r"['\"]STORE['\"]", r"['\"]EXPUNGE['\"]",
                    r"\bexpunge\s*\(", r"is_read\s*=\s*(?!=)"):
            self.assertIsNone(re.search(pat, self.code),
                              "检查脚本里出现服务端写操作 %s" % pat)

    def test_imap_uses_readonly_and_peek(self):
        self.assertIn("readonly=True", self.code)
        self.assertIn("BODY.PEEK[", self.code)

    def test_ews_only_fetches_message_id(self):
        """EWS 侧只取 message_id，不拉正文（几千封拉正文会很重）。"""
        self.assertIn('.only("message_id")', self.code)
        self.assertNotIn("mime_content", self.code)

    def test_reports_both_consequences(self):
        """报告要说出两个后果，只报数字没用。"""
        src = read("_folder_consistency.py")
        self.assertIn("弹回", src, "应提示重取可能把邮件弹回服务器的归属")
        self.assertIn("Outlook", src, "应提示别的客户端看到的是另一套归类")


class TestRulesEngineStillWorks(unittest.TestCase):
    """顺带守住规则匹配本身（用户最初的疑问是"规则没生效"）。"""

    def test_match_rule_exists(self):
        from core import ai_engine
        self.assertTrue(hasattr(ai_engine.AIEngine, "_match_rule"))

    def test_disabled_rule_skipped(self):
        from core import ai_engine
        eng = ai_engine.AIEngine.__new__(ai_engine.AIEngine)
        eng.prompts = {"rules": [{
            "name": "关着的", "enabled": False, "match_mode": "all",
            "conditions": [{"field": "from", "op": "contains", "value": "x@y"}],
            "actions": {"mark_read": True}}]}
        self.assertIsNone(eng._match_rule({"sender_email": "x@y.com",
                                           "subject": "s", "body": "b"}))

    def test_enabled_rule_matches(self):
        from core import ai_engine
        eng = ai_engine.AIEngine.__new__(ai_engine.AIEngine)
        eng.prompts = {"rules": [{
            "name": "开着的", "enabled": True, "match_mode": "all",
            "conditions": [{"field": "from", "op": "is",
                            "value": "system@notice.examplecloud.example"}],
            "actions": {"folder": "告警通知", "mark_read": True}}]}
        got = eng._match_rule({"sender_email": "system@notice.examplecloud.example",
                               "subject": "告警", "body": ""})
        self.assertIsNotNone(got)
        self.assertEqual((got.get("actions") or {}).get("folder"), "告警通知")

    def test_first_match_wins(self):
        """按顺序匹配、首个命中生效 —— 界面上就是这么写的。"""
        from core import ai_engine
        eng = ai_engine.AIEngine.__new__(ai_engine.AIEngine)
        mk = lambda n, f: {"name": n, "enabled": True, "match_mode": "all",
                           "conditions": [{"field": "from", "op": "contains",
                                           "value": "examplecloud"}],
                           "actions": {"folder": f}}
        eng.prompts = {"rules": [mk("第一条", "A"), mk("第二条", "B")]}
        got = eng._match_rule({"sender_email": "x@examplecloud.example",
                               "subject": "", "body": ""})
        self.assertEqual(got.get("name"), "第一条")


if __name__ == "__main__":
    unittest.main(verbosity=2)
