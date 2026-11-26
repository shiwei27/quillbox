"""POP3 直连收信后端（不依赖 Outlook / MAPI）。

存在理由
--------
原有 core/email_extractor.py 走 win32com 读取本机 Outlook 存储，前提是 Outlook
已配好账号并处于可用状态。本模块直接用 POP3 从邮件服务器取信，服务器参数沿用
Thunderbird 上已验证可用的配置（mail.example.com:995 隐式 TLS，用户名为
``域\\用户`` 形式）。

协议局限（是 POP3 的固有限制，不是实现缺陷，改代码解决不了）
------------------------------------------------------------
1. POP3 没有文件夹概念，**只能取收件箱**。Outlook 路径能拿到的「项目 / 变更通知 /
   已发送邮件 / 存档」等文件夹，POP3 一律取不到。因此本模块产出的 ``folder``
   恒为 :data:`INBOX_NAME`，config.yaml 里基于 folder 的分拣规则对这部分邮件
   只会命中收件箱。
2. POP3 没有已读/未读标记。``unread`` 由本地 UIDL 状态推断：本次首见的 UIDL 记为
   未读，已记录过的记为已读。
3. POP3 没有服务端搜索/排序，也没有稳定的时间索引。本模块按报文序号倒序扫描，
   先用 ``TOP n 0`` 只取信头判断 Date，早于 cutoff 才停止，避免整箱全量 RETR。
4. **本模块永不发送 DELE**，服务器上的邮件不会被删除（等价于 Thunderbird 的
   leave_on_server=true）。这是有意的安全属性，不要改成删信。

输出记录与 EmailExtractor 完全同构（相同键名与语义），额外多一个 ``message_id``
键，供跨后端（Outlook / POP3）去重使用。
"""
from __future__ import annotations

import email
import hashlib
import html as _html
import json
import logging
import os
import poplib
import re
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
INLINE_ROOT = _ROOT / "data" / "inline"
STATE_PATH = _ROOT / "data" / ".pop3_state.json"

# 与 Outlook 路径产出的中文文件夹名保持一致，否则前端会多出一个空文件夹页签
INBOX_NAME = "收件箱"

# 某些邮件的单条头部（超长 References / DKIM）会超过 poplib 默认 2048 字节上限，
# 触发 "line too long"。放宽到 1MB。
poplib._MAXLINE = 1 << 20  # noqa: SLF001

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  文本工具
#
#  说明：这里刻意复制 EmailExtractor._clean_body / _safe_filename 的实现，
#  而不是 import 它们。因为 core/email_extractor.py 在模块顶层就
#  `import win32com.client`，一旦 import 就把 POP3 路径重新绑回 Outlook 依赖，
#  与本模块「不依赖 Outlook」的目的直接冲突。两处逻辑简单且已稳定，宁可重复。
# ---------------------------------------------------------------------- #

def _clean_body(raw_body: str) -> str:
    """去标签、还原实体、压缩空白，与 Outlook 路径产出的 body 风格一致。

    **必须先删掉 style/script 的内容再去标签。** 只去标签的话，
    `<style>` 里的 CSS 声明本身不是标签，会被当成正文留下来 ——
    例如一封只有 text/html 的通知邮件，正文会整段显示成
    `.ksomail-p { mso-line-height-rule:exactly; } ...`。

    这个缺陷以前碰不到：`body` 取 `text_body or html_body`，而绝大多数邮件都带
    text/plain 分段，走不到 html 这条路。HTML-only 的邮件才会触发。
    """
    if not raw_body:
        return ""
    # 1) 成对的 style/script：连内容一起删
    text = re.sub(r"(?is)<(style|script)\b[^>]*>.*?</\1\s*>", " ", raw_body)
    # 2) 未闭合的 style/script：既然没有闭合标签，后面本来就是 CSS/JS，丢到结尾
    text = re.sub(r"(?is)<(style|script)\b[^>]*>.*$", " ", text)
    # 3) HTML 注释（Outlook 的条件注释里常藏着整段 CSS）
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ") or "img"
    return name[:120]


