#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 工具面的治理红线守卫（离线，纳入回归）。

## 为什么需要这个测试

MCP 面不提供 download_attachment 这类把本机文件送出的工具。曾经出现过：
**服务端的工具清单里一直声明着它**，只是客户端白名单把它禁掉了——
等于把红线交给调用方自觉，换个不带白名单的会话连上来就能拿到。

更危险的是它调的 `/api/emails/{id}/attachment` 当时并不存在，所以"看起来没事"。
一旦有人为了 Web 界面把这个端点加上，那个工具就会**自动变成可用**，
红线在没人察觉的情况下被打开。

所以红线必须钉在服务端，并且用测试守住：靠人记得是不够的。

## 边界（别把这条测试理解过宽）

* **MCP 面**：不得提供任何把本机文件内容送出的能力 —— 本测试守的是这个。
* **Web 面**：用户在本机界面里把附件从邮箱下到本机磁盘
  **不构成外流**，是允许且应该做的。本测试**不**约束 Web 端点。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import io
import os
import re
import sys
import unittest

os.chdir(ROOT)
sys.path.insert(0, ROOT)

import mcp_server as m                              # noqa: E402


def tool_names():
    return sorted(t.get("name") or "" for t in m.TOOLS)


class TestNoFileEgressTools(unittest.TestCase):
    """工具清单里不得出现能把文件送出去的工具。"""

    # 按能力命名，而不是只封一个具体名字：换个名字重新加进来同样要拦住
    FORBIDDEN_SUBSTRINGS = ("attach", "download", "upload", "export",
                            "save_to", "writefile", "write_file", "readfile",
                            "read_file", "getfile", "get_file", "fetch_file")

    def test_no_such_tool(self):
        for n in tool_names():
            for bad in self.FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(bad, n.lower(),
                                 "工具 %r 的名字含 %r，涉嫌文件外流能力" % (n, bad))

    def test_download_attachment_specifically_gone(self):
        self.assertNotIn("download_attachment", tool_names())

    def test_no_handler_left_behind(self):
        """只删声明不删处理器，等于留一条只差入口的通路。"""
        src = io.open("mcp_server.py", encoding="utf-8", errors="replace").read()
        self.assertNotIn('if name == "download_attachment"', src)

    def test_no_dest_dir_parameter_anywhere(self):
        """任何工具都不该收"把东西存到哪"这类参数。"""
        for t in m.TOOLS:
            props = ((t.get("inputSchema") or {}).get("properties") or {})
            for k in props:
                self.assertNotIn("dest", k.lower(),
                                 "工具 %s 有落盘目标参数 %s" % (t.get("name"), k))
                self.assertNotIn("path", k.lower(),
                                 "工具 %s 有路径参数 %s" % (t.get("name"), k))

    def test_no_local_filesystem_writes_in_handlers(self):
        """处理器里不该有往本地写文件的动作。"""
        src = io.open("mcp_server.py", encoding="utf-8", errors="replace").read()
        # 剥掉注释与 docstring 再查：注释里说明"刻意不做"不算违规
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                d = ast.get_docstring(node)
                if d:
                    src = src.replace(d, "")
        src = re.sub(r"#.*", "", src)
        for pat in (r"\.write_bytes\s*\(", r"open\s*\([^)]*['\"]wb['\"]",
                    r"\.mkdir\s*\(", r"shutil\.copy"):
            hits = [l.strip() for l in src.splitlines() if re.search(pat, l)]
            self.assertEqual(hits, [], "处理器里有本地写盘动作 %s: %s" % (pat, hits))


class TestOtherRedLines(unittest.TestCase):
    """顺带守住此前商定的另几条边界。"""

    def test_no_send_email_tool(self):
        """发送必须由人在界面上确认，MCP 不得直接外发。"""
        self.assertNotIn("send_email", tool_names())

    def test_no_scheduled_send(self):
        """定时发送会绕过"人确认"这道闸。"""
        self.assertNotIn("schedule_email", tool_names())

    def test_no_hard_delete(self):
        """删除不可逆，只提供软删除（trash）与还原。"""
        names = tool_names()
        self.assertNotIn("delete_email", names)
        self.assertIn("trash_email", names)
        self.assertIn("restore_email", names)

    def test_no_settings_mutation(self):
        self.assertNotIn("update_settings", tool_names())

    def test_create_draft_is_allowed(self):
        """写草稿是允许的：它不发送，只落库，发送仍需人确认。"""
        self.assertIn("create_draft", tool_names())

    def test_bulk_trash_capped_lower_than_bulk_mark(self):
        """误删代价更高，所以 bulk_trash 的上限要比 bulk_mark 低。"""
        lim = {}
        for t in m.TOOLS:
            if t.get("name") in ("bulk_trash", "bulk_mark"):
                ids = ((t.get("inputSchema") or {}).get("properties") or {}).get("ids") or {}
                lim[t["name"]] = ids.get("maxItems")
        self.assertTrue(lim.get("bulk_trash"), "bulk_trash 没有 maxItems")
        self.assertTrue(lim.get("bulk_mark"), "bulk_mark 没有 maxItems")
        self.assertLess(lim["bulk_trash"], lim["bulk_mark"])


class TestToolInventory(unittest.TestCase):
    def test_expected_tool_set(self):
        """工具清单是治理面，变动应当是有意识的 —— 这条会在增删工具时提醒你更新它。"""
        expected = {
            "bulk_mark", "bulk_trash", "create_draft", "create_todo", "get_email",
            "get_stats", "get_thread", "list_contacts", "list_drafts", "list_emails",
            "list_folders", "list_todos", "mark_email", "move_email",
            "restore_email", "search_emails", "sync_now", "trash_email",
        }
        got = set(tool_names())
        self.assertEqual(got, expected,
                         "工具清单变了：多了 %s，少了 %s。"
                         "若是有意新增，请评估治理影响后更新本测试。"
                         % (sorted(got - expected), sorted(expected - got)))

    def test_every_tool_has_description(self):
        for t in m.TOOLS:
            self.assertTrue((t.get("description") or "").strip(),
                            "工具 %s 没有描述" % t.get("name"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
