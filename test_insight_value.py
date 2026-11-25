#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：摘要价值（B 链接/附件型邮件如实标注；C v2 摘要去元话）。纯函数 + 假 LLM，不调真实模型。"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import json, sys
sys.path.insert(0, ROOT)
from core import insight_v2 as iv2
from core.ai_engine import AIEngine
fail = []
def check(label, got, want=True):
    ok = got == want
    print("  %s %-62s got=%r" % ("OK  " if ok else "FAIL", label, got if not isinstance(got, (dict, list)) else str(got)[:70]))
    if not ok: fail.append(label)
BOIL = " *****该邮件由系统自动发送，请勿回复***** ----- 本邮件及其附件含有示例科技公司的保密信息，仅限于发送给上面地址中列出的个人或群组。This e-mail and its attachments contain confidential information."
def mk(subj, body, se="x@example.com", att=None, html=""):
    return {"subject": subj, "body": body, "html_body": html, "sender_email": se, "attachments": json.dumps(att or [], ensure_ascii=False)}
print("== B：链接 / 附件型识别")
gg = mk("【公告】行政字【2026】第2号 关于图书角管理小组调整的公告",
        "亲爱的同事： 请查看行政部发布的公告通知：行政字【2026】第2号 关于图书角管理小组调整的公告， 点此查看详情" + BOIL)
i = iv2.pointer_info(gg)
check("OA 公告（正文只有「点此查看详情」）→ 链接型", i and i["kind"], "link")
check("说明里点明「只有链接」", i and "只有链接" in i["message"], True)
check("云监控周报（正文 7 个字）→ 空正文", (iv2.pointer_info(mk("10月09日-10月16日 云监控周报", "云监控邮件周报")) or {}).get("kind"), "empty")
check("待办流程提醒 → 链接型", (iv2.pointer_info(mk("【待办】周小舟提交的请假申请单（单号:LV-0001）", "亲爱的同事： 请您处理周小舟提交的【请假申请】。 点此前往处理 -------" + BOIL)) or {}).get("kind"), "link")
ia = iv2.pointer_info(mk("报价确认", "请查收附件。", att=["image001.png", "显卡报价1019.xlsx"]))
check("「请查收附件」+ xlsx → 附件型，图片不算", (ia or {}).get("kind"), "attachment")
check("附件型说明点名文件并说明未读取", ia and "报价1019.xlsx" in ia["message"] and "没有读取附件" in ia["message"], True)
check("示例云云盘通知（有新数字 3）不算指针", iv2.pointer_info(mk("示例云按量付费云盘创建成功提醒", "尊敬的 ops@example.org ： 您有3块按量付费云盘创建成功。 查看及管理磁盘，请登录 ECS控制台-全部磁盘 。", se="system@notice.examplecloud.example")), None)
check("文件发布通知（有版本号变化）不算指针", iv2.pointer_info(mk("【通知】陈一提交的《会议室预订指引》等文件已发布", "亲爱的同事： 《会议室预订指引》 02.10→02.20 《访客登记说明》 02.00→02.20 点此查看详情")), None)
check("转发 + 被引用历史（FYI）不算指针", iv2.pointer_info(mk("供应商带宽优惠事宜", "FYI 发件人: 张三 发送时间: 2026年9月1日 主题: 带宽优惠 各位好，供应商承诺的优惠如下：第一年按月返还，第二年…" + "优惠细节。" * 30)), None)
check("没有任何指针线索的短消息不算指针", iv2.pointer_info(mk("项目安排", "好的，下周三碰一下，我把材料带过去。")), None)
check("正文很长不算指针", iv2.pointer_info(mk("周报", "本周完成了很多事情。" * 80 + "点此查看详情")), None)
check("空邮件不抛异常", iv2.pointer_info({}) is None or True, True)

print("== B：标注逻辑（假 LLM 结果）")
eng = AIEngine(base_url="", model="", provider="api", api_base_url="http://x", api_key="k", api_model="m", api_timeout=5, prompts={}, signature="")
def run(mail, res):
    eng._analyze_insight_inner = lambda e: res
    return eng.analyze_insight(mail)
