"""按需取回附件内容。

为什么按需而不是同步时全量存盘
------------------------------
库里 3956 封有附件。同步时全存要占大量磁盘，而且**已有邮件还得整批重拉一遍**才有内容。
按需取回则对存量邮件立即可用：首次点击时回邮件服务器取该封原文、抽出附件、
顺手缓存到本地，之后直接命中缓存。

按账户分派，不按 id 前缀
------------------------
记录 id 的前缀只说明"当初是哪条后端同步进来的"，但邮件**现在**还躺在原邮箱里。
所以按 ``account`` 决定用哪条通道：

* 配在 ``imap.accounts`` 下的账户 → IMAP
* 配在 ``ews.accounts`` 下的账户  → EWS
* 都没配的账户（例如只读权限不够的共享邮箱）→ 取不了

这一点很要紧：早先经 Outlook 同步进来的邮件，按 id 前缀会被判成
"没有可用后端"，按账户分派就都能取回。

三条硬约束
----------
1. **绝不改服务端状态**：IMAP 用 ``select(readonly=True)`` + ``BODY.PEEK[]``；
   EWS 只读 ``mime_content``。取附件不该把邮件标成已读。
2. **文件名一律 basename 化**：附件名来自邮件头，是不可信输入。
   ``../`` 之类必须挡掉，否则能写到目录外。
3. **不对 MCP 面开放**：这个模块只服务 Web 界面。Quillbox 跑在本机、用户经 RDP 操作，
   附件下到本机磁盘不构成外流；但经 Agent 送出 Windows 是红线，
   见 ``test_mcp_redlines.py``。
"""
from __future__ import annotations

import email as _email
import hashlib
import logging
import mimetypes
import os
import re
from email.header import decode_header, make_header
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = _ROOT / "data" / "attachments"

# 附件名来自邮件头，属不可信输入
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class AttachmentError(Exception):
    """取附件失败。message 会直接给用户看，所以要说人话。"""


def safe_name(name: str) -> str:
    """basename 化 + 去掉危险字符。防的是 ``../../x`` 这类路径穿越。"""
    n = os.path.basename(str(name or "").replace("\\", "/")).strip()
    n = _BAD.sub("_", n)
    n = n.lstrip(".") or "attachment"
    return n[:180]


def _hdr(v) -> str:
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:                                  # noqa: BLE001
        return str(v)


def cache_path(email_id: str, name: str) -> Path:
    """缓存路径。用 email_id 的哈希做目录名 —— id 里可能有不适合做目录名的字符
    （Outlook 的 EntryID 是一长串十六进制，还好；但不做假设更稳）。"""
    d = hashlib.sha1(str(email_id).encode("utf-8")).hexdigest()[:16]
    return CACHE_ROOT / d / safe_name(name)


def guess_mime(name: str) -> str:
    t, _ = mimetypes.guess_type(safe_name(name))
    # 猜不出来时用 octet-stream，让浏览器直接下载而不是尝试渲染
    return t or "application/octet-stream"


# ---------------------------------------------------------------- #
# 从 MIME 原文里抽出附件
# ---------------------------------------------------------------- #
def extract_from_mime(raw: bytes, want: str) -> Optional[Tuple[str, bytes]]:
    """在 RFC822 原文里找名为 want 的附件，返回 (真实文件名, 字节)。

    比较时两边都 basename 化并去掉大小写差异 —— 邮件头里的文件名可能带编码、
    也可能和库里存的略有出入（库里存的就是解码后的名字，但空格/大小写会飘）。
    """
    try:
        msg = _email.message_from_bytes(raw)
    except Exception as exc:                           # noqa: BLE001
        raise AttachmentError("邮件原文解析失败：%s" % type(exc).__name__)

    def _key(v: str) -> str:
        # 忽略空白与大小写：库里存的名字与邮件头里的偶有空白差异
        return re.sub(r"\s+", "", safe_name(v).lower())

    target = safe_name(want).lower()
    target_key = _key(want)
    fallback = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = _hdr(part.get_filename())
        if not fn:
            continue
        cur = safe_name(fn).lower()
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:                              # noqa: BLE001
            continue
        if not data:
            continue
        if cur == target:
            return (safe_name(fn), data)
        # 兜底只认"忽略空白后同名"。
        # 原来写的是"只有一个附件就返回它"，太宽 —— 实测请求一个根本不存在的
        # 名字也会拿到那个唯一附件。请求不存在的名字必须报错，
        # 不能悄悄换一个文件给你。
        if fallback is None and _key(fn) == target_key:
            fallback = (safe_name(fn), data)
    return fallback


