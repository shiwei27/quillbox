#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""vector_store 安全性：不许静默删索引、不许把错误文本当语义内容。

最关键的一条是「非冲突异常不得触发重建」——改前的
`except Exception: self.client.delete_collection("emails")`
会让一次 sqlite busy 或文件锁就清掉 13000 条 / 140 MB 索引，
而按自愈 200 封每 5 分钟一轮算，重建要 5 个多小时，且一行日志都没有。

用桩替掉 chromadb 客户端与嵌入函数，避免加载 768 维模型（否则每次跑要几分钟）。

用法：python test_vector_store_safety.py
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import sqlite3
import sys
import unittest

sys.path.insert(0, ROOT)
from core import vector_store as vs_mod  # noqa: E402


class FakeCollection:
    def __init__(self, name="emails"):
        self.name = name
        self.queries = 0

    def query(self, **kwargs):
        self.queries += 1
        return {"ids": [["a"]], "metadatas": [[{"subject": "s"}]], "distances": [[0.25]]}


class FakeClient:
    """可编程的 chroma 客户端桩：控制 get_or_create 抛什么异常。"""

    def __init__(self, raise_on_get=None):
        self.raise_on_get = raise_on_get
        self.deleted = []
        self.created = []

    def get_or_create_collection(self, **kw):
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return FakeCollection()

    def delete_collection(self, name):
        self.deleted.append(name)

    def create_collection(self, **kw):
        self.created.append(kw.get("name"))
        return FakeCollection()


def build(raise_on_get=None):
    """构造 VectorStore，全程不碰真实 chroma 与嵌入模型。"""
    client = FakeClient(raise_on_get)
    orig_pc = vs_mod.chromadb.PersistentClient
    orig_ef = vs_mod.SentenceTransformerEmbeddingFunction
    vs_mod.chromadb.PersistentClient = lambda **kw: client
    vs_mod.SentenceTransformerEmbeddingFunction = lambda **kw: object()
    try:
        store = vs_mod.VectorStore("dummy_path", "dummy_model")
        return store, client
    finally:
        vs_mod.chromadb.PersistentClient = orig_pc
        vs_mod.SentenceTransformerEmbeddingFunction = orig_ef


class ConflictJudgeTest(unittest.TestCase):

    def test_conflict_messages(self):
        for e in (ValueError("Embedding function conflict"),
                  ValueError("dimension mismatch: 384 != 768"),
                  RuntimeError("embedding_function does not match")):
            self.assertTrue(vs_mod._is_embedding_conflict(e), e)

    def test_transient_errors_are_not_conflicts(self):
        """这些必须判为"非冲突"，否则又会去删库。"""
        for e in (sqlite3.OperationalError("database is locked"),
                  OSError("[WinError 32] 另一个程序正在使用此文件"),
                  TimeoutError("timed out"),
                  MemoryError()):
            self.assertFalse(vs_mod._is_embedding_conflict(e), e)


class InitTest(unittest.TestCase):

    def test_normal_open(self):
        store, client = build()
        self.assertEqual(client.deleted, [])
        self.assertIsNotNone(store.collection)

    def test_conflict_triggers_rebuild(self):
        store, client = build(ValueError("Embedding function conflict"))
        self.assertEqual(client.deleted, ["emails"])
        self.assertEqual(client.created, ["emails"])

    def test_transient_error_must_not_delete(self):
        """核心回归：非冲突异常必须抛出，且绝不删集合。"""
        with self.assertRaises(sqlite3.OperationalError):
            build(sqlite3.OperationalError("database is locked"))

    def test_transient_error_leaves_collection_untouched(self):
        client = FakeClient(sqlite3.OperationalError("database is locked"))
        orig_pc = vs_mod.chromadb.PersistentClient
        orig_ef = vs_mod.SentenceTransformerEmbeddingFunction
        vs_mod.chromadb.PersistentClient = lambda **kw: client
        vs_mod.SentenceTransformerEmbeddingFunction = lambda **kw: object()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                vs_mod.VectorStore("dummy", "dummy")
        finally:
            vs_mod.chromadb.PersistentClient = orig_pc
            vs_mod.SentenceTransformerEmbeddingFunction = orig_ef
        self.assertEqual(client.deleted, [], "非冲突异常竟然删了集合")


class UnusableSummaryTest(unittest.TestCase):

    def test_ai_error_strings_blocked(self):
        for s in ("[API 异常: Connection refused]", "[Ollama 异常: timeout]",
                  '{"error": "boom"}', "AI 分析失败", "摘要生成失败", "", "   "):
            self.assertTrue(vs_mod._unusable_summary(s), s)

    def test_normal_summary_allowed(self):
        for s in ("刘总批准合同续签，需在本周内回复确认。", "会议一小时后开始"):
            self.assertFalse(vs_mod._unusable_summary(s), s)

    def test_api_error_was_the_gap(self):
        """改前判据只挡 [Ollama，当前后端的 [API 异常 是漏过去的那一类。"""
        self.assertTrue(vs_mod._unusable_summary("[API 异常: x]"))


class BuildIndexTextTest(unittest.TestCase):

    def setUp(self):
        self.store, _ = build()

    def test_uses_summary_when_good(self):
        t = self.store._build_index_text(
            {"subject": "主题", "summary": "正常摘要内容", "body": "正文" * 100})
        self.assertIn("正常摘要内容", t)
        self.assertNotIn("正文正文", t)

    def test_falls_back_when_summary_unusable(self):
        t = self.store._build_index_text(
            {"subject": "主题", "summary": "[API 异常: x]", "body": "真正的正文"})
        self.assertIn("真正的正文", t)
        self.assertNotIn("API 异常", t, "错误文本进了索引文本")

    def test_falls_back_when_no_summary(self):
        t = self.store._build_index_text({"subject": "主题", "summary": "", "body": "正文"})
        self.assertIn("正文", t)


class SearchTest(unittest.TestCase):

    def test_normal_search(self):
        store, _ = build()
        r = store.search("查询")
        self.assertEqual(r[0]["id"], "a")
        self.assertAlmostEqual(r[0]["score"], 0.75)

    def test_query_failure_returns_empty_and_does_not_raise(self):
        store, _ = build()

        def boom(**kw):
            raise RuntimeError("hnsw segment corrupt")
        store.collection.query = boom
        self.assertEqual(store.search("查询"), [])       # 不抛，但已记 warning

    def test_chat_search_shares_the_path(self):
        store, _ = build()
        self.assertEqual(len(store.chat_search("查询")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
