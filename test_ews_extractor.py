#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EWS 取件后端的离线验收（不连网，可进回归套件）。

真实连接的验收在 _dry_ews.py（要凭据、要内网），这里只测能离线测的部分。
其中最有价值的两条：

* **字段集合必须与 IMAP 后端逐键相同** —— 三条后端产出同构记录是这个项目的
  既有约定，漂移了前端和 AI 都会出错，而这种错很隐蔽。
* **源码级静态守卫：模块里不得出现任何改服务端状态的调用** ——
  "绝不改服务端状态"是硬约束（这个邮箱有 836 封未读，被读一遍抹掉就找不回来了），
  但它平时不会自己暴露。用静态断言把它钉住，比指望人记得更可靠。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import io
import os
import re
import sys
import tempfile
import unittest

os.chdir(ROOT)
sys.path.insert(0, ROOT)

from core import ews_extractor as ews          # noqa: E402
from core.ews_extractor import EwsExtractor, from_config, OVERLAP_SECONDS  # noqa: E402

RAW = (b"From: =?utf-8?B?5byg5LiJ?= <zhang@example.org>\r\n"
       b"To: alice@example.org\r\n"
       b"Subject: =?utf-8?B?5rWL6K+V5Li76aKY?=\r\n"
       b"Message-ID: <ABC-123@example.org>\r\n"
       b"Date: Wed, 17 Sep 2026 10:30:00 +0800\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
       b"\xe6\xad\xa3\xe6\x96\x87\xe5\x86\x85\xe5\xae\xb9\r\n")

CFG = {"ews": {"enabled": True, "accounts": {"alice@example.org": {
    "host": "mail.example.org", "username": "EXAMPLE\\alice",
    "verify_tls": False, "timeout": 45, "initial_days": None,
    "max_per_folder": 1500}}}}


def _read(path):
    """读文件并确保关闭 —— 不用 with 会刷 ResourceWarning，把测试输出弄脏。"""
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def mk(**kw):
    d = dict(host="h", username="u", account="alice@example.org")
    d.update(kw)
    return EwsExtractor(**d)


class Folder:
    """够用的文件夹替身。"""
    def __init__(self, name, total=1, cc="IPF.Note", fid="x"):
        self.name = name
        self.total_count = total
        self.CONTAINER_CLASS = cc
        self.id = fid


class Root:
    def __init__(self, kids):
        self._k = kids

    def walk(self):
        return list(self._k)


class Acct:
    def __init__(self, kids, inbox=None):
        self.msg_folder_root = Root(kids)
        self.inbox = inbox


class TestRecordShape(unittest.TestCase):
    """字段结构：与另几条后端同构。"""

    def test_key_set_matches_imap(self):
        """EWS 记录的键集合必须与 IMAP 后端逐键相同。"""
        from core.imap_extractor import ImapExtractor
        imap_keys = set(re.findall(
            r'^\s{12}"(\w+)":',
            self._body(_read("core/imap_extractor.py"),
                       "_build_record"), re.M))
        ews_keys = set(re.findall(
            r'^\s{12}"(\w+)":',
            self._body(_read("core/ews_extractor.py"),
                       "_build_record"), re.M))
        self.assertTrue(imap_keys, "没解析出 IMAP 的字段，测试本身有问题")
        self.assertEqual(imap_keys, ews_keys,
                         "字段漂移！仅 IMAP 有=%s 仅 EWS 有=%s"
                         % (sorted(imap_keys - ews_keys), sorted(ews_keys - imap_keys)))

    @staticmethod
    def _body(src, fn):
        i = src.find("def %s" % fn)
        j = src.find("\n    def ", i + 10)
        return src[i:j if j > 0 else i + 3000]

    def test_build_record_from_real_mime(self):
        e = mk()
        r = e._build_record(RAW, "itemkey", "收件箱", True)
        self.assertIsNotNone(r)
        self.assertEqual(r["subject"], "测试主题")
        self.assertEqual(r["sender_email"], "zhang@example.org")
        self.assertEqual(r["sender"], "张三")
        # message_id 被刻意归一化（去尖括号 + 转小写）——
        # 跨后端按 Message-ID 去重就是靠这个归一化才对得上
        self.assertEqual(r["message_id"], "abc-123@example.org")
        self.assertEqual(r["folder"], "收件箱")
        self.assertEqual(r["account"], "alice@example.org")
        self.assertTrue(r["unread"])
        self.assertFalse(r["has_attachment"])
        self.assertIn("正文", r["body"])
        self.assertTrue(r["received_timestamp"] > 0)

    def test_unidentifiable_mime_is_dropped(self):
        """垃圾报文要丢掉，不能进库。

        注意 email.message_from_bytes **不会抛异常** —— 它对垃圾字节很宽容，
        会产出一个 message_id 和日期都为空的空壳。这种记录既无法跨后端去重、
        又会在界面上显示成 1970 年，所以按"无 message_id 且无日期"显式丢弃。
        """
        self.assertIsNone(mk()._build_record(b"\x00\x01 not mime", "k", "收件箱", False))

    def test_keeps_record_without_message_id_if_dated(self):
        """只是缺 message_id 但有日期的信仍要收 —— 用 fallback id，不能丢邮件。"""
        raw = (b"From: a@b.com\r\nSubject: x\r\n"
               b"Date: Wed, 17 Sep 2026 10:30:00 +0800\r\n\r\nbody\r\n")
        r = mk()._build_record(raw, "k1", "收件箱", False)
        self.assertIsNotNone(r)
        self.assertTrue(r["id"].startswith("ews_"))
        self.assertTrue(r["received_timestamp"] > 0)


