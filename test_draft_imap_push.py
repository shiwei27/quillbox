#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""草稿回写 IMAP：草稿箱识别、APPEND 组包、失败隔离、单向性。

核心判据（按重要性排序）：
1. **回写失败绝不影响草稿本身** —— 草稿箱能用比"手机能看到"重要得多；
2. 草稿箱识别优先认 RFC 6154 的 \\Drafts special-use 标志（唯一与语言无关的判据），
   认不到再按显示名，都找不到就跳过而**不建文件夹**；
3. 重推时先删旧副本，避免手机上堆一串同名草稿；
4. **单向**：只 local -> IMAP。同步侧 DEFAULT_SKIP_FOLDERS 含草稿箱，
   所以副本不会被拉回 emails 表——这是"不需要双向冲突规则"的依据，要钉住；
5. APPEND 用 \\Draft 标志、不是投递；密码不出现在返回值里。

全部用桩 IMAP 连接，不联网、不碰真实邮箱。

用法：python test_draft_imap_push.py
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import os
import sys
import tempfile
import unittest

sys.path.insert(0, ROOT)
from core import imap_extractor as ie  # noqa: E402
from core.db_manager import DBManager  # noqa: E402


class FakeConn:
    """桩 IMAP 连接：记录被调用的命令，可编程失败。"""

    def __init__(self, folders, append_ok=True, appenduid="42"):
        self._folders = folders
        self.append_ok = append_ok
        self.appenduid = appenduid
        self.calls = []
        self.appended = []

    def list(self, *a, **k):
        return ("OK", self._folders)

    def select(self, folder):
        self.calls.append(("select", folder))
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd) + tuple(str(x) for x in args if x is not None))
        if cmd == "SEARCH":
            return ("OK", [b"42"])
        return ("OK", [b""])

    def expunge(self):
        self.calls.append(("expunge",))
        return ("OK", [b""])

    def append(self, folder, flags, date, msg):
        self.appended.append((folder, flags, msg))
        if not self.append_ok:
            return ("NO", [b"quota exceeded"])
        return ("OK", [("[APPENDUID 1 %s] APPEND completed" % self.appenduid).encode()])

    def logout(self):
        self.calls.append(("logout",))
        return ("BYE", [b""])

    def close(self):
        pass


def make_ext(conn):
    # ImapExtractor 的必填参数是 host / username / account（account 是账户标识）
    ext = ie.ImapExtractor(host="imap.example.com", username="me@example.com",
                           account="me@example.com", port=993)
    ext._connect = lambda pw: conn
    return ext


# 服务端 LIST 应答：带 special-use 标志的与不带的两种
LIST_WITH_FLAG = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren \\Drafts) "/" "MyDraftBox"',
    b'(\\HasNoChildren) "/" "Sent"',
]
LIST_NAME_ONLY = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren) "/" "&g0l6P3ux-"',      # mutf7 的“草稿”
    b'(\\HasNoChildren) "/" "Drafts"',
]
LIST_NO_DRAFTS = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren) "/" "Sent"',
]


class DraftFolderTest(unittest.TestCase):

    def test_prefers_special_use_flag(self):
        """\\Drafts 标志与语言无关，必须优先于名字匹配。"""
        conn = FakeConn(LIST_WITH_FLAG)
        self.assertEqual(ie._draft_folder(make_ext(conn), conn), "MyDraftBox")

    def test_falls_back_to_name(self):
        conn = FakeConn(LIST_NAME_ONLY)
        got = ie._draft_folder(make_ext(conn), conn)
        self.assertIn(got, ("&g0l6P3ux-", "Drafts"))

    def test_none_when_absent(self):
        """找不到就返回 None，由调用方跳过——不自作主张建文件夹。"""
        conn = FakeConn(LIST_NO_DRAFTS)
        self.assertIsNone(ie._draft_folder(make_ext(conn), conn))


