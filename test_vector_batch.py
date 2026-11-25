#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RemoteIndex 批量索引回归测试。

钉死 2026-09-07 修复的缺陷：
  daemon.py 固定用 RemoteIndex，而它没有 add_emails_batch，
  同步阶段 2 每轮抛 AttributeError 被 except 吞掉，
  循环仍打印「向量索引完成，共 N 封」——日志假报成功、实际零入库。

用法：
  python test_vector_batch.py            # 离线用例（不碰网络）
  python test_vector_batch.py --live     # 追加一条对本机 server 的真实 upsert
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import sys
import types

sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import daemon as D

# 测试进程不得写进 daemon.log：真实 daemon 正持有同一 RotatingFileHandler，
# 双进程写同一日志会挂住（实测 add_email 的 except 分支卡在 log.warning）。
# 换成静默 logger，既隔离副作用也让失败路径可测。
import logging as _logging

_quiet = _logging.getLogger("test_vector_batch.quiet")
_quiet.addHandler(_logging.NullHandler())
_quiet.propagate = False
D.log = _quiet

# 刻意不 import core.vector_store：它会拉起 sentence_transformers/torch，
# 在无控制台的 SSH 会话里既慢又有死锁风险（daemon 自身也正因此才用 RemoteIndex）。
# 接口对齐改用源码静态检查。
import ast
import io as _io

with _io.open(_os.path.join(ROOT, 'core', 'vector_store.py'), encoding="utf-8") as _f:
    _vs_tree = ast.parse(_f.read())
_VS_METHODS = {
    n.name
    for cls in _vs_tree.body
    if isinstance(cls, ast.ClassDef) and cls.name == "VectorStore"
    for n in cls.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, extra))
        FAILS.append(name)


print("=== 1. 接口存在性（缺陷主体）===")
check("RemoteIndex 有 add_emails_batch",
      hasattr(D.RemoteIndex, "add_emails_batch"))
check("与 VectorStore 接口同名对齐",
      "add_emails_batch" in _VS_METHODS, sorted(_VS_METHODS))
check("add_email 仍在", hasattr(D.RemoteIndex, "add_email"))

print("=== 2. duck typing 调用不再抛 AttributeError ===")
ri = D.RemoteIndex()
calls = []
ri.add_email = lambda e: (calls.append(e.get("id")), True)[1]
try:
    failed = ri.add_emails_batch([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    check("批量调用成功返回", failed == 0, "failed=%r" % (failed,))
    check("逐封都委托到 add_email", calls == ["a", "b", "c"], repr(calls))
except AttributeError as e:
    check("批量调用不抛 AttributeError", False, repr(e))

print("=== 3. 失败封数如实返回（不得假报成功）===")
ri2 = D.RemoteIndex()
ri2.add_email = lambda e: e.get("id") != "bad"
failed2 = ri2.add_emails_batch([{"id": "ok1"}, {"id": "bad"}, {"id": "bad"}])
check("2 封失败被计出", failed2 == 2, "failed=%r" % (failed2,))

print("=== 4. add_email 网络异常时返回 False 而非 None ===")
# 用 stub 制造异常，不打真实网络：Windows 上连不可达端口会挂住而非立刻拒绝。
import urllib.request as _ureq

_orig_urlopen = _ureq.urlopen


def _boom(*a, **kw):
    raise OSError("stubbed connection failure")


_ureq.urlopen = _boom
try:
    ri3 = D.RemoteIndex()
    r = ri3.add_email({"id": "x", "subject": "s", "body": "b"})
    check("连接失败返回 False", r is False, "got=%r" % (r,))
    check("失败也被批量计数",
          ri3.add_emails_batch([{"id": "x"}, {"id": "y"}]) == 2)
finally:
    _ureq.urlopen = _orig_urlopen

print("=== 5. 空批次 ===")
check("空列表返回 0", D.RemoteIndex().add_emails_batch([]) == 0)

if "--live" in sys.argv:
    print("=== 6. 真实 upsert（本机 server, loopback 免令牌）===")
    import sqlite3
    con = sqlite3.connect(_os.path.join(ROOT, 'data', 'emails.db'))
    row = con.execute(
        "select id, subject, body, sender, sender_email, time, folder,"
        " account, category, summary from emails"
        " where coalesce(deleted,0)=0 limit 2").fetchall()
    con.close()
    cols = ["id", "subject", "body", "sender", "sender_email", "time",
            "folder", "account", "category", "summary"]
    emails = [{k: (v if v is not None else "") for k, v in zip(cols, r)}
              for r in row]
    live = D.RemoteIndex()
    failed_live = live.add_emails_batch(emails)
    check("真实批量 upsert 零失败", failed_live == 0,
          "failed=%r" % (failed_live,))

print()
if FAILS:
    print("RESULT FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    raise SystemExit(1)
print("RESULT ALL_PASS")
