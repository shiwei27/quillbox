# -*- coding: utf-8 -*-
"""insight.v2：「判断 + 证据」的结构化邮件分析。

纯函数模块，不依赖 ai_engine：
  load_reader()/v2_enabled()   读 config.yaml（按 mtime 缓存）
  resolve_relation()           发件人与读者的关系
  build_prompt()               system + user 拼成单个 prompt（AIEngine._generate 只收一段文本）
  normalize()                  宽松收敛模型输出 + 原文证据核对；level/headline 不合格返回 None（由调用方重试 / 降级）
  to_legacy()                  映射回旧字段（priority/summary/points/todos/drafts/actions/…）
"""
import datetime as _dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEVELS = ("decide", "reply", "watch", "archive")
TYPES = ("weekly_report", "approval_request", "meeting_minutes", "alert", "vendor", "cc_notice", "other")
RELATIONS = ("subordinate", "manager", "peer", "external", "system", "unknown")
KINDS = ("reply", "calendar", "todo", "open_link", "archive")
LEVEL_TO_PRIORITY = {"decide": "act", "reply": "reply", "watch": "notice", "archive": "fyi"}

# ── 配置 ─────────────────────────────────────────────────────────────
_CFG = {"mt": None, "data": {}}


def load_cfg() -> dict:
    p = ROOT / "config.yaml"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    if _CFG["mt"] != mt:
        import yaml
        with open(p, encoding="utf-8") as f:
            _CFG["data"] = yaml.safe_load(f) or {}
        _CFG["mt"] = mt
    return _CFG["data"]


def load_reader() -> dict:
    r = load_cfg().get("reader")
    return r if isinstance(r, dict) else {}


def v2_enabled() -> bool:
    return bool((load_cfg().get("ai") or {}).get("insight_v2", True))


_PROMPT = {"txt": None}


def system_prompt() -> str:
    if _PROMPT["txt"] is None:
        _PROMPT["txt"] = (Path(__file__).resolve().parent / "insight_v2_prompt.md").read_text(encoding="utf-8")
    return _PROMPT["txt"]


# ── 关系 ─────────────────────────────────────────────────────────────
def _person_match(p, se: str, name: str) -> bool:
    if not isinstance(p, dict):
        return False
    e = str(p.get("email") or "").strip().lower()
    n = str(p.get("name") or "").strip()
    return bool((e and e == se) or (n and not e and n in name))


def resolve_relation(email: dict, reader: dict, system: bool = False) -> str:
    """system → 系统；名单里的上级 / 下属 / 平级 → 对应关系；域名不在内部域名里 → external；其余 unknown。"""
    if system:
        return "system"
    se = str(email.get("sender_email") or "").strip().lower()
    name = str(email.get("sender") or "")
    reader = reader or {}
    if _person_match(reader.get("manager"), se, name):
        return "manager"
    if any(_person_match(p, se, name) for p in (reader.get("subordinates") or [])):
        return "subordinate"
    if any(_person_match(p, se, name) for p in (reader.get("peers") or [])):
        return "peer"
    dom = se.split("@")[-1] if "@" in se else ""
    internal = [str(d).strip().lower() for d in (reader.get("internal_domains") or []) if d]
    if dom and internal and not any(dom == d or dom.endswith("." + d) for d in internal):
        return "external"
    return "unknown"


# ── prompt ───────────────────────────────────────────────────────────
def _plain_names(lst):
    out = []
    for p in lst or []:
        n = p.get("name") if isinstance(p, dict) else p
        if n:
            out.append(str(n))
    return out


def build_prompt(email: dict, reader: dict, relation: str, body: str, truncated: bool = False, now_iso: str = "") -> str:
    reader = reader or {}
    mgr = reader.get("manager")
    payload = {
        "now": now_iso or _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "reader": {"name": reader.get("name") or "", "role": reader.get("role") or "",
                   "manager": (mgr.get("name") if isinstance(mgr, dict) else mgr) or "",
                   "subordinates": _plain_names(reader.get("subordinates")),
                   "focus": [str(x) for x in (reader.get("focus") or [])]},
        "relation": relation,
        "email": {"subject": str(email.get("subject") or ""),
                  "from": str(email.get("sender") or "") + " <" + str(email.get("sender_email") or "") + ">",
                  "to": [x.strip() for x in re.split(r"[;,，；]", str(email.get("recipients") or "")) if x.strip()][:10],
                  "cc": [], "date": str(email.get("time") or ""),
                  "attachments": _attach_names(email), "truncated": bool(truncated), "body": body},
        "thread": [], "series_previous": None, "reader_feedback": [],
        "calendar": _calendar(),
    }
    return (system_prompt().rstrip() + "\n\n=====\n\n分析下面这封邮件。只输出符合 insight.v2 的 JSON。\n\n```json\n"
            + json.dumps(payload, ensure_ascii=False) + "\n```")


