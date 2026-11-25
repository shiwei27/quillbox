#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""结构化 AI 分析（insight）离线回归：桩 AI + 临时库，不碰生产数据与网络。
用法：python test_insight.py [--live]   （--live 追加一次真实 server 端到端调用，会真正调一次 AI）
钉死：解析/归一化（含代码围栏、垃圾字段、非法 priority）、失败不缓存、缓存命中不重复调 AI、
正文变更后 stale、列表批量取优先级。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import json, os, sys, tempfile, shutil
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ROOT)
from core.ai_engine import AIEngine
from core.db_manager import DBManager

fail = []
def check(label, got, want=True):
    ok = want(got) if callable(want) else got == want
    print("  %s %-44s got=%r" % ("ok  " if ok else "FAIL", label, got if not isinstance(got, str) else got[:100]))
    if not ok: fail.append(label)

eng = AIEngine(provider="api", api_base_url="http://127.0.0.1:9/never", api_model="stub")
calls = []
def stub(text):
    def g(prompt, fmt=None, timeout=60):
        calls.append(prompt); return text
    return g
EMAIL = {"id": "e1", "subject": "AIDC 9 月报告", "body": "请周五前反馈。", "sender": "孙丽", "sender_email": "x@partner.cn", "account": "me@example.org"}

print("=== 1. 解析与归一化 ===")
eng._generate = stub('```json\n{"priority":"REPLY","summary":"  9 月月报已发出，周五前反馈 ","points":["可用率 99.95%","可用率 99.95%",3],'
                     '"todos":["周五前反馈"],"drafts":[{"tone":"确认收到","text":"你好，已收到。"},{"tone":"","text":"x"},"bad"],"extra":1}\n```')
r = eng.analyze_insight(EMAIL)
check("ok", r.get("ok"))
check("priority 小写归一", r.get("priority"), "reply")
check("summary 去首尾空白", r.get("summary"), "9 月月报已发出，周五前反馈")
check("points 去重并转字符串", r.get("points"), ["可用率 99.95%", "3"])
check("drafts 丢弃不合规项", r.get("drafts"), [{"tone": "确认收到", "text": "你好，已收到。"}])
check("多余字段被丢弃", "extra" in r, False)
check("prompt 带入发件人/主题/正文", ("孙丽" in calls[-1] and "AIDC 9 月报告" in calls[-1] and "请周五前反馈" in calls[-1]), True)
eng._generate = stub('{"priority":"urgent","summary":"x","drafts":[{"tone":"好","text":"y"}]}')
r = eng.analyze_insight(EMAIL)
check("非法 priority → fyi", r.get("priority"), "fyi")
check("fyi 不保留草稿", r.get("drafts"), [])
eng._generate = stub('前言：{"priority":"act","summary":"s","points":"not a list","todos":null}  后记')
r = eng.analyze_insight(EMAIL)
check("从夹杂文字中取出 JSON", (r.get("ok"), r.get("priority"), r.get("points"), r.get("todos")), (True, "act", [], []))

print("=== 2. 失败不伪装 ===")
eng._generate = stub("[API 异常: timed out]")
check("生成失败 → ok=False", eng.analyze_insight(EMAIL).get("ok"), False)
eng._generate = stub("我无法回答")
check("非 JSON → ok=False", eng.analyze_insight(EMAIL), {"ok": False, "error": "parse_failed"})

print("=== 3. 缓存表 ===")
td = tempfile.mkdtemp(prefix="test_insight_")
try:
    db = DBManager(os.path.join(td, "e.db"))
    for i in ("a", "b", "c"):
        db.upsert_email({"id": i, "subject": "s" + i, "body": "b", "sender": "x", "sender_email": "x@y.z", "received_timestamp": 1790000000, "folder": "收件箱", "account": "acc"})
    check("未分析 → None", db.get_insight("a"), None)
    db.save_insight("a", {"priority": "act", "summary": "s", "points": ["p"], "todos": ["t"], "drafts": [{"tone": "t", "text": "x"}]}, body_hash="h1", model="m")
    g = db.get_insight("a")
    check("读回 JSON 字段", (g["priority"], g["points"], g["drafts"][0]["tone"], g["body_hash"]), ("act", ["p"], "t", "h1"))
    db.save_insight("a", {"priority": "reply", "summary": "s2"}, body_hash="h2")
    check("upsert 覆盖", (db.get_insight("a")["priority"], db.get_insight("a")["points"]), ("reply", []))
    db.save_insight("c", {"priority": "notice"})
    check("批量取优先级（未分析的不出现）", db.get_insight_priorities(["a", "b", "c", ""]), {"a": "reply", "c": "notice"})
    check("空列表", db.get_insight_priorities([]), {})
    db._get_conn().close()
finally:
    shutil.rmtree(td, ignore_errors=True)

if "--live" in sys.argv:
    print("=== 4. 真实 server 端到端（会调用一次 AI）===")
    import urllib.request
    B = "http://127.0.0.1:8765"
    def call(p, m="GET"):
        return json.load(urllib.request.urlopen(urllib.request.Request(B + p, method=m), timeout=120))
    eid = call("/api/emails?page_size=1&light=1")["emails"][0]["id"]
    r1 = call("/api/emails/%s/insight" % eid, "POST")
    check("POST 生成成功", r1.get("ok"), True)
    ins = r1.get("insight") or {}
    check("字段齐全", sorted(k for k in ins if k in ("priority", "summary", "points", "todos", "drafts")), ["drafts", "points", "priority", "summary", "todos"])
    check("priority 合法", ins.get("priority"), lambda p: p in ("act", "reply", "notice", "fyi"))
    r2 = call("/api/emails/%s/insight" % eid, "POST")
    check("再次 POST 命中缓存", r2.get("cached"), True)
    check("GET 读缓存", (call("/api/emails/%s/insight" % eid).get("insight") or {}).get("priority"), ins.get("priority"))
    rows = call("/api/emails?page_size=5&light=1")["emails"]
    check("列表行带 ai_priority", next((x.get("ai_priority") for x in rows if x["id"] == eid), None), ins.get("priority"))

print()
if fail:
    print("RESULT FAILED(%d): %s" % (len(fail), ", ".join(fail))); sys.exit(1)
print("RESULT ALL_PASS")
