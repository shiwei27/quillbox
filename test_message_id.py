"""message_id 地基自检：全部在临时库上做，不触碰 data/emails.db。

覆盖：迁移建列与索引、入库归一化、跨形态查重、缺失统计与回填、upsert 不误擦、
与 POP3 后端归一化口径一致。

用法：python test_message_id.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print("  %s %s%s" % ("✓" if cond else "✗", label, ("  " + detail) if detail else ""))
    if not cond:
        FAILED.append(label)


def main() -> int:
    from core.db_manager import DBManager

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="quillbox_msgid_"))
    try:
        db = DBManager(str(tmpdir / "t.db"))

        print("=== 1. 迁移：列与索引 ===")
        with db._conn() as conn:                                  # noqa: SLF001
            cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
            idx = {r[1] for r in conn.execute("PRAGMA index_list(emails)")}
        check("emails 表已有 message_id 列", "message_id" in cols)
        check("已建 idx_message_id 索引", "idx_message_id" in idx, str(sorted(idx)))

        print("=== 2. 入库归一化 ===")
        base = {
            "subject": "s1", "body": "b", "html_body": "", "sender": "n",
            "sender_email": "a@b.com", "recipients": [], "time": "2026-09-03 10:00:00+00:00",
            "received_timestamp": 1000.0, "unread": True, "folder": "收件箱",
            "account": "alice@example.com", "has_attachment": False, "attachments": [],
        }
        r1 = dict(base, id="outlook-entryid-1", message_id="<ABC@Example.com>")
        check("插入返回新增", db.insert_email_if_new(r1) is True)
        with db._conn() as conn:                                  # noqa: SLF001
            got = conn.execute("SELECT message_id FROM emails WHERE id=?", (r1["id"],)).fetchone()[0]
        check("存库已归一化（去尖括号+小写）", got == "abc@example.com", repr(got))

        print("=== 3. 跨形态查重 ===")
        check("按小写裸值命中", db.find_id_by_message_id("abc@example.com") == r1["id"])
        check("按带尖括号大写命中", db.find_id_by_message_id("<ABC@EXAMPLE.COM>") == r1["id"])
        check("限定 account 命中", db.find_id_by_message_id(
            "abc@example.com", account="alice@example.com") == r1["id"])
        check("换 account 不误命中", db.find_id_by_message_id(
            "abc@example.com", account="alice@example.org") is None)
        check("空值不命中", db.find_id_by_message_id("") is None)
        check("不存在的值不命中", db.find_id_by_message_id("nope@x") is None)

        print("=== 4. 缺失统计与回填 ===")
        r2 = dict(base, id="outlook-entryid-2", subject="s2", message_id="")
        db.insert_email_if_new(r2)
        check("缺失计数=1", db.count_missing_message_id() == 1,
              "got=%d" % db.count_missing_message_id())
        ids = db.ids_missing_message_id(limit=10)
        check("待回填列表含该行", ids == [r2["id"]], str(ids))
        check("回填成功", db.set_message_id(r2["id"], "<DEF@x>") is True)
        check("回填后缺失计数=0", db.count_missing_message_id() == 0)
        check("回填值已归一化", db.find_id_by_message_id("def@x") == r2["id"])
        check("回填空值视为无操作", db.set_message_id(r2["id"], "  ") is False)

        print("=== 5. upsert 不误擦已有值 ===")
        db.upsert_email(dict(r1, subject="s1-改", message_id=""))
        with db._conn() as conn:                                  # noqa: SLF001
            row = conn.execute("SELECT subject, message_id FROM emails WHERE id=?",
                               (r1["id"],)).fetchone()
        check("主题已更新", row[0] == "s1-改", row[0])
        check("message_id 未被空值覆盖", row[1] == "abc@example.com", row[1])
        db.upsert_email(dict(r1, message_id="<NEW@x>"))
        with db._conn() as conn:                                  # noqa: SLF001
            row = conn.execute("SELECT message_id FROM emails WHERE id=?", (r1["id"],)).fetchone()
        check("非空值可以覆盖", row[0] == "new@x", row[0])

        print("=== 6. 与 POP3 后端归一化口径一致 ===")
        import email as _email
        from core.pop3_extractor import Pop3Extractor
        msg = _email.message_from_string("Message-ID: <Mixed.Case@Example.COM>\n\nx")
        pop3_mid = Pop3Extractor._message_id(msg)                 # noqa: SLF001
        db_mid = DBManager.norm_message_id("<Mixed.Case@Example.COM>")
        check("两侧归一化结果相同", pop3_mid == db_mid, "%r vs %r" % (pop3_mid, db_mid))

        print("\n结果：%s" % ("全部通过" if not FAILED else "失败 %d 项 -> %s" % (len(FAILED), FAILED)))
        return 0 if not FAILED else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