_WD = "一二三四五六日"


def _calendar(today=None):
    """近 7 天到未来 14 天每个日期的星期，让模型直接查表而不是自己推算（模型算星期经常错）。"""
    today = today or _dt.date.today()
    return {(today + _dt.timedelta(days=i)).isoformat(): "周" + _WD[(today + _dt.timedelta(days=i)).weekday()] for i in range(-7, 15)}


def _attach_names(email: dict):
    a = email.get("attachments")
    try:
        if isinstance(a, str):
            a = json.loads(a) if a.strip().startswith("[") else []
        out = []
        for x in a or []:
            n = x.get("name") or x.get("filename") if isinstance(x, dict) else x
            if n:
                out.append(str(n)[:80])
        return out[:10]
    except Exception:
        return []


# ── 收敛 + 证据核对 ───────────────────────────────────────────────────
_PUNCT = re.compile(r"[\s，,。.；;：:、！!？?“”\"'‘’（）()\[\]【】《》<>·\-—_/\\|]")


def _n(s) -> str:
    return _PUNCT.sub("", str(s or "")).lower()


def _s(x, n) -> str:
    return " ".join(str(x if x is not None else "").split())[:n]


def _sn(x, n):
    return _s(x, n) or None


def _date(x):
    x = str(x or "").strip()[:10]
    try:
        return _dt.date.fromisoformat(x).isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x) else None
    except ValueError:
        return None


def _strs(x, n, w):
    out = []
    for v in (x if isinstance(x, list) else []):
        v = _s(v, w)
        if v and v not in out:
            out.append(v)
    return out[:n]


def _int(x, d=0):
    try:
        return int(x)
    except (TypeError, ValueError):
        return d


class _Ev:
    """证据必须能在原文里找到（忽略空白与标点）。"""

    def __init__(self, body_text: str):
        self.body = _n(body_text)
        self.checked = self.ok_n = self.dropped = 0

    def ok(self, ev) -> bool:
        ev = _n(ev)
        if len(ev) < 3:
            return True
        self.checked += 1
        if ev in self.body:
            self.ok_n += 1
            return True
        self.dropped += 1
        return False


def _items(lst, ev: _Ev, n=6):
    out = []
    for x in (lst if isinstance(lst, list) else []):
        if isinstance(x, str):          # 模型有时直接给字符串：没有引文，只能算推断
            x = {"text": x, "basis": "inferred", "evidence": None}
        if not isinstance(x, dict):
            continue
        t = _s(x.get("text"), 120)
        basis = x.get("basis") if x.get("basis") in ("stated", "inferred", "unchecked") else "stated"
        e = _sn(x.get("evidence"), 60)
        if not t:
            continue
        if e and not ev.ok(e):
            continue
        out.append({"text": t, "basis": basis, "evidence": e})
    return out[:n]


_LENS_KEYS = {"weekly_report": ("changes", "plan_check", "counts", "period", "next", "signals", "unquantified"),
              "approval_request": ("ask", "materials", "risks", "amount", "scope"),
              "meeting_minutes": ("my_actions", "decisions", "vs_last"), "alert": ("object", "status", "ticket"),
              "vendor": ("they_want", "first_contact", "caution", "contract_or_amount"), "cc_notice": ("why_cc", "mentions_reader")}


