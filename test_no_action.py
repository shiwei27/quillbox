#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「无需处理」离线回归（桩配置 + 临时库，不写 config.yaml、不碰生产库）。用法：python test_no_action.py [--live]
--live：对真实 server 只做只读预览 + 单封打标签再还原（不新增规则、不改 config）。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import json, os, sys, tempfile, shutil
sys.stdout.reconfigure(encoding="utf-8", errors="replace"); sys.path.insert(0, ROOT)
from core.ai_engine import AIEngine
from core.db_manager import DBManager
fail = []
def check(label, got, want=True):
    ok = want(got) if callable(want) else got == want
    print("  %s %-46s got=%r" % ("ok  " if ok else "FAIL", label, got))
    if not ok: fail.append(label)

print("=== 1. 同类主题模板 ===")
P = AIEngine.subject_pattern
check("日期归一", P("示例集群巡检报告 - 2026-10-09") == P("示例集群巡检报告 - 2026-10-10"), True)
check("去回复/转发前缀", P("回复: Re:示例集群巡检报告 - 2026-10-09"), P("示例集群巡检报告 - 2026-10-11"))
check("不同主题不相等", P("示例云ECS续费成功提醒") == P("【示例云配额中心】配额告警"), False)
check("数字个数不同仍同类", P("您有3块云盘") == P("您有12块云盘"), True)

print("=== 2. 规则判定：no_action 独立于首个命中链 ===")
rules = [
    {"name": "示例云告警转移", "enabled": True, "match_mode": "all", "conditions": [{"field": "from", "op": "contains", "value": "@notice.examplecloud.example"}],
     "actions": {"folder": "云告警"}},
    {"name": "无需处理 · 示例云 · 续费", "enabled": True, "match_mode": "all",
     "conditions": [{"field": "from", "op": "is", "value": "system@notice.examplecloud.example"}, {"field": "subject", "op": "pattern_is", "value": P("示例云ECS续费成功提醒")}],
     "actions": {"no_action": True}},
    {"name": "停用的", "enabled": False, "match_mode": "all", "conditions": [{"field": "from", "op": "is", "value": "x@y.z"}], "actions": {"no_action": True}},
]
eng = AIEngine(provider="api", prompts={"rules": rules})
e_renew = {"sender": "示例云", "sender_email": "system@notice.examplecloud.example", "subject": "示例云ECS续费成功提醒"}
e_quota = {"sender": "示例云", "sender_email": "system@notice.examplecloud.example", "subject": "【示例云配额中心】配额告警"}
check("续费：命中无需处理", (eng.match_no_action(e_renew) or {}).get("name"), "无需处理 · 示例云 · 续费")
check("续费：移动规则仍然命中（不被挡）", (eng._match_rule(e_renew) or {}).get("name"), "示例云告警转移")
check("配额告警：不命中无需处理", eng.match_no_action(e_quota), None)
check("停用规则不生效", eng.match_no_action({"sender_email": "x@y.z", "subject": "s"}), None)
check("no_action 规则不进入首个命中链", (AIEngine(provider="api", prompts={"rules": rules[1:2]})._match_rule(e_renew)), None)
check("pattern_is 在 RULE_OPS 中（AI 生成规则不被剔除）", "pattern_is" in AIEngine.RULE_OPS, True)

print("=== 3. 标签 ===")
td = tempfile.mkdtemp(prefix="test_noact_")
try:
    db = DBManager(os.path.join(td, "e.db"))
    db.upsert_email({"id": "a", "subject": "s", "body": "b", "sender": "x", "sender_email": "x@y.z", "received_timestamp": 1790000000, "folder": "收件箱", "account": "acc"})
    check("首次打标签 → True", db.add_tag("a", "无需处理"), True)
    check("重复打标签幂等 → False", db.add_tag("a", "无需处理"), False)
    check("has_tag", db.has_tag("a", "无需处理"), True)
    check("tags 只有一份", db.get_email_by_id("a")["tags"], ["无需处理"])
    check("不存在的邮件 → False", db.add_tag("zz", "无需处理"), False)
    db._get_conn().close()
finally:
    shutil.rmtree(td, ignore_errors=True)

if "--live" in sys.argv:
    print("=== 4. 真实 server（只读预览 + 单封打标签后还原）===")
    import urllib.request
    B = "http://127.0.0.1:8765"
    def call(p, m="GET", body=None):
        r = urllib.request.Request(B + p, method=m, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=120))
    e = next(x for x in call("/api/emails?page_size=40&light=1")["emails"] if "notice.examplecloud.example" in (x.get("sender_email") or ""))
    pv = call("/api/no-action/preview?scope=same_kind&email_id=" + e["id"])
    check("预览：同类 ≥ 1 封", pv.get("matched"), lambda n: isinstance(n, int) and n >= 1)
    pv2 = call("/api/no-action/preview?scope=sender&email_id=" + e["id"])
    check("预览：同发件人 ≥ 同类", pv2.get("matched", 0) >= pv.get("matched", 0), True)
    cfg_before = call("/api/rules")
    tags0 = call("/api/emails/" + e["id"]).get("tags") or []
    r = call("/api/no-action", "POST", {"email_id": e["id"], "scope": "single"})
    check("单封：打上标签", "无需处理" in (call("/api/emails/" + e["id"]).get("tags") or []), True)
    check("单封：不新增规则", call("/api/rules"), cfg_before)
    ins = call("/api/emails/%s/insight" % e["id"]).get("insight")
    check("insight 输出：仅供阅读、无待办无草稿", (ins or {"priority": "fyi", "todos": [], "drafts": []}) and ((ins or {}).get("priority", "fyi"), (ins or {}).get("todos", []), (ins or {}).get("drafts", [])), ("fyi", [], []))
    x = call("/api/emails/%s/extract-todos" % e["id"], "POST")
    check("提取待办被跳过", (x.get("ok"), x.get("source"), x.get("count")), (True, "no_action", 0))
    rows = call("/api/emails?page_size=40&light=1")["emails"]
    check("列表 ai_priority = fyi", next((y.get("ai_priority") for y in rows if y["id"] == e["id"]), None), "fyi")
    call("/api/emails/" + e["id"], "PATCH", {"tags": tags0})
    check("还原标签", call("/api/emails/" + e["id"]).get("tags") or [], tags0)

print()
if fail:
    print("RESULT FAILED(%d): %s" % (len(fail), ", ".join(fail))); sys.exit(1)
print("RESULT ALL_PASS")
