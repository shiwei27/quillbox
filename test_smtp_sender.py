#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SMTP 组包与提交逻辑（离线，桩 SMTP，不联网不发信）。

按重要性排序的判据：
1. **Bcc 绝不出现在邮件头**——写进头部就等于把密送名单泄露给所有收件人；
   但它必须出现在 RCPT TO 里，否则密送根本收不到。
2. **回复必须带 In-Reply-To / References**——改前走 Outlook EntryID，
   对 IMAP 邮件恒定位不到，线程头就丢了，对方客户端里不成串。
3. HTML 必须配纯文本替代，否则部分客户端与手机端只显示空白或标签。
4. 收件人去重（同一地址同时在 To 与 Cc 时不该收到两封）。
5. dry_run 不得进入 DATA 阶段。
6. 附件单个失败不毁整封。

用法：python test_smtp_sender.py
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import os
import sys
import tempfile
import unittest

sys.path.insert(0, ROOT)
from core import smtp_sender as ss  # noqa: E402


class FakeSMTP:
    """桩 SMTP_SSL：记录调用，绝不联网。"""
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = []
        self.noops = 0
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return (250, b"ok")

    def login(self, u, p):
        self.logged_in = (u, "<pw>")

    def noop(self):
        self.noops += 1
        return (250, b"ok")

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append({"msg": msg, "from": from_addr, "to": list(to_addrs or [])})

    def quit(self):
        self.quit_called = True


def patch_smtp():
    ss.smtplib.SMTP_SSL = FakeSMTP
    FakeSMTP.instances = []


class BuildMessageTest(unittest.TestCase):

    def test_bcc_never_in_headers(self):
        """核心安全判据：密送名单不能出现在头部。"""
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="s",
                             body="b", bcc="secret@x.com")
        raw = m.as_string()
        self.assertNotIn("secret@x.com", raw, "密送地址泄露进了邮件内容")
        self.assertIsNone(m.get("Bcc"))

    def test_to_and_cc_in_headers(self):
        m = ss.build_message(sender="me@x.com", to="a@x.com", cc="c@x.com",
                             subject="s", body="b")
        self.assertIn("a@x.com", m["To"])
        self.assertIn("c@x.com", m["Cc"])

    def test_semicolon_separated_addresses(self):
        """前端按 Outlook 习惯用分号分隔，必须接受。"""
        m = ss.build_message(sender="me@x.com", to="a@x.com; b@y.com",
                             subject="s", body="b")
        self.assertIn("a@x.com", m["To"])
        self.assertIn("b@y.com", m["To"])

    def test_reply_headers(self):
        mid = "<orig@server>"
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="Re: s",
                             body="b", in_reply_to=mid)
        self.assertEqual(m["In-Reply-To"], mid)
        self.assertEqual(m["References"], mid)

    def test_references_chain_preserved(self):
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="s", body="b",
                             in_reply_to="<b@s>", references="<a@s> <b@s>")
        self.assertEqual(m["References"], "<a@s> <b@s>")

    def test_html_gets_plaintext_alternative(self):
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="s",
                             body="<p>粗体<b>字</b></p>", is_html=True)
        types = {p.get_content_type() for p in m.walk()}
        self.assertIn("text/html", types)
        self.assertIn("text/plain", types)
        plain = [p for p in m.walk() if p.get_content_type() == "text/plain"][0]
        txt = plain.get_content()
        self.assertIn("粗体", txt)
        self.assertNotIn("<b>", txt, "纯文本部分里残留了标签")

    def test_signature_before_quote(self):
        """签名要在正文后、引用前——在引用之后没人会看到。"""
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="s",
                             body="正文", signature="张三 / 运维",
                             quote_text="------ 原始邮件 ------", is_html=False)
        txt = m.get_content()
        self.assertLess(txt.index("张三"), txt.index("原始邮件"))

    def test_message_id_present(self):
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="s", body="b")
        self.assertTrue((m["Message-ID"] or "").startswith("<"))

    def test_attachment_added(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "note.txt")
        open(p, "w", encoding="utf-8").write("hi")
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="s",
                             body="b", attachments=[p])
        names = [x.get_filename() for x in m.walk() if x.get_filename()]
        self.assertIn("note.txt", names)

    def test_missing_attachment_does_not_break(self):
        m = ss.build_message(sender="me@x.com", to="a@x.com", subject="s",
                             body="b", attachments=["D:/nope/missing.bin"])
        self.assertEqual(m["To"], "a@x.com")


class SendTest(unittest.TestCase):

    def setUp(self):
        patch_smtp()
        self.msg = ss.build_message(sender="me@x.com", to="a@x.com", cc="c@x.com",
                                    subject="s", body="b")

    def send(self, **kw):
        kw.setdefault("host", "h")
        kw.setdefault("username", "me@x.com")
        kw.setdefault("password", "pw")
        kw.setdefault("msg", self.msg)
        return ss.send_via_smtp(**kw)

    def test_bcc_in_envelope_only(self):
        r = self.send(bcc="secret@x.com")
        self.assertTrue(r["ok"])
        env = FakeSMTP.instances[0].sent[0]["to"]
        self.assertIn("secret@x.com", env, "密送没有进 RCPT TO，收不到")
        self.assertNotIn("secret@x.com", self.msg.as_string())

    def test_recipient_dedup(self):
        m = ss.build_message(sender="me@x.com", to="a@x.com", cc="A@X.com",
                             subject="s", body="b")
        self.send(msg=m)
        env = FakeSMTP.instances[0].sent[0]["to"]
        self.assertEqual(len(env), 1, "同一地址被投递了多次: %s" % env)

    def test_dry_run_does_not_send(self):
        r = self.send(dry_run=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dry_run"])
        inst = FakeSMTP.instances[0]
        self.assertEqual(inst.sent, [], "dry_run 竟然真发了")
        self.assertEqual(inst.noops, 1)

    def test_no_recipient_is_error(self):
        m = ss.build_message(sender="me@x.com", to="", subject="s", body="b")
        r = self.send(msg=m)
        self.assertFalse(r["ok"])

    def test_always_quits(self):
        self.send()
        self.assertTrue(FakeSMTP.instances[0].quit_called)

    def test_password_not_in_result(self):
        r = self.send()
        self.assertNotIn("pw", str(r))


class QuoteTest(unittest.TestCase):

    def test_html_quote_contains_headers_and_body(self):
        html, text = ss.build_quote({"sender": "张三", "time": "2026-09-16 10:00",
                                     "subject": "原主题",
                                     "html_body": "<p>原文内容</p>"}, is_html=True)
        self.assertIn("原始邮件", html)
        self.assertIn("张三", html)
        self.assertIn("原文内容", html)
        self.assertIn("原主题", text)

    def test_text_only_quote(self):
        html, text = ss.build_quote({"sender": "李四", "time": "t", "subject": "s",
                                     "body": "纯文本原文"}, is_html=False)
        self.assertEqual(html, "")
        self.assertIn("纯文本原文", text)

    def test_escapes_html_in_headers(self):
        html, _t = ss.build_quote({"sender": "<script>x</script>", "time": "t",
                                   "subject": "s", "body": "b"}, is_html=True)
        self.assertNotIn("<script>", html, "发件人字段未转义，可注入")


if __name__ == "__main__":
    unittest.main(verbosity=2)
