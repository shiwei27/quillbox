#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：AI 分析读附件。文件在内存里现造，取件与模型都用假的——不联网、不写库、不动磁盘缓存。"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import io, json, sys, zipfile
sys.path.insert(0, ROOT)
from core import attachment_text as at
from core import insight_v2 as iv2
from core.ai_engine import AIEngine
fail = []
def check(label, got, want=True):
    ok = got == want
    print("  %s %-62s got=%r" % ("OK  " if ok else "FAIL", label, got if not isinstance(got, (dict, list)) else str(got)[:70]))
    if not ok: fail.append(label)

def mini_pdf(text):
    stream = ("BT /F1 18 Tf 72 720 Td (%s) Tj ET" % text).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out); out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs: out += b"%010d 00000 n \n" % o
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)

print("== 抽取")
from openpyxl import Workbook
wb = Workbook(); ws = wb.active; ws.title = "耗材报价"
ws.append(["型号", "数量", "单价(元)"]); ws.append(["打印纸", 8, 235.0]); ws.append(["硒鼓", 4, 320.5]); ws.append([None, None, None])
ws2 = wb.create_sheet("备注"); ws2.append(["含税", "是"])
b = io.BytesIO(); wb.save(b)
t, st, rs, tr = at.extract_text("报价.xlsx", b.getvalue())
check("xlsx：按工作表、行、单元格还原", ("〔工作表 耗材报价〕型号 | 数量 | 单价(元)；打印纸 | 8 | 235；硒鼓 | 4 | 320.5" in t, "〔工作表 备注〕含税 | 是" in t), (True, True))
check("xlsx：整数型浮点不带 .0", "235.0" not in t, True)
wb = Workbook(); ws = wb.active
for i in range(400): ws.append(["行%d" % i, "x" * 40])
b = io.BytesIO(); wb.save(b)
t, st, rs, tr = at.extract_text("大表.xlsx", b.getvalue(), 1000)
check("xlsx：超长按字数上限截断并标记", (len(t) <= 1000, tr, st), (True, True, "read"))
from docx import Document
d = Document(); d.add_paragraph("合同主要条款"); tb = d.add_table(rows=2, cols=2); tb.cell(0, 0).text = "期限"; tb.cell(0, 1).text = "2 年"; tb.cell(1, 0).text = "付款"; tb.cell(1, 1).text = "按月后付"
d.add_paragraph("结尾段落"); b = io.BytesIO(); d.save(b)
t, st, rs, tr = at.extract_text("合同.docx", b.getvalue())
check("docx：段落与表格按原顺序", t.index("合同主要条款") < t.index("期限 | 2 年") < t.index("结尾段落"), True)
t, st, rs, tr = at.extract_text("q.pdf", mini_pdf("Quote paper price 235 per box for ten boxes"))
check("pdf：抽出文字", (st, "paper price 235" in t), ("read", True))
from pypdf import PdfWriter
w = PdfWriter(); w.add_blank_page(612, 792); b = io.BytesIO(); w.write(b)
t, st, rs, tr = at.extract_text("扫描件.pdf", b.getvalue())
check("pdf：没有文字（扫描件）→ scan，如实说明", (st, "扫描" in rs), ("scan", True))
b = io.BytesIO()
with zipfile.ZipFile(b, "w") as z:
    z.writestr("ppt/slides/slide2.xml", "<a:t>第二页：风险</a:t>"); z.writestr("ppt/slides/slide1.xml", "<a:t>第一页：背景</a:t><a:t>预算 2 万</a:t>")