def _lens(typ: str, src, ev: _Ev) -> dict:
    if isinstance(src, dict) and not isinstance(src.get(typ), dict) and any(k in src for k in _LENS_KEYS.get(typ, ())):
        src = {typ: src}                # 模型把 lens 内容摊平写在了 lens 下一层：按 type 嵌套回去
    src = src.get(typ) if isinstance(src, dict) else None
    if not isinstance(src, dict):
        return {}
    L = lambda k: src.get(k) if isinstance(src.get(k), list) else []
    if typ == "weekly_report":
        c = src.get("counts") if isinstance(src.get("counts"), dict) else {}
        pc, ch = [], []
        for x in L("plan_check"):
            if isinstance(x, dict) and x.get("status") in ("done", "progress", "moved", "missing") and _s(x.get("planned"), 120):
                e = _sn(x.get("evidence"), 60)
                if e and not ev.ok(e):
                    continue
                pc.append({"planned": _s(x["planned"], 120), "status": x["status"], "evidence": e})
        for x in L("changes"):
            if not isinstance(x, dict) or not _s(x.get("what"), 80):
                continue
            e = _sn(x.get("evidence"), 60)
            if e and not ev.ok(e):
                continue
            ip = x.get("is_production")
            ch.append({"what": _s(x["what"], 80), "when": _sn(x.get("when"), 40),
                       "is_production": ip if isinstance(ip, bool) else "unknown", "reviewed": bool(x.get("reviewed")),
                       "missing": [m for m in (x.get("missing") if isinstance(x.get("missing"), list) else [])
                                   if m in ("window", "impact", "rollback", "change_ticket", "date")],
                       "unchecked": [m for m in (x.get("unchecked") if isinstance(x.get("unchecked"), list) else []) if m == "change_ticket"],
                       "evidence": e})
        return {"period": _sn(src.get("period"), 40),
                "counts": {"done": _int(c.get("done")), "in_progress": _int(c.get("in_progress")), "next": _int(c.get("next"))},
                "plan_check": pc[:8], "changes": ch[:6], "unquantified": _items(src.get("unquantified"), ev),
                "signals": _items(src.get("signals"), ev), "next": _strs(src.get("next"), 8, 60)}
    if typ == "approval_request":
        m = src.get("materials") if isinstance(src.get("materials"), dict) else {}
        return {"ask": _s(src.get("ask"), 120), "amount": _sn(src.get("amount"), 40), "scope": _sn(src.get("scope"), 80),
                "deadline": _date(src.get("deadline")),
                "materials": {"have": _strs(m.get("have"), 8, 40), "missing": _strs(m.get("missing"), 8, 40)},
                "risks": _items(src.get("risks"), ev)}
    if typ == "meeting_minutes":
        acts = []
        for x in L("my_actions"):
            if isinstance(x, dict) and _s(x.get("text"), 120):
                e = _sn(x.get("evidence"), 60)
                if e and not ev.ok(e):
                    continue
                acts.append({"text": _s(x["text"], 120), "owner_match": x.get("owner_match") if x.get("owner_match") in ("named", "maybe", "no") else "no",
                             "due": _date(x.get("due")), "evidence": e})
        return {"my_actions": acts[:8], "decisions": _items(src.get("decisions"), ev), "vs_last": _items(src.get("vs_last"), ev)}
    if typ == "alert":
        return {"object": _s(src.get("object"), 80), "status": src.get("status") if src.get("status") in ("firing", "resolved") else "firing",
                "first_seen": _sn(src.get("first_seen"), 40),
                "recent_count_7d": _int(src.get("recent_count_7d"), None) if src.get("recent_count_7d") is not None else None,
                "ticket": src.get("ticket") if src.get("ticket") in ("found", "none", "unchecked") else "unchecked"}
    if typ == "vendor":
        return {"they_want": _s(src.get("they_want"), 120), "contract_or_amount": _sn(src.get("contract_or_amount"), 60),
                "deadline": _date(src.get("deadline")), "first_contact": bool(src.get("first_contact")),
                "caution": _strs(src.get("caution"), 4, 60)}
    if typ == "cc_notice":
        return {"why_cc": _s(src.get("why_cc"), 100), "mentions_reader": bool(src.get("mentions_reader"))}
    return {}


