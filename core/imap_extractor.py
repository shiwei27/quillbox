"""IMAP 取件后端。

为什么是 IMAP 而不是 POP3
--------------------------
不少企业邮件服务器上 **POP3 只暴露"尚未收取"的邮件**
（登录成功但报 0 封），而 Outlook 正用 POP3 在收，等于每收一次就把邮件从 POP3
视图里划掉。若 Quillbox 也走 POP3，两边会抢同一批邮件。IMAP 看到的是完整服务端
状态（全部文件夹与邮件），且带文件夹、已读标记、已发送邮件。

三条硬约束
----------
1. **绝不改服务端状态**：`select(readonly=True)` + `BODY.PEEK[]` 取信。
   若用 `RFC822` 取，服务器会给邮件打上 ``\\Seen``，把你在 Outlook/OWA 里的
   未读状态抹掉 —— 这是最容易踩的坑。
2. **状态推进由调用方决定**：`fetch()` 只把"本轮扫到哪个 UID"记在内存里，
   要等调用方成功入库后显式调 `commit_state()` 才落盘。空跑（dry-run）因此
   不会消耗增量窗口，可以反复跑。
3. **记录结构与另两条后端同构**：MIME 解析直接复用
   :mod:`core.pop3_extractor` 的实现（同一份代码，避免字段漂移），只有
   ``folder`` 与 ``id`` 前缀不同。

去重
----
本模块只负责产出记录（含归一化 ``message_id``）；跨后端去重由调用方用
``DBManager.find_id_by_message_id`` 完成，历史行缺 message_id 的那一小部分
再用"同账户 + 主题相同 + 收信时间差 ≤2 秒"兜底。
"""
from __future__ import annotations

import base64
import email
import hashlib
import imaplib
import json
import logging
import re
import ssl
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from core.pop3_extractor import (       # 复用：同一套 MIME 解析，保证两条后端记录同构
    INBOX_NAME,
    INLINE_ROOT,
    Pop3Extractor,
    _clean_body,
    _decode_hdr,
)
from core.pop3_extractor import _hstr  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = _ROOT / "data" / ".imap_state.json"

# 默认跳过的文件夹（按解码后的显示名比较，小写）：草稿不是收到的信，
# 回收站/待删除会把用户已经删掉的邮件重新拉回来。
DEFAULT_SKIP_FOLDERS = (
    "trash", "drafts", "junk",
    "草稿", "草稿箱", "已删除邮件", "待删除邮件", "垃圾邮件",
)

# IMAP 文件夹名 → Quillbox folder 列。只映射语义等价的入口，其余保留服务端原名。
FOLDER_ALIASES = {
    "inbox": INBOX_NAME,          # IMAP 的 Inbox 就是 Outlook 侧的"收件箱"
}

_LIST_HEAD_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s*(?P<rest>.*)', re.S)


# --------------------------------------------------------------------------- #
#  IMAP modified UTF-7（RFC 3501 §5.1.3）
# --------------------------------------------------------------------------- #