res_n = {"ok": True, "priority": "notice", "headline": "图书角管理小组调整公告已发布", "summary": "行政部发布关于图书角管理小组调整的公告。", "points": ["x"]}
r = run(gg, dict(res_n))
check("通知类、摘要只是重说主题 → 换成如实说明", (r["headline"], r["points"]), (iv2.pointer_info(gg)["message"], []))
r = run(gg, dict(res_n, priority="act"))
check("需要行动的（act）保留原摘要", r["headline"], "图书角管理小组调整公告已发布")
r = run(gg, dict(res_n, headline="小组新增3名成员：张某、李某、王某；原成员刘某退出", summary="图书角管理小组新增张某、李某、王某三名成员，刘某退出，自10月21日起生效。"))
check("正文只有指针时，模型写出的「新内容」只能是猜测 → 如实说明", r["headline"], iv2.pointer_info(gg)["message"])
r = run(gg, {"ok": False, "error": "generation_failed"})
check("生成失败原样返回", r, {"ok": False, "error": "generation_failed"})
r = run(gg, dict(res_n, v2={"headline": "旧", "digest": "旧", "points": ["旧"], "level": "archive"}))
check("同步改写 v2 的 headline / digest / points", (r["v2"]["headline"] == r["headline"], r["v2"]["points"], r["v2"]["level"]), (True, [], "archive"))
r = run(mk("示例云按量付费云盘创建成功提醒", "尊敬的 ops@example.org ： 您有3块按量付费云盘创建成功。请登录 ECS控制台。", se="system@notice.examplecloud.example"), {"ok": True, "priority": "notice", "headline": "示例云创建了3块按量付费云盘", "summary": "x", "points": []})
check("有实际信息的通知不被改写", r["headline"], "示例云创建了3块按量付费云盘")