class TestRecordId(unittest.TestCase):
    def test_prefix(self):
        self.assertTrue(mk()._record_id("<a@b>", "f").startswith("ews_"))

    def test_normalizes_angle_brackets_and_case(self):
        """同一封信的 message_id 大小写/尖括号差异不该产生两个 id。"""
        e = mk()
        self.assertEqual(e._record_id("<A@B.COM>", "x"), e._record_id("a@b.com", "y"))

    def test_account_scoped(self):
        """不同账户下同一封信是不同记录（各自邮箱各存一份）。"""
        self.assertNotEqual(mk()._record_id("<a@b>", "f"),
                            mk(account="noc@example.org")._record_id("<a@b>", "f"))

    def test_falls_back_when_no_message_id(self):
        e = mk()
        self.assertNotEqual(e._record_id("", "收件箱:1"), e._record_id("", "收件箱:2"))

    def test_digest_matches_imap_rule(self):
        """摘要算法必须与 IMAP 一致，只有前缀不同 —— 这样跨后端才好比对。"""
        import hashlib
        e = mk()
        want = hashlib.sha1(("alice@example.org|a@b.com").encode("utf-8")).hexdigest()
        self.assertEqual(e._record_id("<a@b.com>", "x"), "ews_" + want)


class TestWatermark(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = ews.STATE_PATH
        ews.STATE_PATH = __import__("pathlib").Path(self.tmp) / "s.json"

    def tearDown(self):
        ews.STATE_PATH = self._orig

    def test_no_state_initially(self):
        self.assertFalse(mk().has_state())
        self.assertIsNone(mk().folder_watermark("收件箱"))

    def test_commit_persists_and_fetch_stages_only(self):
        e = mk()
        e._pending["收件箱"] = 1000.0
        self.assertFalse(ews.STATE_PATH.exists(), "commit 前不该落盘")
        self.assertEqual(e.commit_state(), 1)
        self.assertTrue(ews.STATE_PATH.exists())
        self.assertEqual(mk().folder_watermark("收件箱"), 1000.0)
        self.assertEqual(e._pending, {}, "提交后应清空暂存")

    def test_commit_only_moves_forward(self):
        """重跑历史窗口不该把水位拉回去，否则会反复重取。"""
        e = mk()
        e._pending["收件箱"] = 2000.0
        e.commit_state()
        e._pending["收件箱"] = 500.0
        e.commit_state()
        self.assertEqual(mk().folder_watermark("收件箱"), 2000.0)

    def test_commit_noop_when_nothing_pending(self):
        self.assertEqual(mk().commit_state(), 0)

    def test_accounts_isolated(self):
        e = mk()
        e._pending["收件箱"] = 900.0
        e.commit_state()
        self.assertIsNone(mk(account="noc@example.org").folder_watermark("收件箱"))

    def test_overlap_window_positive(self):
        """重叠窗口必须存在：宁可重复取，不可漏。"""
        self.assertGreater(OVERLAP_SECONDS, 0)


class TestFolderFilter(unittest.TestCase):
    def test_skips_system_and_non_mail(self):
        e = mk()
        kids = [Folder("收件箱", 5), Folder("项目", 3),
                Folder("{06967759-274D}", 6),          # 隐藏系统文件夹
                Folder("Recipient Cache", 44),          # 收件人缓存
                Folder("日历", 4, cc="IPF.Appointment"),  # 非邮件容器
                Folder("联系人", 5, cc="IPF.Contact"),
                Folder("已删除邮件", 72),                 # 默认跳过项
                Folder("空文件夹", 0)]                    # 没邮件
        names = [f.name for f in e._folders(Acct(kids))]
        self.assertEqual(names, ["收件箱", "项目"])

    def test_respects_config_skip(self):
        e = mk(skip_folders=["项目"])
        names = [f.name for f in e._folders(Acct([Folder("收件箱"), Folder("项目")]))]
        self.assertEqual(names, ["收件箱"])

    def test_inbox_label_normalized(self):
        """收件箱统一成 INBOX_NAME，避免同一概念两种写法。"""
        from core.imap_extractor import INBOX_NAME
        e = mk()
        inbox = Folder("Inbox", 5, fid="INBOXID")
        self.assertEqual(e._folder_label(Acct([inbox], inbox=inbox), inbox), INBOX_NAME)

    def test_other_folder_keeps_server_name(self):
        e = mk()
        f = Folder("变更通知", 3, fid="F2")
        inbox = Folder("Inbox", 1, fid="F1")
        self.assertEqual(e._folder_label(Acct([f], inbox=inbox), f), "变更通知")


class TestConfig(unittest.TestCase):
    def test_from_config(self):
        e = from_config(CFG, "alice@example.org")
        self.assertEqual(e.host, "mail.example.org")
        self.assertEqual(e.username, "EXAMPLE\\alice")
        self.assertEqual(e.account, "alice@example.org")
        self.assertEqual(e.timeout, 45)
        self.assertEqual(e.max_per_folder, 1500)
        self.assertFalse(e.verify_tls)

    def test_missing_host_raises(self):
        with self.assertRaises(ValueError):
            from_config({"ews": {"accounts": {"a@b.com": {}}}}, "a@b.com")

    def test_interface_matches_imap_backend(self):
        """daemon 用同一套调用，方法名必须对得上。"""
        from core.imap_extractor import ImapExtractor
        for name in ("fetch", "commit_state", "has_state", "check_login",
                     "get_all_emails", "get_incremental_emails"):
            self.assertTrue(hasattr(EwsExtractor, name), "EWS 缺 %s" % name)
            self.assertTrue(hasattr(ImapExtractor, name), "IMAP 缺 %s" % name)

    def test_inline_root_set(self):
        """复用的 _save_inline 依赖 inline_root，漏设会在取带内嵌图的信时才炸。"""
        self.assertTrue(getattr(mk(), "inline_root", None))


class TestNeverMutatesServer(unittest.TestCase):
    """静态守卫：模块里不得出现任何会改服务端状态的调用。

    这个邮箱有 836 封未读。IMAP 用 RFC822 取信会隐式打已读标记，是这类系统
    最容易踩的坑；EWS 不会隐式改，但只要有人写了 item.save() 就会真的改。
    靠人记不住，用断言钉住。
    """

    FORBIDDEN = [
        (r"\.save\s*\(", "item.save() 会写回服务端"),
        (r"\.move\s*\(", "移动邮件"),
        (r"\.delete\s*\(", "删除邮件"),
        (r"\.soft_delete\s*\(", "软删除"),
        (r"\.move_to_trash\s*\(", "移入回收站"),
        (r"is_read\s*=\s*(?!=)", "写 is_read 会抹掉未读状态"),
        (r"\.mark_as_junk\s*\(", "标记垃圾"),
        (r"\.archive\s*\(", "归档"),
        (r"\.send\s*\(", "取件模块不该发信"),
    ]

    def setUp(self):
        src = _read("core/ews_extractor.py")
        # 剥掉注释与 docstring：注释里提到这些词是说明"刻意不做"，不算违规。
        # （这个项目已经因为裸字符串统计误报过多次。）
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                d = ast.get_docstring(node)
                if d:
                    src = src.replace(d, "")
        src = re.sub(r"#.*", "", src)
        self.code = src

    def test_no_mutating_calls(self):
        for pat, why in self.FORBIDDEN:
            hits = [l.strip() for l in self.code.splitlines() if re.search(pat, l)]
            self.assertEqual(hits, [], "出现改服务端状态的调用（%s）: %s" % (why, hits))

    def test_reads_is_read_but_never_assigns(self):
        self.assertIn("is_read", self.code, "应该读 is_read 来带出未读状态")
        self.assertNotRegex(self.code, r"is_read\s*=\s*(?!=)")

    def test_uses_failfast(self):
        """必须 FailFast：默认重试策略会把一次错误密码放大成多次尝试，可能锁账户。"""
        self.assertIn("FailFast", self.code)

    def test_only_fetches_needed_fields(self):
        """mime_content 很重，必须用 only() 限定字段，否则整箱正文全拉一遍。"""
        self.assertIn(".only(", self.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