def normalize(raw, body_text: str, relation: str, today=None, has_previous: bool = False, subject: str = ""):
    """→ (v2 | None, reason, stats)。None 表示不合格（level / headline 缺失或非法），调用方应重试或降级。"""
    if not isinstance(raw, dict):
        return None, "输出不是 JSON 对象", {}
    level = str(raw.get("level") or "").strip().lower()
    if level not in LEVELS:
        return None, "level 必须是 decide / reply / watch / archive 之一", {}
    headline = _s(raw.get("headline"), 60)
    if not headline:
        return None, "缺少 headline", {}
    ev = _Ev(body_text)
    typ = raw.get("type") if raw.get("type") in TYPES else "other"
    acts = []
    for a in (raw.get("actions") if isinstance(raw.get("actions"), list) else []):
        if not isinstance(a, dict):
            continue
        kind, label = a.get("kind"), _s(a.get("label") or a.get("text") or a.get("title"), 24)
        if kind not in KINDS or not label:
            continue
        date, draft, url = _date(a.get("date")), _sn(a.get("draft"), 200), _sn(a.get("url"), 500)
        if (kind == "reply" and not draft) or (kind == "calendar" and not date):
            continue
        if kind == "open_link" and not (url and re.match(r"https?://", url) and url in body_text):
            continue
        e = _sn(a.get("evidence"), 60)
        if e and not ev.ok(e):
            e = None            # 动作本身有用，只是引文对不上：去掉引文，不丢动作
        acts.append({"kind": kind, "label": label, "date": date, "draft": draft, "url": url, "evidence": e})
    if level == "archive":      # 仅存档：只留一个归档按钮、不追问
        acts = [a for a in acts if a["kind"] == "archive"][:1] or [{"kind": "archive", "label": "完成并归档", "date": None, "draft": None, "url": None, "evidence": None}]
    qs = []
    if level != "archive":
        for q in (raw.get("questions") if isinstance(raw.get("questions"), list) else []):
            ask = _s(q.get("ask") or q.get("question"), 40) if isinstance(q, dict) else ""
            if ask and _s(q.get("draft"), 160):
                qs.append({"ask": ask, "why": _s(q.get("why"), 60), "draft": _s(q["draft"], 160)})
    gaps = []
    for g in (raw.get("gaps") if isinstance(raw.get("gaps"), list) else []):
        if isinstance(g, str):
            g = {"what": g}
        if not isinstance(g, dict) or not _s(g.get("what") or g.get("item") or g.get("gap"), 60):
            continue
        e = _sn(g.get("evidence"), 60)
        if e and not ev.ok(e):
            continue            # 引文在原文里找不到 = 编造，整条丢弃
        gaps.append({"what": _s(g.get("what") or g.get("item") or g.get("gap"), 60), "why_it_matters": _s(g.get("why_it_matters"), 60), "evidence": e})
    lens = _lens(typ, raw.get("lens"), ev)
    notes = _strs(raw.get("notes"), 4, 80)
    conf = raw.get("confidence") if raw.get("confidence") in ("high", "medium", "low") else "medium"
    # ① 日期与星期对不上：用日历确定性检查，不靠模型推算
    cg = calendar_gaps(body_text, today or _dt.date.today())
    gaps = (cg + gaps)[:6]
    # ② 没有上期：不允许出现「对照上周计划」（模型会编上期内容）
    if typ == "weekly_report" and not has_previous:
        if isinstance(lens.get("plan_check"), list):
            lens["plan_check"] = []
        if not any("没有上期" in n for n in notes):
            notes = (notes + ["没有上期可对照"])[:4]
    # ③ 正文里针对 AI 的指令：只记一笔，不影响判断
    injected = bool(_INJ.search(body_text or ""))
    notes = [n for n in notes if injected or "指令" not in n]        # 模型凭空说「含有针对 AI 的指令」（把保密声明当指令）：以正文检测为准
    if injected and not any("指令" in n or "忽略" in n for n in notes):
        notes = (notes + ["正文含有针对 AI 的指令，已忽略"])[:4]
    # ④ 正文几乎只有图片：把握只能是低
    if _IMG.search(body_text or "") and len(_n(_IMG.sub("", body_text or ""))) < 30:
        conf = "low"
        if not any("图片" in n for n in notes):
            notes = (notes + ["正文几乎只有图片，判断依据不足"])[:4]
    # ⑦ 请示缺材料：每项缺失材料至少有一个追问（模型常列了缺口却不追问）
    if typ == "approval_request" and level != "archive":
        for m in ((lens.get("materials") or {}).get("missing") or [])[:2]:
            if len(qs) < 2 and not any(m[:2] in (q["ask"] + q["draft"]) for q in qs):
                qs.append({"ask": "能补充%s吗？" % m, "why": "请示里没有附%s" % m, "draft": "麻烦补充一下%s，我看完再给意见。" % m})
    # ⑥ 纪要里点名了读者的行动：至少 reply（提示词规则「读者被分配了行动」；模型偶尔只给 watch）
    if typ == "meeting_minutes" and level == "watch" and any(m.get("owner_match") == "named" for m in (lens.get("my_actions") or [])):
        level = "reply"
    # ⑤ 告警已恢复 → 仅存档（验收用例 5）；动作与追问同步收敛成仅归档
    level_reason = _s(raw.get("level_reason"), 80)
    if level != str(raw.get("level") or "").strip().lower() and level == "reply":
        level_reason = "纪要里点名了你的行动"
    if typ == "alert" and (lens.get("status") if isinstance(lens, dict) else None) == "resolved" and level != "archive":
        level, level_reason = "archive", "告警已恢复，无需处理"
        acts = [{"kind": "archive", "label": "完成并归档", "date": None, "draft": None, "url": None, "evidence": None}]
        qs = []
    v2 = {"version": "insight.v2", "type": typ, "relation": relation if relation in RELATIONS else "unknown", "level": level,
          "level_reason": level_reason, "headline": headline,
          "digest": _s(raw.get("digest"), 200), "points": _strs(raw.get("points"), 5, 60),
          "actions": acts[:3], "questions": qs[:2], "gaps": gaps[:6], "lens": {typ: lens} if lens else {},
          "confidence": conf, "notes": notes,
          "evidence_stats": {"checked": ev.checked, "ok": ev.ok_n, "dropped": ev.dropped}}
    # C：摘要里的元话（读者 / 被抄送 / 与你无关）
    v2["digest"] = clean_meta(v2["digest"], 200)
    v2["points"] = [p for p in (clean_meta(p, 60) for p in v2["points"]) if p]
    hl = clean_meta(v2["headline"], 60)
    hl = re.sub(r"[，,、 ]*(存档即可|仅供存档|存档备查|无需处理)[。.]?$", "", hl).strip() or hl     # 级别已有标签，标题别再写「存档即可」
    _rest = re.sub(r"^(仅存档|仅供参考|仅供知悉|仅供阅读)[：:，,、 ]*|^例行[^：:，,]{0,6}[：:]\s*", "", hl)      # _LEVEL_WORD：级别已有标签，标题里不重复
    if len(_rest) >= 6:
        hl = _rest
    if len(hl) <= 4:                                  # 清完只剩「仅存档」之类：用内容摘要的第一句顶上
        first = re.split(r"[。；;！!]", v2["digest"])[0].strip("，, ") if v2["digest"] else ""
        hl = first[:40] or hl or v2["headline"]
    # 验收修复：标题是类型标签 / 套话但摘要有内容 → 用摘要第一句顶上；要点为空 → 从摘要拆；档位是「仅存档」但主题带停服·催款等风险字样 → 至少 watch
    d = v2["digest"]
    body_len = len(re.sub(r"\s+", "", body_text or ""))
    weak = (not re.search(r"\d", hl) and _LABEL_END.search(hl)) or (_ROUTINE.search(hl) and body_len >= 250 and len(d) >= 50)
    if weak and len(d) >= 20:
        sents = [x.strip() for x in re.split(r"[。；;！!]", d) if x.strip()]
        good = [x for x in sents if not re.match(r"这是一封", x) and not _LABEL_END.search(x)]      # 跳过「这是一封…通知」这类自己也是标签的句子
        pick = next((x for x in good if re.search(r"\d", x)), good[0] if good else (sents[0] if sents else d))
        lead = first_sentence(pick)
        lead = re.sub(r"^这是一封", "", lead).strip() or lead
        if len(lead) >= 10 and lead != hl:
            hl = lead
    if len(hl) > 48:                                  # 提示词要求 ≤40 字，模型常超；在分句处截，别停在半个词上
        cut = max(hl.rfind(c, 0, 48) for c in "，,；;、")
        hl = (hl[:cut] if cut >= 16 else hl[:48]).rstrip("，,；;、 ") 
    v2["headline"] = hl or v2["headline"]
    if not v2["points"] and len(d) >= 60:
        v2["points"] = [p.strip()[:60] for p in re.split(r"[。；;]", d) if len(p.strip()) >= 8][:4]
    if v2["level"] == "archive" and relation != "system" and _RISK_SUBJ.search(subject or ""):
        v2["level"], v2["level_reason"] = "watch", "主题涉及停服 / 欠款 / 逾期等风险字样，建议留意"
    return v2, "", v2["evidence_stats"]


