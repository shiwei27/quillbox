#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单封待办提取：去重判据（离线）+ 端点口径（对运行中的 server）。

Part A 用临时库测 todo_exists 的语义，不碰生产库。
Part B 打真端点，验证"优先用已落库 action_items、重复调用不再新增"，
       结束时按 id 差集把本次造出的待办删干净。

为什么 Part B 要用差集清理：cached 路径只回 count 与 contents、不回 id
（import_action_items 本身只回条数），所以用「调用前后 /api/todos 的 id 差集」
反推本次新增了哪些，比要求端点多回一层数据更省事，也更贴近真实调用。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import json
import os
import sys
import tempfile
import urllib.request

BASE = "http://127.0.0.1:8765"
sys.path.insert(0, ROOT)

fail = []


def check(label, got, want):
    ok = got == want
    print("  %s %-42s got=%r want=%r" % ("OK  " if ok else "FAIL", label, got, want))
    if not ok:
        fail.append(label)


def api(method, path, body=None, timeout=180):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else None


# ══════════════════════════════════════════════════════════════════
print("=== Part A：todo_exists 语义（临时库，离线）===")
from core.db_manager import DBManager  # noqa: E402

tmp = tempfile.mkdtemp(prefix="quillbox_todo_")
db = DBManager(os.path.join(tmp, "t.db"))

db.add_todo("甲事项", source_email_id="mail-1")
check("同邮件同内容 -> True", db.todo_exists("甲事项", "mail-1"), True)
check("同邮件不同内容 -> False", db.todo_exists("乙事项", "mail-1"), False)
check("换邮件同内容 -> False", db.todo_exists("甲事项", "mail-2"), False)
check("首尾空白应被忽略", db.todo_exists("  甲事项  ", "mail-1"), True)
check("source 为 None 时不误命中", db.todo_exists("甲事项", None), False)
db.add_todo("无源事项")
check("无源待办可被查到", db.todo_exists("无源事项", None), True)

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════
print("\n=== Part B：/extract-todos 口径（对运行中的 server）===")
import sqlite3  # noqa: E402
row = None
if not _os.path.exists(_os.path.join(ROOT, "data", "emails.db")):
    print("  没有 data/emails.db（全新检出），Part B 跳过")
else:
    con = sqlite3.connect("file:" + _os.path.join(ROOT, 'data', 'emails.db') + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("""
        SELECT id, subject, action_items FROM emails
        WHERE deleted=0 AND action_items IS NOT NULL AND action_items != '[]'
          AND LENGTH(action_items) > 10
        ORDER BY received_timestamp DESC LIMIT 1
    """).fetchone()
    con.close()
if not row:
    print("  库里没有带 action_items 的邮件，Part B 跳过")
else:
    eid = row["id"]
    cached_items = json.loads(row["action_items"])
    print("  样本:", (row["subject"] or "")[:46])
    print("  已落库 action_items:", len(cached_items), "条")

    ids_before = {t["id"] for t in api("GET", "/api/todos")}
    created = []
    try:
        import time
        t0 = time.time()
        r1 = api("POST", "/api/emails/%s/extract-todos" % eid)
        d1 = time.time() - t0
        print("  第 1 次：%.2fs %s" % (d1, json.dumps(
            {k: r1.get(k) for k in ("ok", "count", "source", "considered")},
            ensure_ascii=False)))
        ids_mid = {t["id"] for t in api("GET", "/api/todos")}
        created += sorted(ids_mid - ids_before)

        check("走 cached 路径（不调 AI）", r1.get("source"), "cached")
        check("considered = 落库条数", r1.get("considered"), len(cached_items))
        check("耗时应远小于 AI 调用(<1s)", d1 < 1.0, True)

        t0 = time.time()
        r2 = api("POST", "/api/emails/%s/extract-todos" % eid)
        d2 = time.time() - t0
        print("  第 2 次：%.2fs %s" % (d2, json.dumps(
            {k: r2.get(k) for k in ("ok", "count", "source", "considered")},
            ensure_ascii=False)))
        ids_after = {t["id"] for t in api("GET", "/api/todos")}
        created += sorted(ids_after - ids_mid)

        check("第 2 次仍走 cached", r2.get("source"), "cached")
        check("第 2 次不再新增（去重生效）", r2.get("count"), 0)
        check("第 2 次确实没多出待办行", len(ids_after - ids_mid), 0)
        check("considered 仍如实汇报", r2.get("considered"), len(cached_items))

        # 内容应当来自 action_items，而不是新问 AI 得到的别的东西
        check("contents 与落库内容一致",
              sorted(r1.get("contents") or []),
              sorted(str(x).strip() for x in cached_items))
    finally:
        print("\n  === 清理本次新增的待办 ===")
        for tid in created:
            try:
                api("DELETE", "/api/todos/%d" % tid)
                print("    删除", tid)
            except Exception as e:
                print("    删除 %s 失败 %s" % (tid, repr(e)[:60]))
        print("    共清理", len(created), "条")
        left = {t["id"] for t in api("GET", "/api/todos")}
        check("待办表已回到调用前状态", left == ids_before, True)

print()
print("RESULT", "ALL_PASS" if not fail else "FAILED: " + ", ".join(fail))
sys.exit(0 if not fail else 1)