class PushTest(unittest.TestCase):

    def push(self, conn, **kw):
        kw.setdefault("subject", "主题")
        kw.setdefault("body", "正文")
        return ie.push_draft_to_imap(make_ext(conn), "pw", **kw)

    def test_appends_with_draft_flag(self):
        conn = FakeConn(LIST_WITH_FLAG)
        folder, uid = self.push(conn, to_addr="a@x.com")
        self.assertEqual(folder, "MyDraftBox")
        self.assertEqual(uid, "42")
        self.assertEqual(len(conn.appended), 1)
        _f, flags, raw = conn.appended[0]
        self.assertIn("Draft", flags)                 # 标为草稿，不是投递
        self.assertIn(b"Subject:", raw)
        self.assertIn(b"X-Quillbox-Draft", raw)

    def test_html_carries_both_parts(self):
        conn = FakeConn(LIST_WITH_FLAG)
        self.push(conn, body="<b>粗</b>", is_html=True)
        raw = conn.appended[0][2]
        self.assertIn(b"text/html", raw)

    def test_no_draft_folder_is_skipped_not_created(self):
        conn = FakeConn(LIST_NO_DRAFTS)
        self.assertEqual(self.push(conn), ("", ""))
        self.assertEqual(conn.appended, [], "没有草稿箱竟然还 APPEND 了")
        self.assertFalse(any(c[0] == "create" for c in conn.calls))

    def test_append_failure_returns_empty(self):
        conn = FakeConn(LIST_WITH_FLAG, append_ok=False)
        self.assertEqual(self.push(conn), ("", ""))

    def test_replace_deletes_old_copy_first(self):
        conn = FakeConn(LIST_WITH_FLAG)
        self.push(conn, drop_uid="7")
        store = [c for c in conn.calls if c[0] == "uid" and c[1] == "STORE"]
        self.assertTrue(store, "重推时没有删旧副本")
        self.assertIn("7", store[0])
        self.assertTrue(any(c[0] == "expunge" for c in conn.calls))
        self.assertEqual(len(conn.appended), 1)

    def test_delete_failure_does_not_block_append(self):
        """删不掉旧副本不该挡住新副本。"""
        conn = FakeConn(LIST_WITH_FLAG)

        def boom(cmd, *a):
            if cmd == "STORE":
                raise OSError("permission denied")
            return ("OK", [b"42"])
        conn.uid = boom
        folder, _uid = self.push(conn, drop_uid="7")
        self.assertEqual(folder, "MyDraftBox")
        self.assertEqual(len(conn.appended), 1)

    def test_always_logs_out(self):
        conn = FakeConn(LIST_NO_DRAFTS)
        self.push(conn)
        self.assertTrue(any(c[0] == "logout" for c in conn.calls), "没有登出")

    def test_password_not_in_return_value(self):
        conn = FakeConn(LIST_WITH_FLAG)
        self.assertNotIn("pw", "".join(str(x) for x in self.push(conn)))


class DraftColumnsTest(unittest.TestCase):
    """drafts 表的增量列要能在既有库上幂等生效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quillbox_dimap_")
        self.db = DBManager(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_columns_exist(self):
        did = self.db.add_draft(subject="s", body="b")
        d = self.db.get_draft(did)
        self.assertIn("imap_folder", d)
        self.assertIn("imap_uid", d)

    def test_columns_updatable_via_whitelist(self):
        did = self.db.add_draft(subject="s", body="b")
        self.assertTrue(self.db.update_draft(did, imap_folder="Drafts", imap_uid="9"))
        d = self.db.get_draft(did)
        self.assertEqual((d["imap_folder"], d["imap_uid"]), ("Drafts", "9"))

    def test_migration_is_idempotent(self):
        again = DBManager(os.path.join(self.tmp, "t.db"))
        self.assertEqual(again.count_drafts(), self.db.count_drafts())


class OneWayContractTest(unittest.TestCase):
    """单向性依据：同步侧必须跳过草稿箱，否则副本会被拉回来成重复邮件。"""

    def test_sync_skips_draft_folders(self):
        skip = {s.lower() for s in ie.DEFAULT_SKIP_FOLDERS}
        for name in ("drafts", "草稿", "草稿箱"):
            self.assertIn(name, skip,
                          "同步不跳过 %s，回写会产生重复邮件" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
