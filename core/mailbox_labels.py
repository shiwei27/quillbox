"""邮箱来源标签：规则解析与校验（后端与前端共用同一套语义）。

为什么是"规则"而不是"账户→标签"的死映射
------------------------------------------
各家公司的共同点是都有内网/外网邮箱（按域名就能区分），但除此之外还会有公共邮箱、
告警邮箱、职能邮箱之类需要各自定义（例如 noc@partner.example）。所以规则同时支持：

  - ``accounts``：精确账户列表 —— 用来给公共邮箱单独打标；
  - ``domains``：域名后缀列表 —— 内外网这类"整域一个标签"的情况一次配好，
    以后新增同域账户自动带标签，不必回来改配置。

匹配顺序固定为 **精确账户优先，其次域名**，与规则的排列顺序无关；这样把
``noc@partner.example`` 单独标成 NOC 时，不会被"@partner.example → 外网"那条抢走。

标签只从 ``emails.account`` 派生，不落数据库列：account 本身已唯一标识邮箱，
派生方案零迁移、零回填，新邮件天生带标签。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# 可选配色：只允许白名单里的键，前端按键取预置 class，避免任意 CSS 注入
PALETTE = {
    "blue":    "蓝",
    "amber":   "琥珀",
    "rose":    "玫红",
    "emerald": "绿",
    "violet":  "紫",
    "cyan":    "青",
    "orange":  "橙",
    "slate":   "灰",
}
DEFAULT_COLOR = "slate"

# 未配置 mailbox_labels 时不打任何标签；规则由用户在设置页或 config.yaml 里定义，例如：
#   mailbox_labels:
#     rules:
#       - {name: 内网, color: blue,  domains: [corp.example]}
#       - {name: NOC,  color: rose,  accounts: [noc@partner.example]}
#       - {name: 外网, color: amber, domains: [partner.example]}
DEFAULT_RULES: List[Dict] = []


def _norm_list(value) -> List[str]:
    """把 accounts / domains 归一化成去重小写列表，兼容逗号分隔的字符串输入。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("，", ",").replace(";", ",").split(",")
    else:
        try:
            items = list(value)
        except TypeError:
            return []
    out: List[str] = []
    for it in items:
        s = str(it or "").strip().lower().lstrip("@")
        if s and s not in out:
            out.append(s)
    return out


def validate_rules(raw) -> Tuple[List[Dict], List[str]]:
    """校验并归一化规则列表，返回 (规则, 错误列表)。错误非空时调用方不应保存。"""
    errors: List[str] = []
    if raw is None:
        return [], ["缺少 rules"]
    if not isinstance(raw, (list, tuple)):
        return [], ["rules 必须是数组"]

    rules: List[Dict] = []
    seen_names = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append("第 %d 条不是对象" % (i + 1))
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            errors.append("第 %d 条缺少名称" % (i + 1))
            continue
        if len(name) > 12:
            errors.append("第 %d 条名称过长（上限 12 字）：%s" % (i + 1, name))
            continue
        if name in seen_names:
            errors.append("标签名重复：%s" % name)
            continue
        color = str(item.get("color") or DEFAULT_COLOR).strip().lower()
        if color not in PALETTE:
            errors.append("第 %d 条颜色不在可选范围：%s" % (i + 1, color))
            continue
        accounts = _norm_list(item.get("accounts"))
        domains = _norm_list(item.get("domains"))
        if not accounts and not domains:
            errors.append("第 %d 条（%s）既没有账户也没有域名，不会命中任何邮件"
                          % (i + 1, name))
            continue
        seen_names.add(name)
        rules.append({"name": name, "color": color,
                      "accounts": accounts, "domains": domains})
    return rules, errors


def load_rules(cfg: dict) -> List[Dict]:
    """从 config 读规则；缺失或全非法时回落到种子规则（只读，不写盘）。"""
    section = (cfg or {}).get("mailbox_labels") or {}
    raw = section.get("rules")
    if not raw:
        return [dict(r) for r in DEFAULT_RULES]
    rules, _errs = validate_rules(raw)
    return rules or [dict(r) for r in DEFAULT_RULES]


def resolve(account: str, rules: List[Dict]) -> Optional[Dict]:
    """按 账户精确匹配 → 域名后缀匹配 的顺序返回命中的规则；都不中返回 None。"""
    acct = str(account or "").strip().lower()
    if not acct:
        return None
    for r in rules or []:
        if acct in (r.get("accounts") or []):
            return r
    domain = acct.rsplit("@", 1)[-1] if "@" in acct else ""
    if not domain:
        return None
    for r in rules or []:
        for d in r.get("domains") or []:
            if domain == d or domain.endswith("." + d):
                return r
    return None
