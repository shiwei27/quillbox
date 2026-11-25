#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中文按单字全文搜索（emails_fts2）回归。用法：python test_fts_cjk.py [模块路径]
默认测本仓库的 core/db_manager.py；部署前传候选文件。
1. 临时库：子串召回、多词 AND / OR 放宽、英文与邮箱、增量同步（插入/改正文/删除）、未变更不入队、legacy 回退开关；
2. 本机真实库副本：15 个中文词召回 ⊇ 逐行子串真值（subject/sender/sender_email/body），且只返回未删除邮件。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import importlib.util, os, sqlite3, sys, tempfile, shutil, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ROOT)
MOD = sys.argv[1] if len(sys.argv) > 1 else _os.path.join(ROOT, 'core', 'db_manager.py')
sp = importlib.util.spec_from_file_location("dbm_under_test", MOD); M = importlib.util.module_from_spec(sp); sp.loader.exec_module(M)
DBManager = M.DBManager
fail = []
def check(label, got, want=True):
    ok = want(got) if callable(want) else got == want
    print("  %s %-46s got=%r" % ("ok  " if ok else "FAIL", label, got if not isinstance(got, (list, set)) or len(got) < 8 else "%d 项" % len(got)))
    if not ok: fail.append(label)
ids = lambda rows: [e["id"] for e in rows]
td = tempfile.mkdtemp(prefix="test_fts_cjk_")
try:
    print("=== 1. 临时库 ===")
    db = DBManager(os.path.join(td, "t.db"))
    def put(i, subj, body, ts, sender="张三", se="z@example.com"):
        db.upsert_email({"id": i, "subject": subj, "body": body, "sender": sender, "sender_email": se, "received_timestamp": 1790000000 + ts, "folder": "收件箱", "account": "acc"})
    put("a", "【通知】请您参加孔明16发起的会议", "地点 A1-01", 3)
    put("b", "周报", "本周示例部周会照常召开，会 议纪要见附件", 2)
    put("c", "示例云按量付费云盘创建成功提醒", "您有3块云盘创建成功 ops@example.org", 1)
    put("d", "Alarm: disk usage high", "server alarm triggered", 0, sender="Monitor", se="monitor@examplecloud.example")
    check("「会议」子串命中（旧引擎只认整段）", sorted(ids(db.search_by_keyword("会议"))), ["a", "b"])
    check("「周会」", ids(db.search_by_keyword("周会")), ["b"])
    check("单字「孔」", ids(db.search_by_keyword("孔")), ["a"])
    check("多词 AND：云盘 创建", ids(db.search_by_keyword("云盘 创建")), ["c"])
    check("AND 无结果放宽 OR：云盘 周会", sorted(ids(db.search_by_keyword("云盘 周会"))), ["b", "c"])
    check("英文词", ids(db.search_by_keyword("alarm")), ["d"])
    check("邮箱", ids(db.search_by_keyword("ops@example.org")), ["c"])
    check("发件人中文名", sorted(ids(db.search_by_keyword("张三"))), ["a", "b", "c"])
    put("e", "预算评审", "Q4 预算", 5)
    check("新邮件入队并在搜索前同步", ids(db.search_by_keyword("预算")), ["e"])
    # upsert 冲突时只刷新 subject（UPSERT-KEEP-LOCAL），正文改动用直接 SQL 模拟「其他写库方」
    c = db._get_conn(); c.execute("UPDATE emails SET subject='评审安排', body='下周二' WHERE id='e'"); c.commit()
    check("改内容后旧词消失", ids(db.search_by_keyword("预算")), [])
    check("改内容后新词出现", ids(db.search_by_keyword("下周二")), ["e"])
    c.execute("DELETE FROM emails WHERE id='e'"); c.commit()
    check("物理删除后不再命中", ids(db.search_by_keyword("评审")), [])
    put("a", "【通知】请您参加孔明16发起的会议", "地点 A1-01", 3)
    check("内容未变的 upsert 不入队", c.execute("SELECT COUNT(*) FROM emails_fts2_pending").fetchone()[0], 0)
    # 回归（test_message_id 发现）：刷队列前同一封邮件被 upsert 两次，旧触发器 INSERT OR IGNORE
    # 被外层 UPSERT 的冲突策略覆盖 → UNIQUE 失败、整条写入回滚
    put("f", "改名一", "x", 6); put("f", "改名二", "x", 6); put("f", "改名三", "x", 6)
    check("刷队列前连续 upsert 不报错且最终可搜", ids(db.search_by_keyword("改名三")), ["f"])
    c.execute("UPDATE emails SET deleted=1 WHERE id='b'"); c.commit()
    check("软删除的不返回", ids(db.search_by_keyword("周会")), [])
    check("映射表与 FTS 行数一致", c.execute("SELECT (SELECT COUNT(*) FROM emails_fts2)=(SELECT COUNT(*) FROM emails_fts2_ids)").fetchone()[0], 1)
    # 旧 emails_fts 引擎已删除：表与三个触发器不存在，fts_engine=legacy 不再生效
    check("旧 emails_fts 表与触发器已删除", c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('emails_fts','emails_fts_insert','emails_fts_update','emails_fts_delete')").fetchone()[0], 0)
    _cjk = ids(db.search_by_keyword("会议"))
    db.set_sync_status("fts_engine", "legacy")
    check("fts_engine=legacy 不再生效（结果同新引擎）", ids(db.search_by_keyword("会议")), _cjk)
    db.set_sync_status("fts_engine", "cjk")
    c.close()

    _prod = _os.path.join(ROOT, "data", "emails.db")
    if not _os.path.exists(_prod):
        print("=== 2. 本机真实库副本：跳过（没有 data/emails.db，属集成测试部分）===")
    else:
        print("=== 2. 本机真实库副本：召回 ⊇ 子串真值 ===")
        cp = os.path.join(td, "copy.db")
        s = sqlite3.connect("file:" + _os.path.join(ROOT, 'data', 'emails.db') + '?mode=ro', uri=True); d = sqlite3.connect(cp); s.backup(d); s.close(); d.close()
        con = sqlite3.connect(cp); con.execute("DELETE FROM sync_status WHERE key='fts2_built'"); con.commit(); con.close()
        t = time.perf_counter(); pdb = DBManager(cp); print("  首次初始化（含回填）%.1fs" % (time.perf_counter() - t))
        pc = pdb._get_conn()
        for kw in ["会议", "周会", "告警", "示例云", "云盘", "审批", "报销", "合同", "发票", "故障", "带宽", "机房", "续费", "采购", "预算"]:
            like = "%" + kw + "%"
            truth = {r[0] for r in pc.execute("SELECT id FROM emails WHERE deleted=0 AND (subject LIKE ? OR sender LIKE ? OR sender_email LIKE ? OR body LIKE ?)", (like,) * 4)}
            t = time.perf_counter(); got = set(ids(pdb.search_by_keyword(kw, limit=100000))); dt = (time.perf_counter() - t) * 1000
            print("    %-4s 真值 %-5d 命中 %-5d %6.1fms" % (kw, len(truth), len(got), dt))
            if not truth <= got: check("召回 ⊇ 真值：" + kw, len(truth - got), 0)
        check("15 词召回全部覆盖真值", not [x for x in fail if x.startswith("召回")], True)
        t = time.perf_counter(); r = pdb.search_by_keyword("会议", limit=50); dt = (time.perf_counter() - t) * 1000
        check("「会议」取前 50 封 < 200ms", round(dt, 1), lambda x: x < 200)
        pc.close()
finally:
    shutil.rmtree(td, ignore_errors=True)
print()
if fail:
    print("RESULT FAILED(%d): %s" % (len(fail), ", ".join(fail))); sys.exit(1)
print("RESULT ALL_PASS")
