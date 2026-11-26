#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预先结构化分析：候选筛选（离线，桩 DB/配置）+ worker 调用形态（桩 urlopen）。不调 AI、不碰生产库。"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import json, sys, logging
sys.stdout.reconfigure(encoding="utf-8", errors="replace"); sys.path.insert(0, ROOT)
import daemon as D
_q = logging.getLogger("t.q"); _q.addHandler(logging.NullHandler()); _q.propagate = False; D.log = _q
fail = []
def check(label, got, want=True):
    ok = want(got) if callable(want) else got == want
    print("  %s %-44s got=%r" % ("ok  " if ok else "FAIL", label, got))
    if not ok: fail.append(label)
class FakeDB:
    def __init__(self, tagged=(), analyzed=()): self.tagged, self.analyzed = set(tagged), set(analyzed)
    def has_tag(self, i, t): return i in self.tagged
    def get_insight(self, i): return {"priority": "act"} if i in self.analyzed else None
P = {"skip_accounts": ["noc@example.org"], "skip_folders": ["系统告警"], "skip_sender_patterns": ["no-reply@"]}
def mk(i, **kw):
    e = {"id": i, "unread": True, "folder": "收件箱", "account": "me@x.com", "sender_email": "a@b.com", "received_timestamp": 100}
    e.update(kw); return e
mails = [mk("ok1", received_timestamp=300), mk("ok2", received_timestamp=200), mk("read", unread=False), mk("other", folder="告警通知"),
         mk("noc", account="noc@example.org"), mk("bot", sender_email="no-reply@x.com"), mk("tagged"), mk("done")]
D.load_config = lambda: {"ai": {}}
got = D._pre_insight_candidates(mails, FakeDB(tagged={"tagged"}, analyzed={"done"}), P)
check("只选：收件箱+未读+非跳过+未标记+未分析", got, ["ok1", "ok2"])
check("新到优先", got[0], "ok1")
D.load_config = lambda: {"ai": {"pre_insight_max": 1}}
check("上限生效", D._pre_insight_candidates(mails, FakeDB(), P), ["ok1"])
D.load_config = lambda: {"ai": {"pre_insight": False}}
check("开关关闭 → 不做", D._pre_insight_candidates(mails, FakeDB(), P), [])
D.load_config = lambda: {"ai": {"pre_insight_max": 0}}
check("上限 0 → 不做", D._pre_insight_candidates(mails, FakeDB(), P), [])
check("空输入", D._pre_insight_candidates([], FakeDB(), P), [])

import urllib.request
calls = []
class R:
    def __init__(self, ok): self.ok = ok
    def read(self): return json.dumps({"ok": self.ok}).encode()
def fake(req, timeout=None):
    calls.append((req.get_method(), req.full_url, timeout))
    if "bad" in req.full_url: raise OSError("boom")
    return R(True)
orig = urllib.request.urlopen; urllib.request.urlopen = fake
try:
    D._pre_insight_worker(["imap_a", "id/with/slash", "bad"])
finally:
    urllib.request.urlopen = orig
ins = [c for c in calls if c[1].endswith("/insight")]; cmp_ = [c for c in calls if c[1].endswith("/api/series/compare")]
check("每封一次 POST 到 server insight 端点", [c[0] for c in ins], ["POST"] * 3)
check("随后每封一次「与上期对比」（ai.pre_compare 缺省开启）", len(cmp_), 3)
check("id 做 URL 编码（含 /）", ins[1][1], "http://127.0.0.1:8765/api/emails/id%2Fwith%2Fslash/insight")
check("超时 ≥ 120s", ins[0][2], lambda t: t >= 120)
check("单封失败不影响后续（不抛异常）", True)
print()
if fail:
    print("RESULT FAILED(%d): %s" % (len(fail), ", ".join(fail))); sys.exit(1)
print("RESULT ALL_PASS")