def mutf7_decode(value: str) -> str:
    """modified UTF-7 → str。中文文件夹名形如 ``&kceNLQ-``。"""
    out: List[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        j = value.find("-", i)
        if j < 0:
            j = len(value)
        chunk = value[i + 1:j]
        if chunk == "":
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:                     # noqa: BLE001
                out.append(value[i:j + 1])        # 解不开就原样保留，不丢信息
        i = j + 1
    return "".join(out)


def mutf7_encode(value: str) -> str:
    """str → modified UTF-7（选择文件夹时要用服务端原始名，此函数供构造/测试用）。"""
    out: List[str] = []
    buf: List[str] = []

    def _flush() -> None:
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append("&%s-" % b64)
        buf.clear()

    for ch in value:
        if ch == "&":
            _flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            _flush()
            out.append(ch)
        else:
            buf.append(ch)
    _flush()
    return "".join(out)


class ImapExtractor:
    """按文件夹增量取件，产出与 EmailExtractor / Pop3Extractor 同构的记录。"""

    ID_PREFIX = "imap"

    # ---- 复用 POP3 后端的 MIME 解析（同一份实现，防止字段漂移）----------- #
    _message_id = staticmethod(Pop3Extractor._message_id)          # noqa: SLF001
    _parse_date = staticmethod(Pop3Extractor._parse_date)          # noqa: SLF001
    _collect_recipients = staticmethod(Pop3Extractor._collect_recipients)  # noqa: SLF001
    _walk_parts = Pop3Extractor._walk_parts                        # noqa: SLF001
    _save_inline = Pop3Extractor._save_inline                      # noqa: SLF001

    def __init__(
        self,
        host: str,
        username: str,
        account: str,
        port: int = 993,
        use_ssl: bool = True,
        timeout: int = 60,
        inline_root: Optional[Path] = None,
        state_path: Optional[Path] = None,
        skip_folders: Sequence[str] = DEFAULT_SKIP_FOLDERS,
        verify_tls: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.account = account
        self.use_ssl = bool(use_ssl)
        self.timeout = int(timeout)
        self.inline_root = Path(inline_root) if inline_root else INLINE_ROOT
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.skip_folders = {s.strip().lower() for s in (skip_folders or ())}
        # 公司内网邮件服务器多为自签/私有 CA 证书；默认不校验，避免整条链路不可用。
        self.verify_tls = bool(verify_tls)
        # 本轮扫到的位置：{原始文件夹名: (uidvalidity, max_uid)}，等 commit_state() 落盘
        self._pending: Dict[str, Tuple[int, int]] = {}

    # ------------------------------------------------------------------ #
    #  连接
    # ------------------------------------------------------------------ #

    def _ssl_ctx(self) -> ssl.SSLContext:
        if self.verify_tls:
            return ssl.create_default_context()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connect(self, password: str) -> imaplib.IMAP4:
        """建立连接并登录。异常信息里不会带出密码。"""
        try:
            if self.use_ssl:
                conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    self.host, self.port, ssl_context=self._ssl_ctx(), timeout=self.timeout)
            else:
                conn = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
                try:
                    conn.starttls(ssl_context=self._ssl_ctx())
                except Exception as exc:          # noqa: BLE001
                    raise RuntimeError(
                        "服务器不支持 STARTTLS，拒绝以明文继续：%s" % exc) from None
        except OSError as exc:
            raise RuntimeError("IMAP 连接失败（%s:%d）：%s" % (self.host, self.port, exc)) from None
        try:
            conn.login(self.username, password)
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(
                "IMAP 登录失败（%s:%d，用户 %s）：%s。请确认用户名格式与密码。"
                % (self.host, self.port, self.username, exc)) from None
        return conn

    def check_login(self, password: str) -> Tuple[int, int]:
        """连通性自检，返回 (文件夹数, 收件箱邮件数)。只读。"""
        conn = self._connect(password)
        try:
            folders = self.list_folders(conn)
            inbox_raw = next((raw for raw, disp, _f in folders if disp == INBOX_NAME), "Inbox")
            count = 0
            typ, data = conn.status(self._quote(inbox_raw), "(MESSAGES)")
            if typ == "OK" and data and data[0]:
                m = re.search(rb"MESSAGES (\d+)",
                              b" ".join(self._flatten(x) for x in data))
                if m:
                    count = int(m.group(1))
            return len(folders), count
        finally:
            self._logout(conn)

    @staticmethod
    def _logout(conn: imaplib.IMAP4) -> None:
        try:
            conn.logout()
        except Exception:                         # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    #  文件夹
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_list_item(item) -> Optional[Tuple[str, str, str]]:
        """解析 LIST 响应的一条，返回 (flags, 分隔符, 原始文件夹名)。

        带子文件夹的条目服务器会用 literal 形式返回，此时 imaplib 给的是 tuple
        （头部 + 名字），不能按单行正则处理 —— 早期版本就在这里崩过。
        """
        if isinstance(item, tuple):
            head = item[0].decode("utf-8", "replace") if isinstance(item[0], bytes) else str(item[0])
            name = item[1].decode("utf-8", "replace") if isinstance(item[1], bytes) else str(item[1])
            m = _LIST_HEAD_RE.match(head)
            flags = m.group("flags") if m else ""
            delim = m.group("delim") if m else "/"
            return flags, delim, name.strip().strip('"')
        if isinstance(item, bytes):
            line = item.decode("utf-8", "replace")
        else:
            line = str(item)
        m = _LIST_HEAD_RE.match(line)
        if not m:
            return None
        name = (m.group("rest") or "").strip().strip('"')
        if not name:
            return None
        return m.group("flags"), m.group("delim"), name

    @staticmethod
    def _flatten(item) -> bytes:
        """把 imaplib 的一条响应拍平成 bytes。

        带 literal 的响应（长名字文件夹、部分 STATUS/FETCH）会以 tuple 返回，
        直接当 bytes 用会静默拿不到数据。
        """
        if isinstance(item, tuple):
            return b"".join(x if isinstance(x, bytes) else str(x).encode("utf-8") for x in item)
        if isinstance(item, bytes):
            return item
        return str(item or "").encode("utf-8")

    @staticmethod
    def _quote(raw: str) -> str:
        """把文件夹名包成 IMAP quoted-string。

        RFC 3501 的 quoted-string 里 ``\\`` 与 ``"`` 必须转义；本邮箱就有一个
        名字带反斜杠的子文件夹（数据中心运营组\无标题），不转义会被服务器回 NO。
        """
        return '"%s"' % raw.replace("\\", "\\\\").replace('"', '\\"')

    def list_folders(self, conn: imaplib.IMAP4) -> List[Tuple[str, str, str]]:
        """返回 [(原始名, 显示名, flags)]，已剔除跳过项与不可选中项。"""
        typ, items = conn.list()
        if typ != "OK":
            raise RuntimeError("LIST 失败：%s" % typ)
        out: List[Tuple[str, str, str]] = []
        for item in items or []:
            parsed = self._parse_list_item(item)
            if not parsed:
                continue
            flags, _delim, raw = parsed
            if "\\noselect" in flags.lower():
                continue
            disp = self.folder_display_name(raw)
            if disp.lower() in self.skip_folders or raw.lower() in self.skip_folders:
                continue
            out.append((raw, disp, flags))
        return out

    def folder_counts(self, conn: imaplib.IMAP4, raw: str) -> Tuple[int, int]:
        """返回 (MESSAGES, UNSEEN)；取不到返回 (-1, -1)。只读。"""
        try:
            typ, data = conn.status(self._quote(raw), "(MESSAGES UNSEEN)")
            if typ != "OK" or not data:
                return -1, -1
            info = b" ".join(self._flatten(x) for x in data).decode("utf-8", "replace")
            m = re.search(r"MESSAGES (\d+).*?UNSEEN (\d+)", info)
            return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
        except Exception:                         # noqa: BLE001
            return -1, -1

    @staticmethod
    def folder_display_name(raw: str) -> str:
        """原始（modified UTF-7）文件夹名 → 写入 DB 的 folder 值。"""
        decoded = mutf7_decode(raw)
        return FOLDER_ALIASES.get(decoded.strip().lower(), decoded)

    # ------------------------------------------------------------------ #
    #  增量状态：{account: {"folders": {raw: {uidvalidity, last_uid}}}}
    # ------------------------------------------------------------------ #

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)          # 原子替换，避免掉线写坏状态
        except OSError as exc:
            logger.warning("IMAP 状态写入失败 %s: %s", self.state_path, exc)

    def folder_state(self, raw_folder: str) -> Tuple[int, int]:
        """返回该文件夹已记录的 (uidvalidity, last_uid)；无记录返回 (0, 0)。"""
        entry = (self._load_state().get(self.account, {})
                 .get("folders", {}).get(raw_folder) or {})
        try:
            return int(entry.get("uidvalidity", 0)), int(entry.get("last_uid", 0))
        except (TypeError, ValueError):
            return 0, 0

    def has_state(self) -> bool:
        """该账户是否已有任何文件夹水位。用于区分"首轮全量"与"增量"。"""
        folders = (self._load_state().get(self.account, {}) or {}).get("folders") or {}
        return any(int((v or {}).get("last_uid", 0) or 0) > 0 for v in folders.values())

    def commit_state(self) -> int:
        """把本轮 fetch 扫到的位置落盘。返回更新的文件夹数。

        必须在调用方**成功入库之后**调用：先落盘再入库的话，中途失败会导致
        这批邮件被永久跳过。
        """
        if not self._pending:
            return 0
        state = self._load_state()
        acct = state.setdefault(self.account, {})
        folders = acct.setdefault("folders", {})
        for raw, (uidvalidity, last_uid) in self._pending.items():
            folders[raw] = {
                "uidvalidity": int(uidvalidity),
                "last_uid": int(last_uid),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        acct["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)
        n = len(self._pending)
        self._pending = {}
        return n

    # ------------------------------------------------------------------ #
    #  取件
    # ------------------------------------------------------------------ #

    def _record_id(self, message_id: str, fallback: str) -> str:
        """稳定且文件系统安全的记录 id（内嵌图会以 id 作目录名）。"""
        mid = (message_id or "").strip()
        if mid.startswith("<") and mid.endswith(">"):
            mid = mid[1:-1]
        mid = mid.strip().lower()
        key = mid or "uid:%s" % fallback
        digest = hashlib.sha1(("%s|%s" % (self.account, key)).encode("utf-8")).hexdigest()
        return "%s_%s" % (self.ID_PREFIX, digest)

    def _build_record(self, raw: bytes, uid: str, folder: str, unread: bool) -> Optional[Dict]:
        """RFC822 原文 → 与 Outlook 路径同构的记录字典。"""
        try:
            msg = email.message_from_bytes(raw)
        except Exception as exc:                  # noqa: BLE001
            logger.warning("报文解析失败 uid=%s: %s", uid, exc)
            return None

        message_id = self._message_id(msg)
        email_id = self._record_id(message_id, "%s:%s" % (folder, uid))
        subject = _decode_hdr(msg.get("Subject")) or "(无主题)"
        sender_name, sender_addr = parseaddr(_decode_hdr(msg.get("From")))
        time_str, ts = self._parse_date(msg)
        text_body, html_body, inline, attachments = self._walk_parts(msg)
        html_body = self._save_inline(email_id, html_body, inline)
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
            "folder": folder,
            "account": self.account,
            "has_attachment": bool(attachments),
            "attachments": attachments,
        }

    def _header_only_record(self, raw: bytes, uid: str, folder: str, unread: bool, err) -> Optional[Dict]:
        """_build_record 失败时的兜底：只取头信息，正文写明解析失败。"""
        try:
            msg = email.message_from_bytes(raw)
            message_id = self._message_id(msg)
            subject = _decode_hdr(_hstr(msg.get("Subject"))) or "(无主题)"
            sender_name, sender_addr = parseaddr(_decode_hdr(_hstr(msg.get("From"))))
            time_str, ts = self._parse_date(msg)
        except Exception:                         # noqa: BLE001
            return None
        return {
            "id": self._record_id(message_id, "%s:%s" % (folder, uid)),
            "message_id": message_id,
            "subject": subject,
            "body": "（正文解析失败：%s）" % str(err)[:200],
            "html_body": "",
            "sender": sender_name or sender_addr,
            "sender_email": sender_addr,
            "recipients": [],
            "time": time_str,
            "received_timestamp": ts,
            "unread": unread,
            "folder": folder,
            "account": self.account,
            "has_attachment": False,
            "attachments": [],
        }

    @staticmethod
    def _uidvalidity(conn: imaplib.IMAP4) -> int:
        try:
            typ, data = conn.response("UIDVALIDITY")
            if data and data[0]:
                return int(re.sub(rb"[^\d]", b"", data[0]) or 0)
        except Exception:                         # noqa: BLE001
            pass
        return 0

    def fetch(
        self,
        password: str,
        since_days: Optional[int] = None,
        max_per_folder: int = 500,
        only_folders: Optional[Sequence[str]] = None,
        on_progress: Optional[Callable[[str, str, int, str], None]] = None,
    ) -> List[Dict]:
        """按文件夹增量取件（只读，不改服务端 \\Seen）。

        Parameters
        ----------
        since_days:
            首次取某文件夹（无状态）时的回看天数；None 表示不限（取全部）。
            已有 UID 水位的文件夹不受此参数影响，永远从水位继续。
        max_per_folder:
            每个文件夹单轮最多取多少封，防止首轮把大文件夹一次性拉爆。
        only_folders:
            只取这些文件夹（按显示名或原始名匹配），用于分批验证。
        """
        conn = self._connect(password)
        records: List[Dict] = []
        try:
            wanted = {s.strip().lower() for s in (only_folders or ())}
            for raw, disp, _flags in self.list_folders(conn):
                if wanted and disp.lower() not in wanted and raw.lower() not in wanted:
                    continue
                try:
                    got = self._fetch_folder(
                        conn, raw, disp, since_days, max_per_folder, records, on_progress)
                    if got:
                        logger.info("[IMAP %s] %s 取到 %d 封", self.account, disp, got)
                except Exception as exc:          # noqa: BLE001
                    # 单个文件夹失败不影响其余（与 Outlook 后端的容错口径一致）
                    logger.warning("[IMAP %s] 文件夹 %s 取件失败：%s", self.account, disp, exc)
            return records
        finally:
            self._logout(conn)

    def _fetch_folder(
        self,
        conn: imaplib.IMAP4,
        raw: str,
        disp: str,
        since_days: Optional[int],
        max_per_folder: int,
        sink: List[Dict],
        on_progress: Optional[Callable[[str, str, int, str], None]],
    ) -> int:
        typ, _ = conn.select(self._quote(raw), readonly=True)  # readonly：绝不改服务端状态
        if typ != "OK":
            raise RuntimeError("SELECT 失败：%s" % typ)

        uidvalidity = self._uidvalidity(conn)
        known_validity, last_uid = self.folder_state(raw)
        if known_validity and uidvalidity and uidvalidity != known_validity:
            # 服务端重建了文件夹，UID 体系已作废，水位必须清零重来
            logger.warning("[IMAP %s] %s 的 UIDVALIDITY 变了（%s → %s），水位清零",
                           self.account, disp, known_validity, uidvalidity)
            last_uid = 0

        if last_uid:
            criteria = ["UID", "%d:*" % (last_uid + 1)]
        elif since_days:
            since = (datetime.now() - timedelta(days=int(since_days))).strftime("%d-%b-%Y")
            criteria = ["SINCE", since]
        else:
            criteria = ["ALL"]

        typ, data = conn.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise RuntimeError("UID SEARCH 失败：%s" % typ)
        uids = [u.decode("ascii", "replace") for u in (data[0] or b"").split()]
        # UID n:* 在没有新邮件时也会返回最后一封，需按水位过滤掉
        if last_uid:
            uids = [u for u in uids if u.isdigit() and int(u) > last_uid]
        if not uids:
            self._pending[raw] = (uidvalidity, last_uid)
            return 0
        uids = uids[:max_per_folder]

        got = 0
        max_uid = last_uid
        for uid in uids:
            try:
                # BODY.PEEK[] 而非 RFC822：后者会把邮件标成已读，抹掉真实未读状态
                typ, parts = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
            except Exception as exc:              # noqa: BLE001
                logger.warning("[IMAP %s] %s uid=%s 取信失败：%s", self.account, disp, uid, exc)
                continue
            if typ != "OK" or not parts:
                continue
            flags_txt = ""
            body: Optional[bytes] = None
            for part in parts:
                if isinstance(part, tuple):
                    head = part[0].decode("utf-8", "replace") if isinstance(part[0], bytes) else ""
                    flags_txt += head
                    body = part[1] if isinstance(part[1], bytes) else body
                elif isinstance(part, bytes):
                    flags_txt += part.decode("utf-8", "replace")
            if not body:
                continue
            unread = "\\seen" not in flags_txt.lower()
            try:
                rec = self._build_record(body, uid, disp, unread)
            except Exception as exc:              # noqa: BLE001
                # 单封解析失败不能拖垮整个文件夹（否则水位不动，之后的邮件全被挡住）：
                # 退化成只有头信息的记录入库，正文注明解析失败，邮件本身不丢。
                logger.warning("[IMAP %s] %s uid=%s 解析失败，按头信息入库：%s", self.account, disp, uid, exc)
                rec = self._header_only_record(body, uid, disp, unread, exc)
            if not rec:
                continue
            sink.append(rec)
            got += 1
            try:
                max_uid = max(max_uid, int(uid))
            except ValueError:
                pass
            if on_progress and got % 10 == 0:
                on_progress(self.account, disp, len(sink), rec.get("subject", ""))

        self._pending[raw] = (uidvalidity, max_uid)
        return got

    # ------------------------------------------------------------------ #
    #  与 EmailExtractor 对齐的外部 API
    # ------------------------------------------------------------------ #

    def get_all_emails(self, password: str, days_ago: Optional[int] = None,
                       on_progress=None, max_per_folder: int = 2000) -> List[Dict]:
        return self.fetch(password, since_days=days_ago,
                          max_per_folder=max_per_folder, on_progress=on_progress)

    def get_incremental_emails(self, password: str, days_ago: int = 30,
                               on_progress=None, max_per_folder: int = 500) -> List[Dict]:
        return self.fetch(password, since_days=days_ago,
                          max_per_folder=max_per_folder, on_progress=on_progress)


def _draft_folder(ext: "ImapExtractor", conn) -> Optional[str]:
    """找服务端的草稿箱原始名（用于 APPEND）。

    优先 RFC 6154 的 \\Drafts special-use 标志——这是唯一与语言无关的判据；
    认不到再退回按显示名匹配。都找不到就返回 None，由调用方跳过，
    **不自作主张建文件夹**（建错了用户还得手工清）。
    """
    names = {"drafts", "draft", "草稿", "草稿箱"}
    # 不能用 ext.list_folders()：它按 skip_folders 过滤，而草稿箱正在跳过清单里
    # （同步侧刻意跳过草稿箱——这也是回写不会产生重复邮件的依据）。
    # 用它来找草稿箱，最常见的情形（就叫 Drafts / 草稿）反而找不到。
    typ, items = conn.list()
    if typ != "OK":
        logger.warning("[IMAP %s] LIST 失败，无法定位草稿箱: %s", ext.username, typ)
        return None
    fallback = None
    for item in items or []:
        parsed = ext._parse_list_item(item)
        if not parsed:
            continue
        flags, _delim, raw = parsed
        low = (flags or "").lower()
        if "\\noselect" in low:
            continue
        if "\\drafts" in low or "\\draft" in low:
            return raw                      # special-use 标志与语言无关，优先
        disp = ext.folder_display_name(raw)
        if (disp or "").strip().lower() in names:
            fallback = fallback or raw
    return fallback


def _sent_folder(ext: "ImapExtractor", conn) -> Optional[str]:
    """找服务端“已发送”文件夹原始名。

    与 _draft_folder 同理：优先 RFC 6154 的 \\Sent special-use 标志，
    再按显示名匹配；同样不能用 list_folders（它按 skip_folders 过滤）。
    """
    names = {"sent", "sent items", "sent messages",
             "已发送", "已发送邮件", "发件箱"}
    typ, items = conn.list()
    if typ != "OK":
        return None
    fallback = None
    for item in items or []:
        parsed = ext._parse_list_item(item)
        if not parsed:
            continue
        flags, _d, raw = parsed
        low = (flags or "").lower()
        if "\\noselect" in low:
            continue
        if "\\sent" in low:
            return raw
        if (ext.folder_display_name(raw) or "").strip().lower() in names:
            fallback = fallback or raw
    return fallback


def append_to_sent(ext: "ImapExtractor", password: str, raw_bytes: bytes) -> str:
    """把已发送副本 APPEND 到服务端“已发送”文件夹，返回落点文件夹名。

    Outlook 发信会自动做这件事，SMTP 不会 —— 不补的话在手机/其它客户端里
    看不到自己发过什么。best-effort：失败只记 warning。
    """
    conn = ext._connect(password)
    try:
        folder = _sent_folder(ext, conn)
        if not folder:
            logger.warning("[IMAP %s] 未找到已发送文件夹，跳过存副本", ext.username)
            return ""
        typ, _data = conn.append(ext._quote(folder), r"(\\Seen)", None, raw_bytes)
        if typ != "OK":
            logger.warning("[IMAP %s] 已发送副本 APPEND 失败: %s", ext.username, typ)
            return ""
        return folder
    finally:
        ImapExtractor._logout(conn)


def push_draft_to_imap(ext: "ImapExtractor", password: str, *, subject: str,
                       body: str, to_addr: str = "", cc_addr: str = "",
                       is_html: bool = False,
                       drop_uid: str = "") -> Tuple[str, str]:
    """把一封草稿 APPEND 到服务端草稿箱。返回 (folder_raw, uid)。

    单向：只 local -> IMAP。反向同步才是冲突规则的来源，这里刻意不做。
    APPEND 到草稿箱不是投递，不构成外发能力。
    drop_uid 非空时先删掉那封旧副本，避免手机上堆一串同名草稿。
    """
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid

    msg = EmailMessage()
    msg["From"] = ext.username
    if to_addr:
        msg["To"] = to_addr
    if cc_addr:
        msg["Cc"] = cc_addr
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    # 打个标记，便于人在服务端一眼认出来源；同步侧本就跳过草稿箱，不依赖它去重。
    msg["X-Quillbox-Draft"] = "1"
    if is_html:
        msg.set_content("（此草稿为 HTML，请在支持富文本的客户端查看）")
        msg.add_alternative(body or "", subtype="html")
    else:
        msg.set_content(body or "")

    conn = ext._connect(password)
    try:
        folder = _draft_folder(ext, conn)
        if not folder:
            logger.warning("[IMAP %s] 未找到草稿箱，跳过回写", ext.username)
            return ("", "")

        if drop_uid:
            try:
                conn.select(ext._quote(folder))
                conn.uid("STORE", drop_uid, "+FLAGS", r"(\Deleted)")
                conn.expunge()
            except Exception as exc:      # 删不掉旧副本不该挡住新副本
                logger.warning("[IMAP %s] 删除旧草稿副本 uid=%s 失败: %s",
                               ext.username, drop_uid, exc)

        typ, data = conn.append(ext._quote(folder), r"(\Draft)", None,
                                msg.as_bytes())
        if typ != "OK":
            logger.warning("[IMAP %s] APPEND 草稿失败: %s %s", ext.username, typ, data)
            return ("", "")

        # 多数服务端会在 APPENDUID 里回 uid；回不了就用 SEARCH 兜一下
        uid = ""
        try:
            blob = b" ".join(x for x in data if isinstance(x, bytes)).decode(
                "latin-1", "replace")
            import re as _re2
            m = _re2.search(r"APPENDUID\s+\d+\s+(\d+)", blob)
            if m:
                uid = m.group(1)
        except Exception:
            uid = ""
        if not uid:
            try:
                conn.select(ext._quote(folder))
                typ2, res = conn.uid("SEARCH", None, "HEADER", "Message-ID",
                                     msg["Message-ID"])
                if typ2 == "OK" and res and res[0]:
                    uid = res[0].split()[-1].decode()
            except Exception:
                uid = ""
        return (folder, uid)
    finally:
        ImapExtractor._logout(conn)


def from_config(cfg: dict, account: str) -> ImapExtractor:
    """按 config.yaml 的 imap 段构造实例。

    期望结构::

        imap:
          enabled: true
          accounts:
            you@example.com:
              host: imap.example.com
              port: 993
              ssl: true
              timeout: 60
              username: you@example.com
              skip_folders: [草稿, 草稿箱, 已删除邮件, 待删除邮件, 垃圾邮件, Trash]
    """
    section = (cfg or {}).get("imap") or {}
    accounts = section.get("accounts") or {}
    acc = accounts.get(account)
    if not acc:
        raise KeyError("config.yaml 的 imap.accounts 下没有 %s" % account)
    return ImapExtractor(
        host=acc["host"],
        username=acc.get("username") or account,
        account=account,
        port=int(acc.get("port", 993)),
        use_ssl=bool(acc.get("ssl", True)),
        timeout=int(acc.get("timeout", 60)),
        skip_folders=acc.get("skip_folders", DEFAULT_SKIP_FOLDERS),
        verify_tls=bool(acc.get("verify_tls", True)),  # TLS-VERIFY-DEFAULT-20260930
    )