_WD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*[（(]\s*(?:周|星期)\s*([一二三四五六日天])\s*[）)]")
_INJ = re.compile(r"(忽略|无视|不要理会).{0,8}(以上|上述|之前|前面|所有|全部).{0,8}(要求|指令|指示|内容|规则)|ignore\s+(all\s+)?(the\s+)?(previous|above|prior)"
                  r"|给\s*(AI|ai)\s*(助手|模型)?|(AI|ai)\s*(助手|模型)\s*[:：,，]?\s*(请|务必|必须|忽略|无视)", re.I)
_IMG = re.compile(r"\[(?:图片|image\d*|cid:[^\]]*)\]|<img\b", re.I)


def calendar_gaps(body_text: str, today) -> list:
    """正文里「10 月 18 日（周六）」这类写法，与真实星期不符时给出缺口。年份取离今天最近的一年。"""
    out = []
    for m in _WD_RE.finditer(body_text or ""):
        mo, d, wd = int(m.group(1)), int(m.group(2)), m.group(3)
        wd = "日" if wd == "天" else wd
        dt = None
        for y in (today.year, today.year + 1, today.year - 1):
            try:
                c = _dt.date(y, mo, d)
            except ValueError:
                continue
            if abs((c - today).days) <= 200:
                dt = c
                break
        if dt is None or _WD[dt.weekday()] == wd:
            continue
        out.append({"what": "%d月%d日写作周%s，实为周%s" % (mo, d, wd, _WD[dt.weekday()]), "why_it_matters": "日期和星期对不上，需向发件人确认",
                    "evidence": _s(m.group(0), 60)})
    return out[:2]