t, st, rs, tr = at.extract_text("汇报.pptx", b.getvalue())
check("pptx：按页序抽文字", t.index("第一页") < t.index("第二页") and "预算 2 万" in t, True)
t, st, rs, tr = at.extract_text("巡检.html", "<html><style>.a{}</style><script>x=1</script><body><table><tr><td>集群</td><td>异常Pod</td></tr><tr><td>demo</td><td>2</td></tr></table></body></html>".encode())
check("html：去脚本样式，保留表格结构", ("集群" in t and "demo" in t and "x=1" not in t and ".a{}" not in t, st), (True, "read"))
t, st, rs, tr = at.extract_text("说明.txt", "中文说明：价格已含税".encode("gb18030"))
check("txt：GBK 也能解", (st, "价格已含税" in t), ("read", True))
check(".doc 旧格式 → unsupported 且不抛异常", at.extract_text("旧.doc", b"\xd0\xcf\x11\xe0")[1], "unsupported")
check("损坏的 xlsx → failed 且不抛异常", at.extract_text("坏.xlsx", b"not a zip")[1], "failed")
check("空 txt → empty", at.extract_text("空.txt", b"   ")[1], "empty")
import zipfile as _z
class Big:
    def __init__(self, n): self.file_size = n
orig = _z.ZipFile.infolist
_z.ZipFile.infolist = lambda self: [Big(150 * 1024 * 1024), Big(120 * 1024 * 1024)]
try:
    check("压缩炸弹（解压后过大）被拒绝", at.extract_text("炸弹.docx", b.getvalue())[1], "failed")
finally:
    _z.ZipFile.infolist = orig

print("== 选择与过滤")
e = {"attachments": json.dumps(["image001.png", "a.docx", "b.xlsx", "旧.doc", "c.pdf", "d.pptx", "logo.jpg"])}
check("只挑文档，图片忽略，可读的排前、上限 3 个", at.pick_names(e), ["a.docx", "b.xlsx", "c.pdf"])
check("只有图片 → 不取", at.pick_names({"attachments": json.dumps(["image001.png", "x.jpg"])}), [])
check("附件字段是脏数据也不抛异常", at.pick_names({"attachments": "not json"}), [])

print("== read_for_analysis（假取件）")
calls = []
xl = io.BytesIO(); wb = Workbook(); wb.active.append(["型号", "单价"]); wb.active.append(["打印纸", 235]); wb.save(xl)
def fake_fetch(cfg, row, names, max_bytes, timeout=45):
    calls.append(list(names)); return {n: ((xl.getvalue(), "") if n.endswith(".xlsx") else (None, "取回附件失败：连不上")) for n in names}
at.fetch_many = fake_fetch
row = {"id": "t1", "subject": "报价", "sender_email": "a@example.com", "account": "x", "attachments": json.dumps(["image1.png", "报价.xlsx", "方案.docx"])}
blk, info = at.read_for_analysis({}, row)
check("文字块带【附件：文件名】前缀和内容", blk.startswith("【附件：报价.xlsx】〔工作表") and "型号 | 单价" in blk and "打印纸 | 235" in blk, True)
check("info：成功与失败都如实记录", [(i["name"], i["status"]) for i in info], [("报价.xlsx", "read"), ("方案.docx", "failed")])
check("失败的带原因", info[1]["reason"], "取回附件失败：连不上")
n0 = len(calls); blk2, info2 = at.read_for_analysis({}, row)
check("同一封邮件第二次读取命中内存缓存的部分（成功的不重取）", len(calls) - n0 <= 1 and "报价.xlsx" not in (calls[-1] if len(calls) > n0 else []), True)
check("关闭开关 → 不读", at.read_for_analysis({"ai": {"read_attachments": False}}, dict(row, id="t2")), ("", []))
check("系统发件人（skip_sender_patterns）→ 不读", at.read_for_analysis({"ai": {"skip_sender_patterns": ["noc@"]}}, dict(row, id="t3", sender_email="noc@example.org")), ("", []))
check("主题命中排除词 → 不读", at.read_for_analysis({"ai": {"attachment_skip_subjects": ["薪酬"]}}, dict(row, id="t4", subject="薪酬调整")), ("", []))
check("没有文档附件 → 不取件", (at.read_for_analysis({}, {"id": "t5", "attachments": json.dumps(["a.png"])}), len(calls)), (("", []), len(calls)))
big = {"id": "t6", "attachments": json.dumps(["a.xlsx", "b.xlsx", "c.xlsx"]), "sender_email": "a@b.com"}
wb = Workbook(); wb.active.append(["x" * 5000]); xl = io.BytesIO(); wb.save(xl)
blk, info = at.read_for_analysis({}, big, 7000)
check("总字数上限生效", len(blk) < 7400, True)

