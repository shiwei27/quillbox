# -*- coding: utf-8 -*-
"""SMTP 发送通道：与 IMAP 同一套凭据，不依赖 Outlook 与交互桌面 session。

与 core/email_sender.py（Outlook COM）的关系：两者并存。
COM 那条保留给历史 Outlook 邮件的原生回复/转发；这条负责所有新邮件，
因为原文定位用的是 message_id 而不是 Outlook EntryID。

安全边界：
  * 只发送，不定时（schedule 在 MCP 拒绝清单里，不给它开后门）；
  * 凭据由调用方传入、用完即弃，本模块不读取也不缓存任何密钥；
  * dry_run 走完组包与认证但不进入 DATA 阶段，用于验证而不外发。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

log = logging.getLogger(__name__)

DEFAULT_PORT = 465


def _addr_list(raw: str) -> list:
    """把 "a@x; b@y, c@z" 拆成地址列表。分号与逗号都接受——
    前端用分号（Outlook 习惯），标准是逗号。"""
    if not raw:
        return []
    out = []
    for part in str(raw).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, addr = parseaddr(part)
        out.append(formataddr((name, addr)) if name else (addr or part))
    return out


def _plain_from_html(html: str) -> str:
    """给 HTML 正文配一份纯文本替代。

    不配的话部分客户端（尤其手机端与纯文本网关）只显示空白或整段标签。
    """
    import re
    s = html or ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def build_message(*, sender: str, to: str, subject: str, body: str,
                  cc: str = "", bcc: str = "", is_html: bool = False,
                  signature: str = "",
                  in_reply_to: str = "", references: str = "",
                  quote_html: str = "", quote_text: str = "",
                  attachments=None) -> EmailMessage:
    """组装一封邮件。纯函数，便于离线测试。

    * signature 追加在正文之后、引用之前——签名在引用之后没人会看。
    * in_reply_to / references 用库里的 message_id 填，让对方客户端能成串。
      改前走 Outlook EntryID，对 IMAP 邮件恒定位不到，线程头就丢了。
    """
    msg = EmailMessage()
    msg["From"] = sender
    tos, ccs, bccs = _addr_list(to), _addr_list(cc), _addr_list(bcc)
    if tos:
        msg["To"] = ", ".join(tos)
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    # Bcc 不写进头部：写了收件人就能看见，等于泄露密送名单。
    # 它只用于 sendmail 的 RCPT TO，见 send_via_smtp。
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    # Message-ID 的域名部分要用发件人域，不能用 make_msgid() 默认的本机 FQDN
    # ——有的 Windows 主机上解析出来是 localhost_ipv6，这种 Message-ID 会被部分
    # 反垃圾策略加分甚至拒收，也不利于追踪。
    _sender_domain = (parseaddr(sender)[1] or "").rsplit("@", 1)[-1].strip()
    msg["Message-ID"] = make_msgid(domain=_sender_domain) if _sender_domain else make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references or in_reply_to)

    sig_txt = ("\n\n" + signature) if signature else ""
    sig_html = ("<br><br>" + signature.replace("\n", "<br>")) if signature else ""

    if is_html:
        html = (body or "") + sig_html + (quote_html or "")
        text = _plain_from_html(body or "") + sig_txt + (
            ("\n\n" + quote_text) if quote_text else "")
        msg.set_content(text or " ")
        msg.add_alternative(html, subtype="html")
    else:
        text = (body or "") + sig_txt + (("\n\n" + quote_text) if quote_text else "")
        msg.set_content(text)

    for path in (attachments or []):
        try:
            import mimetypes
            import os as _os
            if not path or not _os.path.isfile(path):
                continue
            ctype, _enc = mimetypes.guess_type(path)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            with open(path, "rb") as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                                   filename=_os.path.basename(path))
        except Exception as e:              # 单个附件失败不该毁掉整封
            log.warning("附件跳过 %s: %s", path, e)
    return msg


def send_via_smtp(*, host: str, port: int = DEFAULT_PORT, username: str,
                  password: str, msg: EmailMessage, bcc: str = "",
                  verify_tls: bool = False, timeout: int = 30,
                  use_ssl: bool = True, dry_run: bool = False) -> dict:
    """提交一封已组装好的邮件。

    dry_run=True 时连接并认证，但**不进入 DATA 阶段**——
    用于"链路可用性"验证而不产生真实外发。
    返回 {ok, message_id, recipients, dry_run}；不回显任何凭据。
    """
    rcpts = []
    for header in ("To", "Cc"):
        rcpts += _addr_list(msg.get(header, ""))
    rcpts += _addr_list(bcc)
    # 去重但保序，避免同一地址收到两封
    seen, uniq = set(), []
    for r in rcpts:
        _n, a = parseaddr(r)
        key = (a or r).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    if not uniq:
        return {"ok": False, "error": "没有有效收件人"}

    ctx = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
    if use_ssl:
        # 隐式 TLS（465）。有的服务器只开 465、587 拒连。
        srv = smtplib.SMTP_SSL(host, int(port), timeout=timeout, context=ctx)
        srv.ehlo()
    else:
        # 明文端口 + STARTTLS（587）。多数 Exchange 走这条，
        # 用 SMTP_SSL 连它会报 SSL: WRONG_VERSION_NUMBER。
        srv = smtplib.SMTP(host, int(port), timeout=timeout)
        srv.ehlo()
        if not srv.has_extn("starttls"):
            # 刻意不退回明文发送：凭据与正文不该在明文里裸奔
            try:
                srv.quit()
            except Exception:                          # noqa: BLE001
                pass
            return {"ok": False,
                    "error": "服务器不支持 STARTTLS，拒绝以明文发送（%s:%s）"
                             % (host, port)}
        srv.starttls(context=ctx)
        srv.ehlo()
    try:
        srv.login(username, password)
        if dry_run:
            srv.noop()
            log.info("SMTP 干跑：认证通过，未进入 DATA 阶段，收件人 %d 个", len(uniq))
            return {"ok": True, "dry_run": True, "message_id": msg["Message-ID"],
                    "recipients": len(uniq)}
        srv.send_message(msg, from_addr=parseaddr(msg["From"])[1] or username,
                         to_addrs=[parseaddr(r)[1] or r for r in uniq])
        log.info("SMTP 已提交 message_id=%s 收件人 %d 个", msg["Message-ID"], len(uniq))
        return {"ok": True, "dry_run": False, "message_id": msg["Message-ID"],
                "recipients": len(uniq)}
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def build_quote(original: dict, is_html: bool = True) -> tuple:
    """从库里的原邮件生成引用块，返回 (html, text)。

    改前回复靠 Outlook 自己生成引用，对 IMAP 邮件没有这条路，
    于是回复出去只有新写的内容、看不到上下文。
    """
    import html as _h
    sender = original.get("sender") or original.get("sender_email") or ""
    when = original.get("time") or ""
    subj = original.get("subject") or ""
    body_html = original.get("html_body") or ""
    body_text = original.get("body") or ""

    head_txt = "------ 原始邮件 ------\n发件人：%s\n时间：%s\n主题：%s\n" % (
        sender, when, subj)
    if is_html:
        head = ("<br><br><div style=\"border-left:2px solid #ccc;padding-left:10px;"
                "color:#666\"><p>------ 原始邮件 ------<br>"
                "发件人：%s<br>时间：%s<br>主题：%s</p>" % (
                    _h.escape(sender), _h.escape(when), _h.escape(subj)))
        inner = body_html or ("<pre>%s</pre>" % _h.escape(body_text[:20000]))
        return (head + inner + "</div>", head_txt + (body_text or "")[:20000])
    return ("", head_txt + (body_text or "")[:20000])
