#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""草稿箱数据层离线测试：临时 sqlite，不碰生产库、不需要 server 在跑。

覆盖点选择的理由：
  * 建表要能在**已有库**上凭 IF NOT EXISTS 幂等生效（生产库不是空库）；
  * update_draft 的字段白名单要真的挡住 id/created_at，否则 PATCH 会被用来改主键；
  * 删除要回布尔，server 靠它区分 404 与成功；
  * 表结构必须与 emails 完全隔离——草稿不得进入邮件列表或 FTS 索引。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.db_manager import DBManager  # noqa: E402


class DraftsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quillbox_drafts_")
        self.path = os.path.join(self.tmp, "t.db")
        self.db = DBManager(self.path)

    def test_init_is_idempotent(self):
        """同一路径再建一次不应报错——生产库已存在时就是这个路径。"""
        again = DBManager(self.path)
        self.assertEqual(again.count_drafts(), 0)

    def test_crud_roundtrip(self):
        did = self.db.add_draft(subject="主题", body="正文",
                                to_addr="a@x.com", cc_addr="b@x.com",
                                is_html=True, source="agent")
        self.assertIsInstance(did, int)
        self.assertEqual(self.db.count_drafts(), 1)

        d = self.db.get_draft(did)
        self.assertEqual(d["subject"], "主题")
        self.assertEqual(d["to_addr"], "a@x.com")
        self.assertEqual(d["cc_addr"], "b@x.com")
        self.assertEqual(d["is_html"], 1)
        self.assertEqual(d["source"], "agent")
        self.assertIsNotNone(d["created_at"])

        self.assertTrue(self.db.update_draft(did, subject="改过", is_html=False))
        d2 = self.db.get_draft(did)
        self.assertEqual(d2["subject"], "改过")
        self.assertEqual(d2["is_html"], 0)
        self.assertGreaterEqual(d2["updated_at"], d["created_at"])

        self.assertTrue(self.db.delete_draft(did))
        self.assertIsNone(self.db.get_draft(did))
        self.assertEqual(self.db.count_drafts(), 0)

    def test_missing_id_is_falsey(self):
        """server 用返回值区分 404，不能靠异常。"""
        self.assertIsNone(self.db.get_draft(99999))
        self.assertFalse(self.db.delete_draft(99999))
        self.assertFalse(self.db.update_draft(99999, subject="x"))

    def test_update_field_whitelist(self):
        did = self.db.add_draft(subject="s", body="b")
        before = self.db.get_draft(did)
        # 不在白名单的键必须被丢弃；全是非法键时应返回 False 而不是改表
        self.assertFalse(self.db.update_draft(did, id=1234, created_at=0))
        after = self.db.get_draft(did)
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["created_at"], before["created_at"])

    def test_ordering_is_by_updated_desc(self):
        a = self.db.add_draft(subject="a")
        b = self.db.add_draft(subject="b")
        self.db.update_draft(a, subject="a2")   # a 后改，应排在前
        ids = [d["id"] for d in self.db.get_drafts()]
        self.assertEqual(ids[0], a)
        self.assertIn(b, ids)

    def test_drafts_do_not_leak_into_emails(self):
        """草稿是独立表：不进邮件列表、不进 FTS、不影响 count()。"""
        self.db.add_draft(subject="草稿主题", body="草稿正文")
        self.assertEqual(self.db.count(), 0)
        self.assertEqual(self.db.get_emails_paginated(page=0, page_size=50), [])
        self.assertEqual(self.db.search_by_keyword("草稿主题"), [])

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
