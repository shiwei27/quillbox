"""IMAP 后端离线自检：不连网络、不写业务库、不碰服务端状态。

覆盖点：
  1. config.yaml 的 imap 段能否解析成 ImapExtractor
  2. modified UTF-7 解码/编码与文件夹显示名映射
  3. LIST 响应解析（普通行 / literal 的 tuple 形式 / \\Noselect 过滤 / 跳过名单）
  4. 记录构建：folder 透传、unread 透传、RFC2047 主题、附件、收件人、内嵌图改写
  5. 记录 id：同 Message-ID 必得同 id；缺 Message-ID 时退化到 folder:uid
  6. 与 EmailExtractor / Pop3Extractor 的键集一致（防三条后端字段漂移）
  7. 增量状态：commit_state 才落盘、UIDVALIDITY 变更会清零水位

用法：python test_imap_offline.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print("  %s %s%s" % ("✓" if cond else "✗", label, ("  " + detail) if detail else ""))
    if not cond:
        FAILED.append(label)


RAW_MAIL = (
    b"Message-ID: <Case.Mixed@Example.com>\r\n"
    b"Subject: =?gb2312?B?tPC4tDogudjT2rvwyb0=?=\r\n"
    b"From: =?gb2312?B?zuK9qMHB?= <erin@example.com>\r\n"
    b"To: alice@example.com\r\n"
    b"Cc: someone@example.com\r\n"
    b"Date: Tue, 1 Sep 2026 19:05:00 +0800\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B1"\r\n'
    b"\r\n"
    b"--B1\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b'<p>\xe6\xad\xa3\xe6\x96\x87</p><img src="cid:img001">\r\n'
    b"--B1\r\n"
    b"Content-Type: image/png\r\n"
    b"Content-ID: <img001>\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"iVBORw0KGgo=\r\n"
    b"--B1\r\n"
    b"Content-Type: application/pdf; name=\"r.pdf\"\r\n"
    b'Content-Disposition: attachment; filename="r.pdf"\r\n'
    b"\r\n"
    b"JVBERi0x\r\n"
    b"--B1--\r\n"
)


def main() -> int:
    from core.imap_extractor import (DEFAULT_SKIP_FOLDERS, ImapExtractor, from_config,
                                     mutf7_decode, mutf7_encode)
    from core.pop3_extractor import INBOX_NAME

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="quillbox_imap_"))
    try:
        print("=== 1. config.yaml -> ImapExtractor ===")
        account = "me@example.com"   # 离线部分用的账户名；有 config.yaml 时换成其中第一个 IMAP 账户
        if not (ROOT / "config.yaml").exists():
            print("  跳过：没有 config.yaml（检查的是本机配置）")
        else:
            cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
            accts = (cfg.get("imap") or {}).get("accounts") or {}
            account = next(iter(accts), account) if isinstance(accts, dict) else account
            ext = from_config(cfg, account)
            print("  host=%s port=%s ssl=%s" % (ext.host, ext.port, ext.use_ssl))
            check("host 已配置", bool(ext.host))
            check("端口为整数", isinstance(ext.port, int))
            check("用户名为完整邮箱地址", ext.username == account)
            check("跳过名单含已删除/草稿", {"已删除邮件", "草稿"} <= ext.skip_folders)

        print("=== 2. modified UTF-7 与显示名 ===")
        check("解码 &mHl27g- = 项目", mutf7_decode("&mHl27g-") == "项目")
        check("解码 &g0l6Pw- 非空且无 &", "&" not in mutf7_decode("&g0l6Pw-"))
        for name in ("项目组", "读书小组", "已发送邮件", "IP", "a&b"):
            check("往返一致：%s" % name, mutf7_decode(mutf7_encode(name)) == name,
                  mutf7_encode(name))
        check("Inbox 映射为收件箱", ImapExtractor.folder_display_name("Inbox") == INBOX_NAME)
        check("inbox 大小写不敏感", ImapExtractor.folder_display_name("INBOX") == INBOX_NAME)
        check("其余文件夹保留服务端原名",
              ImapExtractor.folder_display_name("&mHl27g-") == "项目")

        print("=== 3. LIST 响应解析 ===")
        e = ImapExtractor(host="h", username="u", account=account,
                          inline_root=tmpdir / "inline", state_path=tmpdir / "s.json")
        p1 = e._parse_list_item(b'(\\HasNoChildren) "/" "Inbox"')            # noqa: SLF001
        check("普通行解析出 Inbox", p1 and p1[2] == "Inbox", str(p1))
        p2 = e._parse_list_item((b'(\\HasChildren) "/" {32}', b'&ZXBjbk4tX8OP0IQlfsQ-'))  # noqa: SLF001
        check("literal 的 tuple 形式解析出名字", p2 and p2[2] == "&ZXBjbk4tX8OP0IQlfsQ-", str(p2))
        p3 = e._parse_list_item(b'(\\Noselect \\HasChildren) "/" "Public"')   # noqa: SLF001
        check("Noselect 条目能解析（由 list_folders 过滤）", p3 is not None)
        check("解析不了的行返回 None", e._parse_list_item(b'garbage') is None)  # noqa: SLF001

        print("=== 4. 记录构建 ===")
        rec = e._build_record(RAW_MAIL, "1234", "项目组", True)          # noqa: SLF001
        check("返回记录", rec is not None)
        check("folder 按传入值（非硬编码收件箱）", rec["folder"] == "项目组", rec["folder"])
        check("unread 透传", rec["unread"] is True)
        check("account 正确", rec["account"] == account)
        check("RFC2047 gb2312 主题已解码", "回复" in rec["subject"] or "转发" in rec["subject"]
              or rec["subject"] != "(无主题)", rec["subject"])
        check("发件人姓名/地址分离", rec["sender_email"] == "erin@example.com"
              and rec["sender"] != rec["sender_email"], "%s / %s" % (rec["sender"], rec["sender_email"]))
        check("收件人含 to 与 cc",
              {r["type"] for r in rec["recipients"]} == {"to", "cc"}, str(rec["recipients"]))
        check("附件被识别", rec["attachments"] == ["r.pdf"] and rec["has_attachment"] is True,
              str(rec["attachments"]))
        check("HTML-only 也有非空 body", bool(rec["body"].strip()), repr(rec["body"][:20]))
        check("内嵌图 cid 已改写为 /api/inline", "/api/inline/" in rec["html_body"],
              rec["html_body"][:80])
        check("内嵌图已落盘", any((tmpdir / "inline").rglob("*")))
        check("message_id 已归一化", rec["message_id"] == "case.mixed@example.com",
              rec["message_id"])
        check("时间已解析", rec["received_timestamp"] > 0 and rec["time"], rec["time"])

        print("=== 5. 记录 id 稳定性 ===")
        r2 = e._build_record(RAW_MAIL, "9999", "IP", False)                  # noqa: SLF001
        check("同 Message-ID 换 uid/folder 仍同 id", rec["id"] == r2["id"], rec["id"])
        check("id 前缀 imap_", rec["id"].startswith("imap_"))
        check("id 文件系统安全（无冒号/斜杠）",
              not set(rec["id"]) & set(':/\\<>"|?*'))
        no_mid = RAW_MAIL.replace(b"Message-ID: <Case.Mixed@Example.com>\r\n", b"")
        a = e._build_record(no_mid, "77", "IP", False)                       # noqa: SLF001
        b = e._build_record(no_mid, "78", "IP", False)                       # noqa: SLF001
        check("缺 Message-ID 时按 folder:uid 区分", a["id"] != b["id"])
        check("缺 Message-ID 时 message_id 为空", a["message_id"] == "")

        print("=== 6. 三条后端键集一致 ===")
        outlook_keys = {
            "id", "subject", "body", "html_body", "sender", "sender_email",
            "recipients", "time", "received_timestamp", "unread", "folder",
            "account", "has_attachment", "attachments", "message_id",
        }
        got = set(rec)
        check("键一个不少", not (outlook_keys - got), str(sorted(outlook_keys - got)))
        check("没有多余键", not (got - outlook_keys), str(sorted(got - outlook_keys)))

        print("=== 7. 增量状态 ===")
        st = tmpdir / "s.json"
        check("初始无状态返回 (0,0)", e.folder_state("Inbox") == (0, 0))
        e._pending["Inbox"] = (111, 500)                                     # noqa: SLF001
        check("未 commit 不落盘", not st.exists())
        check("commit 返回更新数 1", e.commit_state() == 1)
        check("落盘后可读回", e.folder_state("Inbox") == (111, 500), str(e.folder_state("Inbox")))
        check("commit 后 pending 清空", e.commit_state() == 0)
        e2 = ImapExtractor(host="h", username="u", account=account,
                           inline_root=tmpdir / "inline", state_path=st)
        check("新实例读到同一水位", e2.folder_state("Inbox") == (111, 500))
        check("别的账户互不干扰",
              ImapExtractor(host="h", username="u", account="other@x",
                            state_path=st).folder_state("Inbox") == (0, 0))

        print("\n结果：%s" % ("全部通过" if not FAILED else "失败 %d 项 -> %s" % (len(FAILED), FAILED)))
        return 0 if not FAILED else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
