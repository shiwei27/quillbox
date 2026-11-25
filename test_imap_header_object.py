#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回归：头字段含未编码 8 位字节时，email 返回 Header 对象而不是 str。

缺陷：内嵌图片的 Content-ID 用了中文文件名（原始 8 位字节），part.get("Content-ID")
返回 email.header.Header，代码对它调 .strip() 抛 AttributeError；异常冒出单封循环，
整个收件箱文件夹每轮失败、水位不动，之后的邮件全部收不进来。

本测试钉两件事（离线，不联网、不碰数据库）：
1. 这样的邮件能正常解析（根因）；
2. 即使单封解析仍然失败，文件夹循环也会退化成按头信息入库并继续，不拖垮整个文件夹。
"""
import email
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
fail = []


def check(label, cond, detail=""):
    print("  %s %s%s" % ("ok  " if cond else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not cond:
        fail.append(label)


from core.imap_extractor import ImapExtractor  # noqa: E402

RAW = (b"From: =?utf-8?B?5byg5LiJ?= <a@example.com>\r\nTo: b@example.com\r\nSubject: =?utf-8?B?5rWL6K+V?=\r\n"
       b"Message-ID: <hdr-obj@example.com>\r\nDate: Tue, 11 Nov 2026 09:00:00 +0800\r\nMIME-Version: 1.0\r\n"
       b"Content-Type: multipart/related; boundary=BB\r\n\r\n"
       b"--BB\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>hello<img src=\"cid:img1\"></p>\r\n"
       b"--BB\r\nContent-Type: image/png\r\nContent-ID: <\xe5\x9b\xbe\xe7\x89\x87.png>\r\n"
       b"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n--BB--\r\n")

print("=== 1. 前提：Content-ID 确实是 Header 对象 ===")
types = {type(p.get("Content-ID")).__name__ for p in email.message_from_bytes(RAW).walk() if p.get("Content-ID") is not None}
check("compat32 返回 Header", types == {"Header"}, types)

from pathlib import Path  # noqa: E402
tmp = Path(tempfile.mkdtemp(prefix="hdrobj_"))
e = ImapExtractor(host="h", username="u", account="me@example.com",
                  inline_root=tmp / "inline", state_path=tmp / "imap_state.json")

print("=== 2. 根因已修：能正常解析 ===")
try:
    rec = e._build_record(RAW, "7", "收件箱", True)
    check("解析成功", bool(rec))
    check("主题已解码", rec and rec["subject"] == "测试", rec and rec["subject"])
    check("发件人地址", rec and rec["sender_email"] == "a@example.com")
except Exception as exc:  # noqa: BLE001
    check("解析成功", False, "%s: %s" % (type(exc).__name__, exc))


print("=== 3. 兜底：单封仍失败时，文件夹循环不中断 ===")


class FakeConn:
    def __init__(self):
        self.untagged_responses = {}

    def response(self, name):
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [b"7 8"])
        if cmd == "FETCH":
            return ("OK", [(b"7 (FLAGS () BODY[] {10}", RAW), b")"])
        return ("OK", [b""])

    def select(self, *a, **k):
        return ("OK", [b"2"])


orig = e._build_record
calls = []


def flaky(raw, uid, folder, unread):
    calls.append(uid)
    if uid == "7":
        raise AttributeError("'Header' object has no attribute 'strip'")
    return orig(raw, uid, folder, unread)


e._build_record = flaky
sink = []
try:
    got = e._fetch_folder(FakeConn(), "INBOX", "收件箱", None, 50, sink, None)
    check("两封都处理了", calls == ["7", "8"], calls)
    check("失败那封按头信息入库", any("正文解析失败" in (r.get("body") or "") for r in sink), [r.get("body", "")[:20] for r in sink])
    check("后一封正常入库", len(sink) == 2, len(sink))
except Exception as exc:  # noqa: BLE001
    check("文件夹循环不中断", False, "%s: %s" % (type(exc).__name__, exc))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print()
print("结果：" + ("全部通过" if not fail else "失败 %d 项 -> %s" % (len(fail), fail)))
sys.exit(1 if fail else 0)