r = run(gg, dict(res_n, priority="act", points=["公告第2号", "行政部发布", "小组调整", "点此查看详情"]))
check("act 保留标题，但没有内容的指针邮件不留要点", r["points"], [])
print("== 验收修复")
T0 = __import__("datetime").date(2026, 10, 20)
BODY_LONG = "本周完成了日常巡检、变更评审和容量处置，具体数据见下。" * 12
v = iv2.normalize({"type": "other", "level": "archive", "headline": "供应商甲的服务赔付金额确认邮件", "digest": "周小舟要求林小满确认供应商甲8月服务中断赔付金额，合计1234.56元将在下月账单扣减。"}, BODY_LONG, "unknown", today=T0)[0]
check("类型标签式标题（无数字、以「邮件」结尾）→ 用摘要第一句", v["headline"].startswith("周小舟要求林小满确认"), True)
v = iv2.normalize({"type": "other", "level": "archive", "headline": "例行周报，没有需要你处理的事。", "digest": "吴一凡汇报了本周工作：完成测试集群新节点组的创建与旧节点组的下线迁移，修改网关限流隔离方案的评审意见。下周继续监控看板开发与联调。"}, BODY_LONG, "unknown", today=T0)[0]
check("内容丰富的邮件里用了套话标题 → 换成摘要第一句", v["headline"].startswith("吴一凡汇报了本周工作"), True)
check("要点为空时从摘要拆出", len(v["points"]) >= 1, True)
v = iv2.normalize({"type": "other", "level": "archive", "headline": "例行周报，没有需要你处理的事。", "digest": ""}, "本周日常巡检。", "unknown", today=T0)[0]
check("正文很短的真例行邮件保留套话（验收用例 2）", v["headline"], "例行周报，没有需要你处理的事。")
v = iv2.normalize({"type": "other", "level": "archive", "headline": "供应商停服预警函转发", "digest": "x" * 30}, "正文", "unknown", today=T0, subject="转发: 【停服预警函】关于逾期付款")[0]
check("主题带「停服 / 预警函」的仅存档 → 至少 watch", v["level"], "watch")
v = iv2.normalize({"type": "other", "level": "archive", "headline": "系统停服预警", "digest": "x" * 30}, "正文", "system", today=T0, subject="【停服预警函】")[0]
check("系统发件人不触发风险抬升", v["level"], "archive")
v = iv2.normalize({"type": "other", "level": "archive", "headline": "普通通报", "digest": "x" * 30}, "正文", "unknown", today=T0, subject="季度评选结果")[0]
check("普通主题不抬升", v["level"], "archive")
info = iv2.pointer_info(mk("【资讯速递】本期行业动态", "祝：工作顺利！ ============================================ 资讯部 示例科技股份有限公司 Example Technology Digital Technology Co.,Ltd. ====== 本邮件及其附件含有示例科技公司的保密信息"))
check("签名块 / 分隔线之后的页脚不算内容 → 空正文", (info or {}).get("kind"), "empty")
check("真正有内容、末尾才是签名的不受影响", iv2.pointer_info(mk("项目进展", "本周完成了接口联调，下周开始压测，需要运维配合开放端口 8443 和 9090。" * 3 + " ======== 示例科技股份有限公司")), None)
m1 = {"id": "z", "subject": "示例云网站有奖调研", "sender_email": "no-reply@examplecloud.example", "body": "此邮件由系统自动发出，请勿直接回复。诚邀您参与调研，填写问卷可抽100元代金券。"}
r = run(m1, {"ok": True, "priority": "reply", "headline": "邀请参与调研", "summary": "邀请参与调研，抽奖", "points": []})
check("机器通知不会「等你回复」→ 降为 notice", r["priority"], "notice")
r = run(mk("人事通知", "请回复确认是否参加。" * 8, se="hr@example.com"), {"ok": True, "priority": "reply", "headline": "h", "summary": "s", "points": []})
check("真人邮件的 reply 保持不变", r["priority"], "reply")
v = iv2.normalize({"type": "other", "level": "archive", "headline": "资讯部发布了行业周报，存档即可。", "digest": "资讯部发布了第12期行业周报，主题包括开源动态和硬件价格。"}, BODY_LONG, "unknown", today=T0)[0]
check("标题里的「存档即可」去掉（并带上内容）", ("存档即可" not in v["headline"], v["headline"].startswith("资讯部发布了")), (True, True))
v = iv2.normalize({"type": "other", "level": "archive", "headline": "这是一封来自行政部的内部宣传邮件", "digest": "这是一封来自行政部的内部宣传邮件，邀请你参与评论并分享身边的好故事以赢取积分，并附有相关文章。"}, BODY_LONG, "unknown", today=T0)[0]
check("「这是一封…邮件」式标题 → 去掉「这是一封」并带上内容", (not v["headline"].startswith("这是一封"), "积分" in v["headline"] or "评论" in v["headline"]), (True, True))
check("「邮件内容与市场负责人的职责范围无关」去掉", iv2.clean_meta("邀请参与评论。邮件内容与市场负责人的职责范围无关。"), "邀请参与评论。")
check("「你不在收件人列表中」去掉", iv2.clean_meta("知会性质，你不在收件人列表中"), "知会性质")
v = iv2.normalize({"type": "other", "level": "archive", "headline": "例行通知：读书会在3号楼开讲", "digest": "x" * 30}, BODY_LONG, "unknown", today=T0)[0]
check("标题的「例行通知：」前缀去掉", v["headline"], "读书会在3号楼开讲")
check("「例行周报，没有需要你处理的事」不受影响", iv2.normalize({"type": "other", "level": "archive", "headline": "例行周报，没有需要你处理的事。", "digest": ""}, "短正文", "unknown", today=T0)[0]["headline"], "例行周报，没有需要你处理的事。")
iv2.load_reader = lambda: {"role": "市场负责人：品牌、渠道、活动"}   # clean_meta 识别「不涉及<岗位>」要用到 reader.role
check("「你作为市场负责人，不涉及该岗位」去掉", iv2.clean_meta("人力资源部发布竞聘信息。你作为市场负责人，不涉及该岗位的竞聘或决策。"), "人力资源部发布竞聘信息。")
v = iv2.normalize({"type": "other", "level": "archive", "headline": "行政部发出的周末读书会报名通知", "digest": "这是一封行政部发出的周末读书会报名通知。活动将于10月19日在3号楼2层多功能厅举行，时间14:00-16:00，费用全免，共30个名额，先到先得。"}, BODY_LONG, "unknown", today=T0)[0]
check("标签式标题 → 跳过同样是标签的第一句，取带数字的句子", v["headline"], "活动将于10月19日在3号楼2层多功能厅举行")
v = iv2.normalize({"type": "other", "level": "reply", "headline": "来自行政部的员工故事分享邮件，主题为“从‘按时交付’到‘携手共进’”，介绍一名同事帮助新同事快速上手的经历", "digest": "x"}, "正文", "unknown", today=T0)[0]
check("超长标题在分句处截断，不停在半个词上", (len(v["headline"]) <= 48, v["headline"].endswith(("邮件", "”", "共进’”"))), (True, True))
print("== C：去元话")
check("「读者」改称「你」", iv2.clean_meta("读者林小满需在10月20日前输出方案"), "你需在10月20日前输出方案")
check("「读者作为…」也改称", iv2.clean_meta("读者作为市场负责人，需要确认"), "你作为市场负责人，需要确认")
check("只说被抄送的分句整个去掉", iv2.clean_meta("型号需确认，读者被抄送但未明确要求回复"), "型号需确认")
check("「与你无关」去掉", iv2.clean_meta("人力资源部发出竞聘通知，与你无关。"), "人力资源部发出竞聘通知")
check("「你在抄送中」「仅面向收件组」去掉", iv2.clean_meta("网络安全部发起了红线考试的通知，仅面向收件组all_staff，你在抄送中"), "网络安全部发起了红线考试的通知")
check("「与你的职责无关」去掉", iv2.clean_meta("仅存档，与你的职责无关。"), "仅存档")
leg = AIEngine._strip_boilerplate({"priority": "fyi", "actions": [], "headline": "考试通知，你在抄送中", "summary": "网络安全部发起考试，仅面向收件组x，你在抄送中。", "points": ["读者被抄送"]})
check("旧路径输出也去元话", (leg["headline"], leg["summary"], leg["points"]), ("考试通知", "网络安全部发起考试", []))
check("「不涉及市场团队」「内容与你的…等无关」去掉", iv2.clean_meta("这是知识分享，不涉及市场团队的具体工作，内容与你的品牌、渠道、活动等无关"), "这是知识分享")
check("「与你负责的市场无关」去掉", iv2.clean_meta("仅存档，与你负责的市场无关"), "仅存档")
check("悬空的「无需你」去掉", iv2.clean_meta("该邮件为全公司范围通知，无需你。"), "该邮件为全公司范围通知")
check("正常内容不动", iv2.clean_meta("采购 4 台服务器，预算 20 万元"), "采购 4 台服务器，预算 20 万元")
raw = {"type": "other", "level": "archive", "headline": "仅存档，与你无关。", "digest": "这是人力资源部发出的区域经理内部竞聘通知，面向全公司。读者与本竞聘无直接关系，与你无关。",
       "points": ["读者林小满被抄送，非直接收件人。", "竞聘面向全公司"]}
v = iv2.normalize(raw, "竞聘通知正文", "unknown")[0]
check("normalize：headline 清完只剩「仅存档」→ 用摘要第一句", v["headline"], "这是人力资源部发出的区域经理内部竞聘通知，面向全公司")
vp = iv2.normalize({"type": "other", "level": "archive", "headline": "仅存档：周小舟补充了显卡租用报价，折扣后月价 999 元起", "digest": "x"}, "正文", "unknown")[0]
check("headline 去掉「仅存档：」前缀", vp["headline"], "周小舟补充了显卡租用报价，折扣后月价 999 元起")
vq = iv2.normalize({"type": "other", "level": "archive", "headline": "仅存档，没事", "digest": "x"}, "正文", "unknown")[0]
check("去前缀后太短则不动", vq["headline"][:3], "仅存档")
check("normalize：纯元话的要点被丢弃", v["points"], ["竞聘面向全公司"])
check("normalize：digest 里的元话分句被去掉", "与你无关" not in v["digest"], True)
print("\nFAIL: %s" % fail if fail else "\nALL PASS")
sys.exit(1 if fail else 0)