# ---------------------------------------------------------------- #
# 各通道取原文
# ---------------------------------------------------------------- #
def _raw_via_ews(cfg: dict, account: str, folder: str, message_id: str) -> bytes:
    from exchangelib import (Account, Configuration, Credentials, DELEGATE,
                             FailFast, NTLM)
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
    from core import win_credential

    acc_cfg = ((cfg.get("ews") or {}).get("accounts") or {}).get(account) or {}
    host = acc_cfg.get("host")
    if not host:
        raise AttachmentError("该账户未配置 EWS")
    if not acc_cfg.get("verify_tls", True):  # TLS-VERIFY-DEFAULT-20260930
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    pw = win_credential.get_password(account)
    if not pw:
        raise AttachmentError("取不到该账户的凭据")
    try:
        creds = Credentials(username=acc_cfg.get("username") or account, password=pw)
        conf = Configuration(server=host, credentials=creds, auth_type=NTLM,
                             retry_policy=FailFast())
        acct = Account(primary_smtp_address=account, config=conf,
                       autodiscover=False, access_type=DELEGATE)
    finally:
        del pw

    mid = message_id.strip()
    candidates = [mid if mid.startswith("<") else "<%s>" % mid, mid]
    # 先在记录里那个文件夹找；找不到再全邮箱扫一遍（改过文件夹的情况）
    folders = []
    try:
        for f in acct.msg_folder_root.walk():
            if (getattr(f, "name", "") or "") == folder:
                folders.append(f)
    except Exception:                                  # noqa: BLE001
        pass
    try:
        folders.append(acct.inbox)
    except Exception:                                  # noqa: BLE001
        pass

    for f in folders:
        for cand in candidates:
            try:
                items = list(f.filter(message_id=cand).only("mime_content")[:1])
            except Exception:                          # noqa: BLE001
                continue
            if items:
                raw = getattr(items[0], "mime_content", None)
                if raw:
                    return raw.encode("utf-8", "replace") if isinstance(raw, str) else raw
    raise AttachmentError("在服务器上找不到这封邮件（可能已被删除或移动）")