# ── C：清掉摘要里的元话 ───────────────────────────────────────────────
_LABEL_END = re.compile(r"(邮件|通知|往来|报告|汇总|说明|函|资料|信息|情况|事宜|沟通|公告|周报)[。.]?$|^这是一封")
_ROUTINE = re.compile(r"没有需要你处理|无需你处理|无需处理|没有需要处理")
_RISK_SUBJ = re.compile(r"停服|催缴|催款|拖欠|违约|律师函|诉讼|封禁|数据泄露|预警函|最后通知|逾期")


def first_sentence(text, n=46) -> str:
    """摘要的第一句（太长就取第一个分句）。"""
    s = re.split(r"[。；;！!]", str(text or "").strip())[0].strip()
    if len(s) > n:
        c = re.split(r"[，,]", s)[0].strip()
        s = c if (len(c) >= 14 and not c.startswith("这是一封")) else s[:n].rstrip("，, ")
    return s


_META_CLAUSE = re.compile(r"被抄送|非直接收件人|抄送给你|在抄送|抄送中|收件组|不涉及(你|你的|你们)|^\s*不涉及该(岗位|竞聘|事项|通知)|不在收件人|内容与[^，,。；;]{0,24}(职责|范围|工作)[^，,。；;]{0,8}无关|与(你|您|读者)[^，,。；;]{0,24}(无关|无直接关系)|未明确要求|没有明确要求|^\s*无需(你|您)?\s*$")


_ROLE_RX = {"key": None, "rx": None}


def _role_clause():
    """读者岗位相关的元话（「不涉及<岗位>」「与<岗位>无关」）。岗位取自 config reader.role 冒号前的部分，
    如「运维负责人：机房、网络」→ 运维负责人 / 运维；没配置 reader.role 时返回 None。"""
    role = str(load_reader().get("role") or "")
    head = re.split(r"[：:，,（(]", role, 1)[0].strip()
    domain = re.sub(r"(负责人|主管|经理|总监|工程师)$", "", head).strip()
    terms = tuple(sorted({t for t in (head, domain) if len(t) >= 2}, key=len, reverse=True))
    if terms != _ROLE_RX["key"]:
        alt = "|".join(re.escape(t) for t in terms)
        _ROLE_RX["key"] = terms
        _ROLE_RX["rx"] = re.compile(r"不涉及(%s)|与(%s)[^，,。；;]{0,24}(无关|无直接关系)" % (alt, alt)) if terms else None
    return _ROLE_RX["rx"]