def _hstr(value) -> str:
    """头字段原值 → str。

    头里含未编码的 8 位字节（例如内嵌图片的 Content-ID 用了中文文件名）时，
    email 的 compat32 策略返回 email.header.Header 对象而不是 str，直接 .strip()
    会抛 AttributeError —— 一封这样的邮件曾让整个收件箱每轮都取件失败。
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        return str(value)
    except Exception:                             # noqa: BLE001
        return ""


def _decode_hdr(raw) -> str:
    """解码 RFC2047 编码头（=?utf-8?B?...?=），容错返回尽量可读的字符串。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    out: List[str] = []
    try:
        parts = decode_header(str(raw))
    except Exception:
        return str(raw)
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            for cand in (enc, "utf-8", "gb18030", "latin-1"):
                if not cand:
                    continue
                try:
                    out.append(chunk.decode(cand))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                out.append(chunk.decode("utf-8", "replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _part_text(part: Message) -> str:
    """取单个 MIME part 的文本，按声明字符集解码，失败时逐个候选回退。"""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        return ""
    charset = (part.get_content_charset() or "").strip() or None
    for cand in (charset, "utf-8", "gb18030", "big5", "latin-1"):
        if not cand:
            continue
        try:
            return payload.decode(cand)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


class Pop3Extractor:
    """按 EmailExtractor 的对外接口提供 POP3 收信。

    Parameters
    ----------
    host, port, use_ssl:
        服务器地址与传输方式。``use_ssl=True`` 走隐式 TLS（995）；
        ``False`` 时若服务器支持会尝试 STARTTLS（110），失败则拒绝以明文继续。
    username:
        POP3 登录名。Exchange 上通常形如 ``DOMAIN\\user``。
    account:
        写入数据库 ``account`` 列的账号标识（邮箱地址），决定前端归类。
    """

    def __init__(
        self,
        host: str,
        username: str,
        account: str,
        port: int = 995,
        use_ssl: bool = True,
        timeout: int = 45,
        inline_root: Optional[Path] = None,
        state_path: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.account = account
        self.use_ssl = bool(use_ssl)
        self.timeout = int(timeout)
        self.inline_root = Path(inline_root) if inline_root else INLINE_ROOT
        self.state_path = Path(state_path) if state_path else STATE_PATH
        # fetch() 每轮扫描过的 UIDL，供调用方在成功后推进状态
        self._last_touched_uidls: List[str] = []

    # ------------------------------------------------------------------ #
    #  连接
    # ------------------------------------------------------------------ #

    def _connect(self, password: str) -> poplib.POP3:
        """建立并登录 POP3 连接。异常信息里不会出现密码。"""
        if self.use_ssl:
            ctx = ssl.create_default_context()
            pop = poplib.POP3_SSL(
                self.host, self.port, timeout=self.timeout, context=ctx
            )
        else:
            pop = poplib.POP3(self.host, self.port, timeout=self.timeout)
            try:
                pop.stls(context=ssl.create_default_context())
            except Exception as exc:
                pop.quit()
                raise RuntimeError(
                    f"{self.host}:{self.port} 不支持 STARTTLS，拒绝以明文传输凭据"
                ) from exc
        try:
            pop.user(self.username)
            pop.pass_(password)
        except poplib.error_proto as exc:
            # 服务器返回的 -ERR 文本可能回显登录名，但不含密码；
            # 仍统一改写为通用消息，避免把服务端应答原样写进日志。
            pop.close()
            raise RuntimeError(
                f"POP3 登录失败（{self.host}:{self.port}，用户 {self.username}）："
                "请确认用户名格式与密码。原始应答已省略。"
            ) from exc
        return pop

    def check_login(self, password: str) -> Tuple[int, int]:
        """只验证能否登录，返回 (邮件数, 邮箱字节数)。不下载任何正文。"""
        pop = self._connect(password)
        try:
            count, size = pop.stat()
            return int(count), int(size)
        finally:
            try:
                pop.quit()
            except Exception:
                pop.close()

    # ------------------------------------------------------------------ #
    #  UIDL 状态
    # ------------------------------------------------------------------ #

    def _load_state(self) -> dict:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.state_path)

    def seen_uidls(self) -> Set[str]:
        return set(self._load_state().get(self.account, {}).get("uidls", []))

    def remember_uidls(self, uidls: Sequence[str], cap: int = 50000) -> None:
        state = self._load_state()
        entry = state.setdefault(self.account, {})
        merged = list(dict.fromkeys(list(entry.get("uidls", [])) + list(uidls)))
        entry["uidls"] = merged[-cap:]
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)

    # ------------------------------------------------------------------ #
    #  报文解析
    # ------------------------------------------------------------------ #

    @staticmethod
    def _message_id(msg: Message) -> str:
        """归一化 Message-ID：去尖括号、去空白、转小写。缺失返回空串。"""
        raw = msg.get("Message-ID") or msg.get("Message-Id") or ""
        raw = _decode_hdr(raw).strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1]
        return raw.strip().lower()

    def _record_id(self, message_id: str, uidl: str) -> str:
        """稳定且文件系统安全的记录 id。

        不能直接用 ``pop3:<account>:<uidl>`` —— id 会被当作 data/inline 下的目录名，
        Windows 不允许文件名含 ':'。这里统一哈希成 ``pop3_<40位hex>``。
        优先用 Message-ID 做哈希输入，使同一封信在重装/换 UIDL 后仍得到同一 id。

        这里再做一次归一化（去尖括号、去空白、转小写），使「同一 Message-ID 必得
        同一 id」这个不变量不依赖调用方是否先调过 :meth:`_message_id`。
        """
        mid = (message_id or "").strip()
        if mid.startswith("<") and mid.endswith(">"):
            mid = mid[1:-1]
        mid = mid.strip().lower()
        key = mid or f"uidl:{uidl}"
        digest = hashlib.sha1(f"{self.account}|{key}".encode("utf-8")).hexdigest()
        return f"pop3_{digest}"

    @staticmethod
    def _parse_date(msg: Message) -> Tuple[str, float]:
        """解析 Date 头，返回 (time 字符串, received_timestamp)。

        为与 Outlook 路径已入库的 11775 行保持排序一致，这里复刻它的怪异约定：
        Outlook 侧 ``str(msg.ReceivedTime)`` 得到带 ``+00:00`` 后缀的字符串，而
        ``received_timestamp`` 是把同一挂钟时间 **当作本地时间** 取的时间戳。
        若这里改成规范 UTC，两批数据在同一列表里排序会错开时区偏移。
        """
        raw = _hstr(msg.get("Date"))
        dt = None
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError, AttributeError, IndexError):
                dt = None
        if dt is None:
            return "", 0.0
        if dt.tzinfo is not None:
            dt = dt.astimezone()          # 转本机时区
        naive = dt.replace(tzinfo=None)   # 挂钟时间
        time_str = str(naive.replace(tzinfo=timezone.utc))
        try:
            ts = naive.timestamp()
        except (OverflowError, OSError, ValueError):
            ts = 0.0
        return time_str, ts

    @staticmethod
    def _collect_recipients(msg: Message) -> List[Dict[str, str]]:
        """To + CC，结构与 EmailExtractor._safe_get_recipients 一致。"""
        out: List[Dict[str, str]] = []
        for field, kind in (("To", "to"), ("Cc", "cc")):
            raw = msg.get_all(field) or []
            decoded = [_decode_hdr(v) for v in raw]
            for name, addr in getaddresses(decoded):
                if not (name or addr):
                    continue
                out.append(
                    {"name": name.strip(), "email": addr.strip(), "type": kind}
                )
        return out

    def _walk_parts(
        self, msg: Message
    ) -> Tuple[str, str, List[Tuple[str, bytes]], List[str]]:
        """遍历 MIME，返回 (纯文本, HTML, 内嵌图片[(cid, 数据)], 附件文件名)。"""
        text_parts: List[str] = []
        html_parts: List[str] = []
        inline: List[Tuple[str, bytes]] = []
        attachments: List[str] = []

        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get_content_disposition() or "").lower()
            filename = _decode_hdr(part.get_filename() or "")
            cid = _hstr(part.get("Content-ID")).strip().strip("<>")

            is_body = (
                disp != "attachment"
                and not filename
                and ctype in ("text/plain", "text/html")
            )
            if is_body:
                if ctype == "text/plain":
                    text_parts.append(_part_text(part))
                else:
                    html_parts.append(_part_text(part))
                continue

            if ctype.startswith("image/") and (cid or disp == "inline"):
                try:
                    data = part.get_payload(decode=True) or b""
                except Exception:
                    data = b""
                if data:
                    name = filename or f"{cid or 'img'}"
                    inline.append((cid or name, data))
                    if filename:
                        # 内嵌图且带文件名时，Outlook 侧也会计入附件列表
                        attachments.append(filename)
                continue

            if filename:
                attachments.append(filename)

        return (
            "\n\n".join(p for p in text_parts if p).strip(),
            "\n".join(p for p in html_parts if p).strip(),
            inline,
            attachments,
        )

    def _save_inline(
        self, email_id: str, html_body: str, inline: List[Tuple[str, bytes]]
    ) -> str:
        """把内嵌图落到 data/inline/<id>/，并将 cid: 引用改写为 /api/inline 路径。

        与 EmailExtractor._extract_html_body 的产物约定保持一致，这样前端
        /api/inline/{email_id}/{filename} 不需要任何改动。
        """
        if not inline:
            return html_body
        target = self.inline_root / email_id
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("内嵌图目录创建失败 %s: %s", target, exc)
            return html_body

        for idx, (cid, data) in enumerate(inline):
            fname = _safe_filename(cid) or f"img{idx}"
            if "." not in fname:
                fname = f"{fname}.img"
            try:
                (target / fname).write_bytes(data)
            except OSError as exc:
                logger.warning("内嵌图写入失败 %s: %s", fname, exc)
                continue
            if html_body and cid:
                html_body = re.sub(
                    r'cid:%s' % re.escape(cid),
                    f"/api/inline/{email_id}/{fname}",
                    html_body,
                    flags=re.IGNORECASE,
                )
        return html_body

    def _build_record(self, raw: bytes, uidl: str, unread: bool) -> Optional[Dict]:
        """把 RFC822 原文转成与 Outlook 路径同构的记录字典。"""
        try:
            msg = email.message_from_bytes(raw)
        except Exception as exc:
            logger.warning("报文解析失败 uidl=%s: %s", uidl, exc)
            return None

        message_id = self._message_id(msg)
        email_id = self._record_id(message_id, uidl)
        subject = _decode_hdr(msg.get("Subject")) or "(无主题)"
        sender_name, sender_addr = parseaddr(_decode_hdr(msg.get("From")))
        time_str, ts = self._parse_date(msg)
        text_body, html_body, inline, attachments = self._walk_parts(msg)
        html_body = self._save_inline(email_id, html_body, inline)

        # 有些信只有 HTML，没有 text/plain：从 HTML 退化出纯文本，
        # 保证 body 列非空，否则 FTS 检索和 AI 摘要都会拿到空输入。
        # text/plain 分支也过一次 _clean_body，与 Outlook 路径的产出风格对齐。
        body = _clean_body(text_body) if text_body else _clean_body(html_body)

        return {
            "id": email_id,
            "message_id": message_id,
            "subject": subject,
            "body": body,
            "html_body": html_body,
            "sender": sender_name or sender_addr,
            "sender_email": sender_addr,
            "recipients": self._collect_recipients(msg),
            "time": time_str,
            "received_timestamp": ts,
            "unread": unread,
            "folder": INBOX_NAME,
            "account": self.account,
            "has_attachment": bool(attachments),
            "attachments": attachments,
        }

    # ------------------------------------------------------------------ #
    #  对外 API（与 EmailExtractor 对齐）
    # ------------------------------------------------------------------ #

    def fetch(
        self,
        password: str,
        days_ago: int = 30,
        on_progress: Optional[Callable] = None,
        limit: Optional[int] = None,
        headers_only: bool = False,
        skip_seen: bool = True,
    ) -> List[Dict]:
        """从服务器取信。

        headers_only=True 时只做 ``TOP n 0``（信头），产出的记录 body/html 为空，
        仅用于 dry-run 体检，不应入库。
        """
        cutoff = datetime.now() - timedelta(days=days_ago)
        seen = self.seen_uidls() if skip_seen else set()
        results: List[Dict] = []
        touched: List[str] = []

        pop = self._connect(password)
        try:
            count, _size = pop.stat()
            if not count:
                return results

            # UIDL 全量映射：{报文序号: uidl}
            uidl_map: Dict[int, str] = {}
            try:
                _resp, lines, _oct = pop.uidl()
                for line in lines:
                    parts = line.decode("ascii", "replace").split()
                    if len(parts) >= 2 and parts[0].isdigit():
                        uidl_map[int(parts[0])] = parts[1]
            except poplib.error_proto:
                logger.warning("服务器不支持 UIDL，退化为按序号去重（可靠性下降）")

            stale_streak = 0
            # 倒序扫描：POP3 序号大致按投递顺序递增，新信在后。
            for num in range(count, 0, -1):
                if limit is not None and len(results) >= limit:
                    break
                uidl = uidl_map.get(num, f"seq:{num}")
                touched.append(uidl)

                # 先只取信头判断时间，避免整箱 RETR（本邮箱历史跨 5 年）
                try:
                    _r, hlines, _o = pop.top(num, 0)
                except poplib.error_proto as exc:
                    logger.warning("TOP %s 失败: %s", num, exc)
                    continue
                head_raw = b"\r\n".join(hlines)
                try:
                    head_msg = email.message_from_bytes(head_raw)
                except Exception:
                    continue

                _tstr, ts = self._parse_date(head_msg)
                if ts and datetime.fromtimestamp(ts) < cutoff:
                    stale_streak += 1
                    # 少数信 Date 头乱序，连续 20 封都早于 cutoff 才认定到边界
                    if stale_streak >= 20:
                        break
                    continue
                stale_streak = 0

                if skip_seen and uidl in seen:
                    continue

                if headers_only:
                    rec = self._build_record(head_raw, uidl, unread=uidl not in seen)
                else:
                    try:
                        _r2, blines, _o2 = pop.retr(num)
                    except poplib.error_proto as exc:
                        logger.warning("RETR %s 失败: %s", num, exc)
                        continue
                    rec = self._build_record(
                        b"\r\n".join(blines), uidl, unread=uidl not in seen
                    )
                if rec is None:
                    continue
                rec["_uidl"] = uidl
                results.append(rec)

                if on_progress and len(results) % 10 == 0:
                    try:
                        on_progress(
                            self.account, INBOX_NAME, len(results), rec["subject"]
                        )
                    except Exception:
                        pass
        finally:
            # 全程不发 DELE：服务器邮件保持原样
            try:
                pop.quit()
            except Exception:
                pop.close()

        self._last_touched_uidls = touched
        return results

    def get_all_emails(
        self, days_ago: int = 1095, on_progress: Optional[Callable] = None
    ) -> List[Dict]:
        """与 EmailExtractor.get_all_emails 同签名，供 daemon 直接替换调用。

        密码从 Windows DPAPI 凭据库读取（core/win_credential.py）。
        取信成功后才推进 UIDL 状态，避免中途异常导致漏信。
        """
        from core.win_credential import get_password

        password = get_password(self.account)
        if not password:
            raise RuntimeError(
                f"未登记 {self.account} 的邮箱密码。"
                f"请在该 Windows 用户下执行：python pop3_setup.py set {self.account}"
            )
        records = self.fetch(password, days_ago=days_ago, on_progress=on_progress)
        del password
        new_uidls = [r.pop("_uidl") for r in records if "_uidl" in r]
        if new_uidls:
            self.remember_uidls(new_uidls)
        return records

    def get_incremental_emails(
        self, days_ago: int = 7, on_progress: Optional[Callable] = None
    ) -> List[Dict]:
        return self.get_all_emails(days_ago=days_ago, on_progress=on_progress)


def from_config(cfg: dict, account: str) -> Pop3Extractor:
    """按 config.yaml 的 pop3 段构造实例。

    期望结构（username 含反斜杠，YAML 里建议用单引号包起来避免歧义）::

        pop3:
          enabled: true
          accounts:
            you@example.com:
              host: mail.example.com
              port: 995
              ssl: true
              username: 'DOMAIN\\user'
    """
    section = (cfg or {}).get("pop3") or {}
    accounts = section.get("accounts") or {}
    acc = accounts.get(account)
    if not acc:
        raise KeyError(f"config.yaml 的 pop3.accounts 下没有 {account}")
    return Pop3Extractor(
        host=acc["host"],
        username=acc["username"],
        account=account,
        port=acc.get("port", 995),
        use_ssl=acc.get("ssl", True),
        timeout=acc.get("timeout", 45),
    )