print("== 引擎集成（假 LLM + 假附件）")
eng = AIEngine(base_url="", model="", provider="api", api_base_url="http://x", api_key="k", api_model="m", api_timeout=5, prompts={}, signature="")
prompts = []
V2 = {"type": "other", "level": "reply", "level_reason": "x", "headline": "周小舟补充了耗材报价：打印纸单价 235 元", "digest": "报价表显示打印纸单价 235 元，8 箱。",
      "points": ["打印纸 8 箱 235"], "actions": [], "questions": [], "gaps": [{"what": "没写有效期", "why_it_matters": "x", "evidence": "打印纸 | 235"}], "lens": {}, "confidence": "high", "notes": []}
def fake_gen(prompt, fmt=None, timeout=60):
    prompts.append(prompt); return json.dumps(V2, ensure_ascii=False)
eng._generate = fake_gen
xl = io.BytesIO(); wb = Workbook(); wb.active.append(["型号", "单价"]); wb.active.append(["打印纸", 235]); wb.save(xl)
mail = {"id": "e1", "subject": "答复: 耗材报价", "sender": "周小舟", "sender_email": "zxz@example.com", "body": "已补充报价，请确认。", "time": "2026-10-20 10:00",
        "recipients": "me@x.com", "account": "a", "attachments": json.dumps(["报价.xlsx"])}
r = eng.analyze_insight(mail)
check("v2：附件文字进了 prompt", "【附件：报价.xlsx】〔工作表" in prompts[-1] and "打印纸 | 235" in prompts[-1], True)
check("v2：引自附件的证据通过核对（缺口保留）", [g["what"] for g in r["v2"]["gaps"]], ["没写有效期"])
check("v2：记录读了哪些附件", [(a["name"], a["status"]) for a in r["v2"]["attachments"]], [("报价.xlsx", "read")])
check("附件有内容 → 不会被当成指针邮件", r["headline"].startswith("周小舟补充了耗材报价"), True)
mail2 = dict(mail, id="e2", body="请查收附件。", subject="报价", attachments=json.dumps(["方案.docx"]))
V2["level"] = "archive"; V2["headline"] = "请查收附件"; V2["digest"] = "报价附件"; V2["points"] = []; V2["gaps"] = []
at._MEMO.clear(); r = eng.analyze_insight(mail2)
check("附件读取失败的指针邮件 → 如实说明原因", ("没能读取" in r["headline"], "连不上" in r["headline"]), (True, True))
V2["level"] = "reply"; V2["headline"] = "对方发来方案，请确认"; V2["digest"] = "对方发来方案附件，请确认"; V2["points"] = ["请确认方案"]
at._MEMO.clear(); rb = eng.analyze_insight(dict(mail2, id="e2b", body="方案见附件，请你本周内确认并回复我，谢谢，期待你的反馈意见。" * 3))
check("附件没读到：要点里明说「附件未读取：文件名（原因）」", any(p.startswith("附件未读取：方案.docx") and "连不上" in p for p in rb["points"]), True)
check("附件没读到：v2.notes 同步记录", any(n.startswith("附件未读取") for n in rb["v2"]["notes"]), True)
V2["level"] = "archive"; V2["headline"] = "请查收附件"; V2["digest"] = "报价附件"; V2["points"] = []
at.fetch_many = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
eng._generate = lambda p, fmt=None, timeout=60: json.dumps(dict(V2, level="reply", headline="不受影响的摘要", digest="正常分析"), ensure_ascii=False)
at._MEMO.clear(); r = eng.analyze_insight(dict(mail, id="e3"))
check("取件整个炸了也不影响分析（按无附件继续）", (r.get("ok"), r["headline"]), (True, "不受影响的摘要"))
machine = {"id": "e4", "subject": "云监控周报", "sender_email": "monitor@x.com", "body": "云监控邮件周报", "attachments": json.dumps(["周报.pdf"])}
print("\nFAIL: %s" % fail if fail else "\nALL PASS")
sys.exit(1 if fail else 0)
