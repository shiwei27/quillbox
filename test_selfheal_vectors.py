#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""向量自愈改造的离线回归（只用临时库，不碰生产数据/网络/日志）。

用法：python test_selfheal_vectors.py [daemon 模块路径]
默认测同目录的 daemon.py；验证候选时传候选文件路径。
钉死的不变量：
  1. 补的邮件集合与顺序 == 旧逻辑（get_emails_paginated 默认过滤）的结果；
  2. 不再调用全量 get_emails_paginated；
  3. SELFHEAL_LIMIT 生效、缺口为 0 时不调用索引；
  4. 交给索引的是整行（含 body），已删除的不补，归档的照补（与旧口径一致）。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import importlib.util, logging, os, sqlite3, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ROOT)
MOD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "daemon.py")

spec = importlib.util.spec_from_file_location("daemon_under_test", MOD)
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)
_q = logging.getLogger("test_selfheal.quiet"); _q.addHandler(logging.NullHandler()); _q.propagate = False
D.log = _q   # 不写 daemon.log（真实 daemon 持有同一 handler）
from core.db_manager import DBManager

FAILS = []
def check(name, cond, extra=""):
    print(("  ok   %s" if cond else "  FAIL %s " + str(extra)) % name)
    if not cond: FAILS.append(name)

class SpyIndex:
    def __init__(self): self.calls = []
    def add_emails_batch(self, emails):
        self.calls.append(list(emails)); return 0

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    dbp = os.path.join(td, "emails.db")
    db = DBManager(dbp)
    base = 1790000000.0
    for i, k in enumerate("abcdef"):
        db.upsert_email({"id": k, "subject": "s-" + k, "body": "body-" + k,
                         "sender": "x", "sender_email": "x@example.com",
                         "received_timestamp": base - i * 60, "folder": "收件箱",
                         "account": "acc"})
    con = sqlite3.connect(dbp)
    con.execute("UPDATE emails SET deleted=1 WHERE id='b'")
    con.execute("UPDATE emails SET archived=1 WHERE id='d'")
    con.commit(); con.close()

    indexed = {"a"}
    D._chroma_indexed_ids = lambda path: set(indexed)
    p = {"chroma_path": td, "db_path": dbp}

    # 旧口径基线：在打桩前用真实方法算
    old_missing = [e["id"] for e in db.get_emails_paginated(page=0, page_size=100000)
                   if str(e["id"]) not in indexed]

    def _forbid(*a, **k): raise AssertionError("不应再全量调用 get_emails_paginated")
    db.get_emails_paginated = _forbid

    print("=== 1. 与旧口径同集合同顺序 ===")
    vs = SpyIndex(); D._selfheal_vectors(p, db, vs)
    got = [e["id"] for e in vs.calls[0]] if vs.calls else None
    check("补的 id 与旧逻辑一致", got == old_missing, "got=%r old=%r" % (got, old_missing))
    check("新邮件优先（按时间倒序）", got == ["c", "d", "e", "f"], got)
    check("已删除的 b 不补", got is not None and "b" not in got)
    check("归档的 d 照补（旧口径不过滤归档）", got is not None and "d" in got)
    check("交给索引的是整行（含 body）",
          bool(vs.calls) and all(e.get("body") == "body-" + e["id"] for e in vs.calls[0]))

    print("=== 2. SELFHEAL_LIMIT 生效 ===")
    old_limit = D.SELFHEAL_LIMIT; D.SELFHEAL_LIMIT = 2
    vs = SpyIndex(); D._selfheal_vectors(p, db, vs)
    check("只补 2 封", vs.calls and [e["id"] for e in vs.calls[0]] == ["c", "d"],
          vs.calls and [e["id"] for e in vs.calls[0]])
    D.SELFHEAL_LIMIT = old_limit

    print("=== 3. 缺口为 0 不调用索引 ===")
    indexed.update({"c", "d", "e", "f"})
    vs = SpyIndex(); D._selfheal_vectors(p, db, vs)
    check("无调用", vs.calls == [], vs.calls)

    print("=== 4. 读不到 chroma（未启用语义检索）静默跳过 ===")
    D._chroma_indexed_ids = lambda path: set()
    vs = SpyIndex(); D._selfheal_vectors(p, db, vs)
    check("无调用", vs.calls == [])

    print("=== 5. 求差集后邮件被删：跳过，不传 None ===")
    D._chroma_indexed_ids = lambda path: {"a"}
    real_get = DBManager.get_email_by_id
    db.get_email_by_id = lambda i: None if i == "c" else real_get(db, i)
    vs = SpyIndex(); D._selfheal_vectors(p, db, vs)
    check("c 被跳过且无 None", vs.calls and [e["id"] for e in vs.calls[0]] == ["d", "e", "f"],
          vs.calls and [e and e["id"] for e in vs.calls[0]])

print()
if FAILS:
    print("RESULT FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS))); raise SystemExit(1)
print("RESULT ALL_PASS")
