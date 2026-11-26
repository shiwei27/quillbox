#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：摘要按邮件类型分模式（纯函数，无 AI 调用、不碰库）。"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import sys, importlib.util
sys.path.insert(0, ROOT)
sp = importlib.util.spec_from_file_location("ae", _os.path.join(ROOT, 'core', 'ai_engine.py')); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
E = m.AIEngine
fail = []
def check(label, got, want=True):
    ok = got == want
    print("  %s %-58s got=%r" % ("OK  " if ok else "FAIL", label, got))
    if not ok: fail.append(label)
def mk(subj, se, body): return {"subject": subj, "sender_email": se, "body": body}
short = "正文" * 200
print("== 路由")
check("示例云系统通知(短) → machine", E._insight_mode(mk("示例云按量付费云盘创建成功提醒", "system@notice.examplecloud.example", short)), "machine")
check("告警主题 + 真人地址 → machine", E._insight_mode(mk("【告警】CPU 使用率过高 PROBLEM", "ops@x.com", short)), "machine")
check("正文含「请勿回复」 → machine", E._insight_mode(mk("您的订单", "a@b.com", "感谢购买。" + "x" * 300 + "此邮件为系统自动发出，请勿回复")), "machine")
check("长机器报告(>1500) → doc", E._insight_mode(mk("示例集群巡检报告", "noc@x.com", "巡检" * 2000)), "doc")
check("会议纪要 → doc", E._insight_mode(mk("【通知】请您查看xx会议纪要", "a@example.com", short)), "doc")
check("周报 → doc", E._insight_mode(mk("示例部-工作周报", "a@example.com", short)), "doc")
check("采购申请(真人) → request", E._insight_mode(mk("关于服务器采购的补充说明", "a@example.com", short)), "request")
check("回复前缀 → request", E._insight_mode(mk("回复: 项目安排", "a@example.com", short)), "request")
check("答复: 周报 也算回复 → request", E._insight_mode(mk("答复: 示例部周报", "a@example.com", short)), "request")
check("人写的合同到期续费请示 → request(不被当成通知)", E._insight_mode(mk("关于合同到期续费的请示", "a@example.com", short)), "request")
check("普通短邮件 → general", E._insight_mode(mk("周五聚餐", "a@example.com", "大家周五一起吃饭")), "general")
check("普通超长邮件 → doc", E._insight_mode(mk("项目情况", "a@example.com", "内容" * 2000)), "doc")
check("空邮件不抛异常", E._insight_mode({}), "general")
print("== 提示词拼装")
for mode in ("machine", "request", "doc", "general"):
    p = E._prompt_for_mode(mode)
    ok = all(p.count(k) == 1 for k in ('"summary":', '"points":', '"headline":', '"todos":', '"drafts":', '"actions":', '"priority":', '{body}'))
    check("%s 提示词字段齐全且各一次" % mode, ok)
check("machine 提示词含「一句话事实」", "一句话事实" in E._prompt_for_mode("machine"))
check("doc 提示词含「3~5 句」", "3~5 句" in E._prompt_for_mode("doc"))
check("未知模式回落 general", E._prompt_for_mode("zzz"), E.DEFAULT_INSIGHT_PROMPT)
print("== 输入预算")
b = "甲" * 20000
check("doc 预算 9000 内", len(E._insight_input({"body": b}, 9000)) <= 9100, True)
check("默认预算 6000 内", len(E._insight_input({"body": b})) <= 6100, True)
print("\nFAIL: %s" % fail if fail else "\nALL PASS")
sys.exit(1 if fail else 0)