def clean_meta(text, limit=None) -> str:
    """「读者」改称「你」；整个分句只是在说「被抄送 / 与你无关」的去掉（这些信息级别标签已经表达）。"""
    t = re.sub(r"读者[\u4e00-\u9fffA-Za-z0-9]{0,3}?(?=被|需|应|要|可|已|在|是|的|不|没|将|会|有|，|,|。|；|\s|$)", "你", str(text or ""))
    t = t.replace("读者", "你")
    t = re.sub(r"你?作为([^，,。；;作]{1,12}?负责人|读者)[，,]\s*(不涉及|与[^，,。；;]{0,20}无关|无需|不需要|不用)[^。；;]*", "", t)      # 「你作为运维负责人，不涉及…」整句是元话
    parts = re.split(r"([，,；;。.！!])", t)
    role_rx = _role_clause()
    keep, i = [], 0
    while i < len(parts):
        seg, sep = parts[i], (parts[i + 1] if i + 1 < len(parts) else "")
        if seg.strip() and not _META_CLAUSE.search(seg) and not (role_rx and role_rx.search(seg)):
            keep.append(seg + sep)
        i += 2
    r = "".join(keep).strip().rstrip("，,；;")
    if r and limit:
        r = r[:limit]
    return r


# ── B：链接 / 附件型邮件（正文没有实际内容）─────────────────────────────
_CUT_BOILER = re.compile(r"\*{3,}\s*[^*]{0,20}(自动发送|请勿回复)|(该|本|此)邮件由系统自动发送|本邮件及其附件含有|This e-mail and its attachments", re.I)
_CUT_QUOTE = re.compile(r"-{3,}\s*(Original Message|原始邮件|转发邮件)|(^|\s)(发件人|From)\s*[:：]|在 ?\d{4}.{0,40}写道|On .{5,80}wrote:", re.I)
_CUE = re.compile(r"点此|点击|详情|请查看|请查阅|请登录|请前往|附件|查收|链接|http", re.I)
_DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".txt", ".html", ".htm", ".zip")
_STOP = ("请查看 请查阅 请登录 请前往 请点击 点此查看详情 点击查看详情 查看详情 点此前往处理 请您处理 请您关注 详情请见 公告通知 通知 公告 发布 "
         "亲爱的同事 亲爱的 您好 大家好 附件 见附件 请查收 系统 自动发送 谢谢 请勿回复 链接 如下 以下 提交 已发布 摘要 邮件 提醒 告知 已 的 了")


