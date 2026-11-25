#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""规则条件匹配回归（离线，不连 LLM、不写库）。

钉死 2026-09-07 修的缺陷：`from` 字段是「显示名 + 邮箱」拼串、
`to` 是 recipients 的字符串化，导致 is / starts_with / ends_with
这类精确算符对地址字段永不命中，规则静默失效。

用法：python test_rule_match.py
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import sys

sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.ai_engine import AIEngine

FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else " -> %s" % (extra,)))
    if not cond:
        FAILS.append(name)


def engine(rules):
    ai = AIEngine.__new__(AIEngine)      # 绕过 __init__，不建 LLM 连接
    ai.prompts = {"rules": rules}
    return ai


MAIL = {
    "subject": "【示例云】资源即将释放",
    "body": "正文内容",
    "sender": "示例云",
    "sender_email": "system@notice.examplecloud.example",
    "recipients": [{"name": "", "email": "alice@example.com",
                    "type": "to"},
                   {"name": "", "email": "boss@example.com",
                    "type": "cc"}],
    "has_attachment": 0,
    "folder": "收件箱",
    "account": "alice@example.com",
}


def rule(conds, mode="all", name="R", enabled=True, ai_match=""):
    return {"name": name, "enabled": enabled, "match_mode": mode,
            "conditions": conds, "ai_match": ai_match,
            "actions": {"mark_read": True}}


def hit(conds, mode="all", mail=None):
    ai = engine([rule(conds, mode)])
    return bool(ai._match_rule(mail or MAIL))


print("=== 1. 缺陷主体：from is <纯地址> 必须命中 ===")
check("from is 纯地址",
      hit([{"field": "from", "op": "is",
            "value": "system@notice.examplecloud.example"}]))
check("大小写不敏感",
      hit([{"field": "from", "op": "is",
            "value": "SYSTEM@NOTICE.EXAMPLECLOUD.EXAMPLE"}]))
check("带前后空格也命中",
      hit([{"field": "from", "op": "is",
            "value": "  system@notice.examplecloud.example  "}]))

print("=== 2. 既有 contains 行为不得回归 ===")
check("from contains 显示名", hit([{"field": "from", "op": "contains",
                                    "value": "示例云"}]))
check("from contains 域名", hit([{"field": "from", "op": "contains",
                                  "value": "@notice.examplecloud.example"}]))
check("subject contains", hit([{"field": "subject", "op": "contains",
                                "value": "资源即将释放"}]))
check("不相关值不命中", not hit([{"field": "from", "op": "contains",
                                  "value": "tencent.com"}]))

print("=== 3. starts_with / ends_with 对地址生效 ===")
check("starts_with 地址前缀",
      hit([{"field": "from", "op": "starts_with", "value": "system@"}]))
check("ends_with 域名",
      hit([{"field": "from", "op": "ends_with", "value": "examplecloud.example"}]))
check("starts_with 显示名（整串候选）",
      hit([{"field": "from", "op": "starts_with", "value": "示例云"}]))

print("=== 4. 否定算符：必须对所有候选成立（否则形同虚设）===")
check("not_is 同一地址 -> 不命中",
      not hit([{"field": "from", "op": "not_is",
                "value": "system@notice.examplecloud.example"}]))
check("not_is 其他地址 -> 命中",
      hit([{"field": "from", "op": "not_is",
            "value": "other@example.com"}]))
check("not_contains 命中的域名 -> 不命中",
      not hit([{"field": "from", "op": "not_contains",
                "value": "examplecloud.example"}]))
check("not_contains 不存在的串 -> 命中",
      hit([{"field": "from", "op": "not_contains", "value": "zzz-nope"}]))

print("=== 5. to（收件人）字段同样可用精确算符 ===")
check("to is 收件人地址",
      hit([{"field": "to", "op": "is",
            "value": "alice@example.com"}]))
check("to is 抄送地址",
      hit([{"field": "to", "op": "is", "value": "boss@example.com"}]))
check("to is 无关地址 -> 不命中",
      not hit([{"field": "to", "op": "is", "value": "nobody@x.com"}]))

print("=== 6. match_mode ===")
check("all: 两条都真才命中",
      hit([{"field": "from", "op": "is",
            "value": "system@notice.examplecloud.example"},
           {"field": "subject", "op": "contains", "value": "示例云"}]))
check("all: 一条假就不命中",
      not hit([{"field": "from", "op": "is",
                "value": "system@notice.examplecloud.example"},
               {"field": "subject", "op": "contains", "value": "不存在"}]))
check("any: 一条真即命中",
      hit([{"field": "from", "op": "is", "value": "nope@x.com"},
           {"field": "subject", "op": "contains", "value": "示例云"}],
          mode="any"))

print("=== 7. 其他既有语义不受影响 ===")
ai = engine([rule([{"field": "from", "op": "is",
                    "value": "system@notice.examplecloud.example"}],
                  enabled=False)])
check("enabled=False 的规则被跳过", ai._match_rule(MAIL) is None)

check("空值条件被忽略（无有效条件不命中）",
      not hit([{"field": "from", "op": "is", "value": "   "}]))
check("has_attachment=0 对 '有' 不命中",
      not hit([{"field": "has_attachment", "op": "is", "value": "是"}]))
m2 = dict(MAIL, has_attachment=1)
check("has_attachment=1 对 '是' 命中",
      hit([{"field": "has_attachment", "op": "is", "value": "是"}], mail=m2))
check("folder 字段可用",
      hit([{"field": "folder", "op": "is", "value": "收件箱"}]))

print("=== 8. 首个命中优先，返回带 actions 的规则 ===")
ai = engine([
    rule([{"field": "from", "op": "is", "value": "nope@x.com"}], name="A"),
    rule([{"field": "from", "op": "is",
           "value": "system@notice.examplecloud.example"}], name="B"),
])
got = ai._match_rule(MAIL)
check("返回 B", (got or {}).get("name") == "B", (got or {}).get("name"))
check("带 actions", bool((got or {}).get("actions")))

print()
if FAILS:
    print("RESULT FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    raise SystemExit(1)
print("RESULT ALL_PASS")
