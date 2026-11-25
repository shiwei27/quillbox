"""邮箱标签规则核心自检（离线，不碰配置与数据库）。

用法：python test_mailbox_labels_core.py
"""
from __future__ import annotations

import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print("  %s %s%s" % ("✓" if cond else "✗", label, ("  " + detail) if detail else ""))
    if not cond:
        FAILED.append(label)


def main() -> int:
    from core.mailbox_labels import (DEFAULT_RULES, PALETTE, load_rules, resolve,
                                     validate_rules)

    print("=== 1. 示例规则 ===")
    check("未配置时不内置任何规则", DEFAULT_RULES == [], str(DEFAULT_RULES))
    example = [{"name": "内网", "color": "blue", "accounts": [], "domains": ["example.com", "example.net"]},
               {"name": "NOC", "color": "rose", "accounts": ["noc@example.org"], "domains": []},
               {"name": "外网", "color": "amber", "accounts": [], "domains": ["example.org"]}]
    rules, errs = validate_rules(example)
    check("示例规则自身合法", not errs, str(errs))
    check("三条规则", len(rules) == 3, str([r["name"] for r in rules]))

    print("=== 2. 匹配优先级（精确账户 > 域名）===")
    check("noc 命中 NOC 而不是外网",
          resolve("noc@example.org", rules)["name"] == "NOC",
          resolve("noc@example.org", rules)["name"])
    check("普通 corp-b 账户命中外网",
          resolve("alice@example.org", rules)["name"] == "外网")
    check("corp-a 账户命中内网",
          resolve("alice@example.com", rules)["name"] == "内网")
    check("大小写/空白容错",
          resolve("  NOC@Example.org ", rules)["name"] == "NOC")
    check("子域名也算命中（mail.example.org）",
          resolve("a@mail.example.org", rules)["name"] == "外网")
    check("外部域名不命中", resolve("x@gmail.com", rules) is None)
    check("空账户不命中", resolve("", rules) is None and resolve(None, rules) is None)

    # 顺序无关：把"外网"放到 NOC 之前，noc 仍应命中 NOC
    reordered = [rules[0], rules[2], rules[1]]
    check("规则顺序颠倒后 noc 仍命中 NOC",
          resolve("noc@example.org", reordered)["name"] == "NOC")

    print("=== 3. 校验规则 ===")
    ok, e = validate_rules([{"name": "公共", "color": "emerald", "accounts": "a@x.com, b@x.com"}])
    check("逗号分隔字符串可用", not e and ok[0]["accounts"] == ["a@x.com", "b@x.com"], str(ok))
    ok, e = validate_rules([{"name": "公共", "color": "emerald", "domains": ["@x.com", "X.COM"]}])
    check("域名去 @ 并去重小写", not e and ok[0]["domains"] == ["x.com"], str(ok))
    _, e = validate_rules([{"name": "", "color": "blue", "domains": ["x.com"]}])
    check("空名称被拒", bool(e), str(e))
    _, e = validate_rules([{"name": "x", "color": "hotpink", "domains": ["x.com"]}])
    check("非白名单颜色被拒", bool(e), str(e))
    _, e = validate_rules([{"name": "空规则", "color": "blue"}])
    check("既无账户也无域名被拒", bool(e), str(e))
    _, e = validate_rules([{"name": "A", "color": "blue", "domains": ["x.com"]},
                           {"name": "A", "color": "rose", "domains": ["y.com"]}])
    check("重名被拒", bool(e), str(e))
    _, e = validate_rules([{"name": "很长很长很长很长很长很长很长", "color": "blue",
                            "domains": ["x.com"]}])
    check("超长名称被拒", bool(e), str(e))
    _, e = validate_rules("not a list")
    check("非数组被拒", bool(e), str(e))

    print("=== 4. 从 config 读取 ===")
    check("缺失时不打标签", load_rules({}) == [])
    custom = {"mailbox_labels": {"rules": [
        {"name": "公共邮箱", "color": "emerald", "accounts": ["ops@x.com"], "domains": []}]}}
    got = load_rules(custom)
    check("自定义规则生效", len(got) == 1 and got[0]["name"] == "公共邮箱", str(got))
    check("全非法时不打标签", load_rules({"mailbox_labels": {"rules": [{"name": ""}]}}) == [])

    print("=== 5. 配色白名单 ===")
    check("至少 8 种可选色", len(PALETTE) >= 8, str(sorted(PALETTE)))

    print("\n结果：%s" % ("全部通过" if not FAILED else "失败 %d 项 -> %s" % (len(FAILED), FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
