#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""watchdog 卡死检测回归（离线，不起进程、不杀进程）。

钉死一个缺陷：原来两个拉起分支都要求 `not _proc_running(...)`，
进程活着但功能已死（例如 server 死锁不再响应）时永不恢复。

用法：python test_watchdog_stuck.py
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import io
import sys

sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import watchdog as W

FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else " -> %s" % (extra,)))
    if not cond:
        FAILS.append(name)


print("=== 1. 结构与阈值 ===")
src = io.open(_os.path.join(ROOT, 'watchdog.py'), encoding="utf-8").read()
check("导入了 re", "\nimport re\n" in src)
check("有 _stuck_seconds", hasattr(W, "_stuck_seconds"))
check("有 _kill_script", hasattr(W, "_kill_script"))
check("SERVER_STUCK_SEC=900", W.SERVER_STUCK_SEC == 900, W.SERVER_STUCK_SEC)
check("DAEMON_STUCK_SEC=300", W.DAEMON_STUCK_SEC == 300, W.DAEMON_STUCK_SEC)
check("阈值 > 冷启动上限(6min=360s)", W.SERVER_STUCK_SEC > 360)
check("卡死判定在拉起分支之前",
      src.index("判定卡死") < src.index("server 未运行，拉起"))

print("=== 2. _stuck_seconds：条件为真时累计 ===")
W._stuck_since.clear()
check("首次观察返回 0", W._stuck_seconds("k", True, 1000.0) == 0.0)
check("10s 后返回 10", W._stuck_seconds("k", True, 1010.0) == 10.0)
check("900s 后返回 900", W._stuck_seconds("k", True, 1900.0) == 900.0)

print("=== 3. 条件转假立即清零（不能残留旧计时）===")
check("转假返回 0", W._stuck_seconds("k", False, 1910.0) == 0.0)
check("内部状态已删除", "k" not in W._stuck_since)
check("再次为真重新从 0 计", W._stuck_seconds("k", True, 2000.0) == 0.0)
check("重新计时基于新起点", W._stuck_seconds("k", True, 2005.0) == 5.0)

print("=== 4. 多个 key 互不干扰 ===")
W._stuck_since.clear()
W._stuck_seconds("server", True, 100.0)
W._stuck_seconds("daemon", True, 150.0)
check("server 计时独立", W._stuck_seconds("server", True, 200.0) == 100.0)
check("daemon 计时独立", W._stuck_seconds("daemon", True, 200.0) == 50.0)
W._stuck_seconds("server", False, 210.0)
check("清 server 不影响 daemon",
      W._stuck_seconds("daemon", True, 210.0) == 60.0)

print("=== 5. 时钟回拨不产生负数 ===")
W._stuck_since.clear()
W._stuck_seconds("k", True, 5000.0)
check("回拨后为 0 而非负数", W._stuck_seconds("k", True, 4000.0) == 0.0)

print("=== 6. 判定门槛：模拟 server 卡死时序 ===")
W._stuck_since.clear()
# 场景：进程在、端口不通，逐步推进时间
fired_at = None
for t in range(0, 1200, 20):
    s = W._stuck_seconds("server", True, float(t))
    if s > W.SERVER_STUCK_SEC:
        fired_at = t
        break
check("在 900s 之后触发", fired_at is not None and fired_at > 900,
      str(fired_at))
check("不早于 920s 触发（20s 巡检粒度）",
      fired_at is not None and fired_at <= 940, str(fired_at))

print("=== 7. 正常冷启动不应被判卡死 ===")
W._stuck_since.clear()
# 冷启动 6 分钟后端口通了 -> 条件转假
for t in range(0, 360, 20):
    W._stuck_seconds("server", True, float(t))
s = W._stuck_seconds("server", False, 360.0)   # 端口通了
check("6 分钟内起来则清零", s == 0.0)
check("未达阈值不会触发", 360 < W.SERVER_STUCK_SEC)

print("=== 8. daemon 心跳过期叠加判定 ===")
W._stuck_since.clear()
fired = None
for t in range(0, 600, 20):
    s = W._stuck_seconds("daemon", True, float(t))
    if s > W.DAEMON_STUCK_SEC:
        fired = t
        break
check("daemon 在 300s 后触发", fired is not None and fired > 300, str(fired))
check("总恢复时延 = 心跳阈值 + 判定阈值",
      W.DAEMON_STALE_SEC + W.DAEMON_STUCK_SEC == 20 * 60 + 300)

print("=== 9. _kill_script 不接受空句柄就崩 ===")
try:
    # 传 None + 一个不存在的脚本名：不应抛异常，返回空列表
    r = W._kill_script(None, "definitely_not_a_real_script_zzz.py")
    check("空句柄 + 不存在脚本 -> 返回列表", isinstance(r, list), repr(r))
    check("没有误杀", r == [], repr(r))
except Exception as e:
    check("不抛异常", False, repr(e))

print()
if FAILS:
    print("RESULT FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    raise SystemExit(1)
print("RESULT ALL_PASS")