def _bg(s):
    s = re.sub(r"[\W_]+", "", str(s or "").lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


_STOPBG = set().union(*[_bg(w) for w in _STOP.split()])


def pointer_info(email: dict, att_info=None):
    """正文只是一个指针（链接 / 附件 / 空），本身没有可总结的内容 → 返回说明；否则 None。
    排除：转发 / 回复里带了被引用的历史；正文里出现主题里没有的新数字（如「3 块云盘」「02.10→02.20」）；没有任何指针线索的短消息。"""
    if att_info and any(a.get("status") == "read" and a.get("chars", 0) >= 60 for a in att_info):
        return None                                   # 附件文字已经读到并交给 AI：正文是指针也没关系
    body = (email.get("body") or "").strip()
    if not body and email.get("html_body"):
        body = re.sub(r"<[^>]+>", " ", email["html_body"])
    body = " ".join(body.split())
    q = _CUT_QUOTE.search(body)
    if q and q.start() > 0 and len(body) - q.start() > 80:
        return None                                   # 带被引用的历史：内容在下面
    m = _CUT_BOILER.search(body)
    core = body[:m.start()] if m and m.start() >= 0 else body
    if q and q.start() > 0:
        core = core[:q.start()]
    core = re.sub(r"https?://\S+", "", core).strip(" -*\t")
    sig = re.search(r"={5,}|示例科技股份有限公司|Example Technology", core)
    if sig and len(core) - sig.start() < 250:         # 签名块 / 分隔线之后只是页脚
        core = core[:sig.start()].strip(" -*\t")
    core = re.sub(r"^(祝[^，。！!：:]{0,10}[：:]?[^，。！!]{0,10}[！!。]?)\s*", "", core).strip()
    subj = re.sub(r"^\s*(\[外部邮件[^\]]*\]\s*)?((re|回复|答复|fw|fwd|转发)\s*[:：]\s*)*", "", str(email.get("subject") or ""), flags=re.I)
    docs = [n for n in _attach_names(email) if n.lower().endswith(_DOC_EXT)]
    has_link = bool(re.search(r"https?://", str(email.get("html_body") or "") + str(email.get("body") or "")))
    novel = _bg(core) - _bg(subj) - _STOPBG
    nums = set(re.findall(r"\d+(?:\.\d+)?", core)) - set(re.findall(r"\d+(?:\.\d+)?", subj))
    if len(core) >= 500 or nums:
        return None
    empty = len(core) <= 12 and not _CUE.search(core) and len(novel) <= 4      # 只是主题的回声（「云监控邮件周报」）才算空；「请周五前反馈」这种真话不算
    if not (empty or ((_CUE.search(core) or docs or has_link) and len(novel) <= 12)):
        return None
    if docs and att_info:
        why = {a.get("name"): a.get("reason") for a in att_info}
        msg = "正文没有具体内容，内容在附件里，但附件没能读取：%s。" % "；".join("%s（%s）" % (d[:24], why.get(d[:80]) or "未读取") for d in docs[:2])
    elif docs:
        msg = "正文没有具体内容，内容在附件里（%s），目前没有读取附件。" % "、".join(d[:24] for d in docs[:2])
    elif empty and not has_link:
        msg = "正文几乎是空的，没有可总结的内容。"
    else:
        msg = "正文没有具体内容，只有链接，详情要到原系统里查看。"
    return {"kind": "attachment" if docs else ("empty" if empty and not has_link else "link"), "docs": docs[:3], "core": core, "subject": subj, "message": msg}


def adds_nothing(text: str, info: dict) -> bool:
    """这段摘要是否只是把主题 / 正文指针重说了一遍（新信息很少）。"""
    return len(_bg(text) - _bg(info.get("subject")) - _bg(info.get("core")) - _STOPBG) <= 8


# ── 映射回旧字段 ──────────────────────────────────────────────────────
def to_legacy(v2: dict) -> dict:
    lvl, acts = v2["level"], v2["actions"]
    lens = (v2.get("lens") or {})
    todo_like = [a for a in acts if a["kind"] in ("todo", "reply")]
    actions, seen = [], set()

    def add(t):
        k = "".join(str(t).split()).lower()
        if t and k not in seen:
            seen.add(k)
            actions.append({"text": t, "owner": ""})
    for a in todo_like:
        add(a["label"])
    mm = [m for m in ((lens.get("meeting_minutes") or {}).get("my_actions") or []) if m.get("owner_match") in ("named", "maybe")]
    for m in mm:
        add(m["text"])
    dated = [(a["date"], a.get("evidence") or "") for a in acts if a.get("date") and a["kind"] in ("todo", "calendar")] \
        + [(m["due"], m.get("evidence") or "") for m in mm if m.get("due")]
    due, due_src = min(dated) if dated else ("", "")
    drafts, dseen = [], set()
    if lvl != "archive":
        for q in v2["questions"]:
            if q["draft"] not in dseen:
                dseen.add(q["draft"]); drafts.append({"tone": "追问", "text": q["draft"]})
        for a in acts:
            if a["kind"] == "reply" and a.get("draft") and a["draft"] not in dseen:
                dseen.add(a["draft"]); drafts.append({"tone": "回复", "text": a["draft"]})
    link = next((a["url"] for a in acts if a["kind"] == "open_link" and a.get("url")), "")
    period = ((lens.get("weekly_report") or {}).get("period")) or ""
    return {"priority": LEVEL_TO_PRIORITY[lvl], "headline": v2["headline"],
            "summary": v2.get("digest") or v2["headline"],
            "points": v2.get("points") or [g["what"] for g in v2["gaps"]],
            "todos": [a["label"] for a in todo_like][:3], "drafts": drafts[:3], "actions": actions[:6],
            "due": due or "", "due_src": due_src if due else "",
            "next": todo_like[0]["label"] if todo_like else "", "link": link, "meta": period}