def _raw_via_imap(cfg: dict, account: str, folder: str, message_id: str,
                  ts: float = 0.0) -> bytes:
    # ts 现在不参与定位（部分服务器上日期搜索也不可用），保留参数避免调用方改动。
    """按 Message-ID 取原文。

    **不用 `SEARCH HEADER Message-ID`**：部分服务器（例如 Domino）上它在所有文件夹都返回空，
    HEADER 搜索不可用。
    改成服务端按收信日期缩窗（`SINCE`/`BEFORE` 是 IMAP 基础能力，必定支持），
    再本地 FETCH 各候选的 Message-ID 头逐个比对。全程 PEEK，不改已读状态。
    """
    from core.imap_extractor import from_config as imap_from_config
    from core import win_credential

    imap_cfg = cfg.get("imap") or {}
    if account not in ((imap_cfg.get("accounts") or {})):
        raise AttachmentError("该账户未配置 IMAP")
    ext = imap_from_config({"imap": imap_cfg}, account)
    pw = win_credential.get_password(account)
    if not pw:
        raise AttachmentError("取不到该账户的凭据")
    try:
        conn = ext._connect(pw)                        # noqa: SLF001
    finally:
        del pw

    want = message_id.strip().strip("<>").lower()
    why = []          # 各文件夹的失败原因，最终带进错误信息（否则零可观测）

    def _norm(v: str) -> str:
        return (v or "").strip().strip("<>").lower()

    try:
        # list_folders 返回 (原始名, 显示名, 标志) 三元组
        items = []
        try:
            items = ext.list_folders(conn)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("[附件] 列举文件夹失败: %s", exc)
        order = []
        for it in items:
            raw = it[0] if isinstance(it, (tuple, list)) else it
            disp = it[1] if isinstance(it, (tuple, list)) and len(it) > 1 else raw
            (order.insert(0, raw) if disp == folder else order.append(raw))
        if not order:
            order = ["INBOX"]

        # 每批取多少封的头。头很小，主要成本是往返次数。
        CHUNK = 400
        # 单个文件夹最多扫多少封，兜住极端大文件夹
        MAX_SCAN = 3000

        for raw_folder in order:
            try:
                typ, data = conn.select(ext._quote(raw_folder), readonly=True)  # noqa: SLF001
                if typ != "OK":
                    continue
                total = int(data[0]) if data and data[0] else 0
                if total <= 0:
                    why.append("%s: 空文件夹" % raw_folder)
                    continue
                # 从最新往旧扫：要找的邮件通常是近期的
                hi = total
                scanned = 0
                hit_seq = None
                while hi >= 1 and scanned < MAX_SCAN and hit_seq is None:
                    lo = max(1, hi - CHUNK + 1)
                    try:
                        typ, resp = conn.fetch(
                            "%d:%d" % (lo, hi),
                            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    except Exception as exc:           # noqa: BLE001
                        why.append("%s 取头失败: %s" % (raw_folder,
                                                        type(exc).__name__))
                        logger.warning("[附件] 取头失败 %s %d:%d: %s",
                                       raw_folder, lo, hi, exc)
                        break
                    if typ != "OK" or not resp:
                        break
                    cur = None
                    for chunk in resp:
                        if not (isinstance(chunk, tuple) and len(chunk) > 1):
                            continue
                        # FETCH 应答元组的 [0] 是应答前缀，序号在里面；正文只在 [1]
                        m = re.match(rb"^\s*(\d+)\s", chunk[0] or b"")
                        if m:
                            cur = m.group(1).decode()
                        head = (chunk[1] or b"").decode("utf-8", "replace")
                        mm = re.search(r"(?i)message-id:\s*(.+)", head)
                        if mm and _norm(mm.group(1)) == want:
                            hit_seq = cur
                            break
                    scanned += (hi - lo + 1)
                    hi = lo - 1
                if hit_seq is None:
                    continue
                typ, resp = conn.fetch(hit_seq, "(BODY.PEEK[])")
                if typ != "OK" or not resp:
                    continue
                for chunk in resp:
                    if isinstance(chunk, tuple) and len(chunk) > 1 and chunk[1]:
                        return chunk[1]
            except Exception as exc:                   # noqa: BLE001
                why.append("%s: %s(%s)" % (raw_folder, type(exc).__name__,
                                           str(exc)[:60]))
                logger.warning("[附件] IMAP 在 %s 里找失败: %s", raw_folder, exc)
        detail = ("；".join(why[:3]) if why
                  else "已扫遍 %d 个文件夹均无匹配" % len(order))
        raise AttachmentError("在服务器上找不到这封邮件（%s）" % detail)
    finally:
        try:
            conn.logout()
        except Exception:                              # noqa: BLE001
            pass


# ---------------------------------------------------------------- #
# 对外入口
# ---------------------------------------------------------------- #
def fetch(cfg: dict, email_row: dict, name: str) -> Tuple[str, bytes, str]:
    """取一个附件，返回 (文件名, 字节, mime)。命中缓存则不联网。"""
    email_id = str(email_row.get("id") or "")
    account = (email_row.get("account") or "").strip()
    folder = (email_row.get("folder") or "").strip()
    message_id = (email_row.get("message_id") or "").strip()
    fname = safe_name(name)
    if not email_id or not fname:
        raise AttachmentError("参数不完整")

    # 1) 缓存
    cp = cache_path(email_id, fname)
    if cp.exists() and cp.stat().st_size > 0:
        return (fname, cp.read_bytes(), guess_mime(fname))

    if not message_id:
        raise AttachmentError("这封邮件没有 Message-ID，无法回服务器定位原文")

    # 2) 按账户选通道 —— 不看 id 前缀：id 前缀只说明当初谁同步的，
    #    邮件现在仍躺在原邮箱里（早先经 Outlook 同步进来的邮件因此也能取）
    ews_accounts = ((cfg.get("ews") or {}).get("accounts") or {})
    imap_accounts = ((cfg.get("imap") or {}).get("accounts") or {})
    if account in ews_accounts:
        raw = _raw_via_ews(cfg, account, folder, message_id)
    elif account in imap_accounts:
        raw = _raw_via_imap(cfg, account, folder, message_id,
                            float(email_row.get("received_timestamp") or 0))
    else:
        raise AttachmentError("账户 %s 没有可用的取件通道（该账户未配置 IMAP / EWS，或没有访问权限）"
                              % account)

    got = extract_from_mime(raw, fname)
    if not got:
        raise AttachmentError("邮件原文里找不到名为「%s」的附件" % fname)
    real_name, data = got

    # 3) 落缓存（失败不影响这次返回）
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(cp.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(cp)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("[附件] 写缓存失败（不影响本次下载）: %s", exc)

    return (real_name, data, guess_mime(real_name))
