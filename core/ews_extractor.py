"""EWS 取件后端（Exchange 账户用）。

为什么是 EWS 而不是 IMAP/POP3
------------------------------
很多 Exchange（如 Exchange 2019 / IIS 10.0）部署的实际情况：

* **IMAP 常常没有启用** —— 993/143 直接超时，这条路不存在。
* **POP3 可用，但只能读收件箱。** 服务端规则会把大量邮件移出收件箱（按项目、
  通知类型分的文件夹，已发送、已删除），走 POP3 等于直接丢掉这些邮件 —— 不是
  "少了分类"，是"少了邮件"。因此放弃 POP3。
* **EWS 可用**（401 + ``Negotiate, NTLM``），且能拿到文件夹结构、已读状态、
  原始 MIME，还能发信。这是唯一能完整覆盖需求的通道。

三条硬约束（与 :mod:`core.imap_extractor` 同款，刻意保持一致）
--------------------------------------------------------------
1. **绝不改服务端状态。** 只读取 ``mime_content`` / ``is_read``，从不写 ``is_read``、
   不移动、不删除。EWS 的 GetItem 不会像 IMAP 的 ``RFC822`` 那样隐式打上已读标记，
   但仍不得调用 ``item.save()`` 或任何 mark-as-read 辅助 —— 那会抹掉你在
   Outlook/OWA 里的未读状态（这个邮箱当前有 836 封未读）。
2. **状态推进由调用方决定。** :meth:`fetch` 只把"本轮扫到的最新收信时间"记在内存，
   要等调用方成功入库后显式调 :meth:`commit_state` 才落盘。空跑不消耗增量窗口。
3. **记录结构与另几条后端同构。** MIME 解析直接复用 :mod:`core.pop3_extractor`
   的实现（同一份代码，避免字段漂移），只有 ``folder`` 与 ``id`` 前缀不同。

增量水位为什么用时间戳而不是 SyncState
--------------------------------------
EWS 原生的增量机制是 ``SyncFolderItems`` + SyncState。但本项目已经吃过
"先推进水位、中途失败导致这批邮件被永久跳过"的亏（见 daemon 里 IMAP 水位那段注释）。
SyncState 一旦失效或错位，丢失是静默的、不可发现的；而时间戳水位配上重叠窗口，
最坏结果只是重复取一遍，而重复取是无害的 —— ``id`` 由
``account|message_id`` 哈希而来、且 ``emails.id`` 是主键，重复记录只会 upsert。
**宁可重复，不可遗漏。**
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.pop3_extractor import INLINE_ROOT, Pop3Extractor

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = _ROOT / "data" / ".ews_state.json"

# 与 IMAP 后端同一套默认跳过项（按显示名小写比较）
from core.imap_extractor import DEFAULT_SKIP_FOLDERS, INBOX_NAME  # noqa: E402

# Exchange 自建的非邮件容器，取件时一律跳过：
# Recipient Cache 是收件人缓存、{GUID} 形态的是系统隐藏文件夹。
_SYSTEM_FOLDER_NAMES = ("recipient cache", "conversation action settings",
                        "quick step settings", "sync issues", "conflicts",
                        "local failures", "server failures")

# 只取邮件类文件夹。日历/联系人/任务的 CONTAINER_CLASS 不是 IPF.Note，
# 本轮范围明确不含日历，所以直接按容器类过滤。
_MAIL_CONTAINER_CLASSES = ("IPF.Note", None, "")

# 重叠窗口：水位往前回退这么多秒再查，容忍服务端时间抖动与同秒多封。
# 重复取是无害的（id 去重），漏取是致命的。
OVERLAP_SECONDS = 180


class EwsExtractor:
    """按文件夹增量取件，产出与 EmailExtractor / Pop3Extractor / ImapExtractor 同构的记录。"""

    ID_PREFIX = "ews"

    # ---- 复用 POP3 后端的 MIME 解析（同一份实现，防止字段漂移）----------- #
    _message_id = staticmethod(Pop3Extractor._message_id)
    _parse_date = staticmethod(Pop3Extractor._parse_date)
    _collect_recipients = staticmethod(Pop3Extractor._collect_recipients)
    # 这两个是**实例方法**（带 self），必须平赋值；
    # 包成 staticmethod 会剥掉隐式 self，调用时报 missing 'msg'（踩过）。
    _walk_parts = Pop3Extractor._walk_parts
    _save_inline = Pop3Extractor._save_inline

    def __init__(self, host: str, username: str, account: str,
                 *, verify_tls: bool = False, timeout: int = 60,
                 skip_folders=None, max_per_folder: int = 2000,
                 initial_days: Optional[int] = None,
                 chunk_size: int = 15,
                 inline_root: Optional[Path] = None):
        # 复用的 _save_inline 依赖 self.inline_root（内嵌图按 email_id 建目录）。
        # 三条后端共用同一个根，正文里的 cid: 引用才能跨后端一致解析。
        self.inline_root = Path(inline_root) if inline_root else INLINE_ROOT
        self.host = host
        self.username = username          # 登录名，如 DOMAIN\user
        self.account = account            # 主 SMTP 地址，作为库里的 account 值
        self.verify_tls = bool(verify_tls)
        self.timeout = int(timeout or 60)
        self.max_per_folder = int(max_per_folder or 2000)
        self.initial_days = initial_days
        # 取正文的批大小。响应体积 ≈ chunk × 单封大小；100（exchangelib 默认）
        # 在部分 Exchange 上会被 IIS reset；15 封约 1MB 上下，较稳妥。
        self.chunk_size = max(1, int(chunk_size or 15))
        skips = [str(s).strip().lower() for s in (skip_folders or []) if str(s).strip()]
        self.skip_folders = tuple(skips) if skips else tuple(
            s.lower() for s in DEFAULT_SKIP_FOLDERS)
        self._pending: Dict[str, float] = {}   # 待提交水位：folder -> 最新收信时间
        self._acct = None

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def _connect(self, password: str):
        """建立 EWS 连接。FailFast：不做内部重试，避免密码错误被放大成多次尝试
        （会触发域账户锁定）。"""
        from exchangelib import (Account, Configuration, Credentials, DELEGATE,
                                 FailFast, NTLM)
        from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

        if not self.verify_tls:
            # 内网 Exchange 的证书通常不是公共 CA 签发；与 imap/smtp 的
            # verify_tls: false 保持一致。
            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
        BaseProtocol.TIMEOUT = self.timeout

        creds = Credentials(username=self.username, password=password)
        cfg = Configuration(server=self.host, credentials=creds,
                            auth_type=NTLM, retry_policy=FailFast())
        return Account(primary_smtp_address=self.account, config=cfg,
                       autodiscover=False, access_type=DELEGATE)

    def check_login(self, password: str) -> bool:
        """一次最小请求验证凭据。失败不重试。"""
        try:
            acct = self._connect(password)
            _ = acct.inbox.total_count
            return True
        except Exception as exc:                       # noqa: BLE001
            logger.warning("[EWS %s] 登录失败: %s", self.account,
                           type(exc).__name__)
            return False

    # ------------------------------------------------------------------ #
    # 水位（与 IMAP 后端同样的"暂存—提交"两段式）
    # ------------------------------------------------------------------ #
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            return {}

    def _save_state(self, state: dict) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(STATE_PATH)

    def has_state(self) -> bool:
        return bool((self._load_state().get(self.account) or {}))

    def folder_watermark(self, folder: str) -> Optional[float]:
        acc = self._load_state().get(self.account) or {}
        v = (acc.get(folder) or {}).get("last_received")
        try:
            return float(v) if v is not None else None
        except Exception:                              # noqa: BLE001
            return None

    def commit_state(self) -> int:
        """把本轮暂存的水位落盘。调用方成功入库后才该调用。"""
        if not self._pending:
            return 0
        state = self._load_state()
        acc = state.setdefault(self.account, {})
        for folder, ts in self._pending.items():
            cur = (acc.get(folder) or {}).get("last_received") or 0
            # 只前进不后退：重跑历史窗口不该把水位拉回去
            if float(ts) > float(cur):
                acc[folder] = {"last_received": float(ts),
                               "updated_at": int(time.time())}
        n = len(self._pending)
        self._save_state(state)
        self._pending.clear()
        return n

    # ------------------------------------------------------------------ #
    # 记录构造
    # ------------------------------------------------------------------ #
    def _record_id(self, message_id: str, fallback: str) -> str:
        """与 IMAP/POP3 后端同一套 id 规则：account + 归一化 message_id 的哈希。
        跨后端去重靠它 —— 同一封邮件无论从哪条通道进来，id 都相同。"""
        mid = (message_id or "").strip()
        if mid.startswith("<") and mid.endswith(">"):
            mid = mid[1:-1]
        mid = mid.strip().lower()
        key = mid or "ews:%s" % fallback
        digest = hashlib.sha1(("%s|%s" % (self.account, key)).encode("utf-8")).hexdigest()
        return "%s_%s" % (self.ID_PREFIX, digest)

    def _build_record(self, raw: bytes, item_key: str, folder: str,
                      unread: bool) -> Optional[Dict]:
        """RFC822 原文 → 与另几条后端同构的记录字典。

        走的是和 IMAP/POP3 完全相同的解析函数，所以字段集合天然一致；
        test_ews_extractor.py 里有一条断言在守这件事。
        """
        import email as _email
        try:
            msg = _email.message_from_bytes(raw)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("[EWS %s] 报文解析失败 %s: %s", self.account,
                           item_key, exc)
            return None

        message_id = self._message_id(msg)
        _time_str, _ts = self._parse_date(msg)
        # email.message_from_bytes 对垃圾字节非常宽容，不会抛异常，而是产出一个
        # 空壳 Message。这种记录既无 message_id（无法跨后端去重）又无收信时间
        # （界面上会显示成 1970 年），留着只会污染库，直接丢弃。
        if not message_id and not _ts:
            logger.warning("[EWS %s] 跳过无法识别的报文 %s（无 message_id 且无日期）",
                           self.account, item_key)
            return None
        email_id = self._record_id(message_id, "%s:%s" % (folder, item_key))
        subject = self._hdr(msg, "Subject") or "(无主题)"
        from email.utils import parseaddr
        sender_name, sender_addr = parseaddr(self._hdr(msg, "From"))
        time_str, ts = _time_str, _ts
        text_body, html_body, inline, attachments = self._walk_parts(msg)
        html_body = self._save_inline(email_id, html_body, inline)
        body = self._clean(text_body) if text_body else self._clean(html_body)

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

    @staticmethod
    def _hdr(msg, name: str) -> str:
        from email.header import decode_header, make_header
        raw = msg.get(name)
        if not raw:
            return ""
        try:
            return str(make_header(decode_header(raw)))
        except Exception:                              # noqa: BLE001
            return str(raw)

    @staticmethod
    def _clean(text: Optional[str]) -> str:
        from core.pop3_extractor import _clean_body  # 同一份清洗实现
        return _clean_body(text or "")

    # ------------------------------------------------------------------ #
    # 文件夹枚举
    # ------------------------------------------------------------------ #
    def _msg_root_with_retry(self, acct, tries: int = 3):
        """取邮箱根，失败重试。

        部分 Exchange 上这一步的 ConnectionReset 很频繁（可达近一半轮次），
        而它一失败整轮 EWS 就取 0 封 —— 虽然水位没推进、下一轮会补（不丢邮件），
        但近一半轮次白跑，收信延迟翻倍。

        重试是**只读**操作，没有副作用。这与"认证只试一次"不冲突：
        那条针对的是密码错误会锁域账户；这里是连接被服务端 reset，重试才是对的。
        """
        import time as _t
        last = None
        for i in range(max(1, tries)):
            try:
                return acct.msg_folder_root
            except Exception as exc:                   # noqa: BLE001
                last = exc
                if i + 1 < tries:
                    _t.sleep(1.5 * (i + 1))            # 间隔递增，别把 reset 打成风暴
        logger.warning("[EWS %s] 取邮箱根失败（已重试 %d 次）: %s",
                       self.account, tries, last)
        return None

    def _folders(self, acct) -> List:
        """返回要取的邮件文件夹。跳过系统容器、非邮件容器与配置里的跳过项。"""
        out = []
        root = self._msg_root_with_retry(acct)
        if root is None:
            return out
        for f in root.walk():
            name = (getattr(f, "name", "") or "").strip()
            low = name.lower()
            if not name:
                continue
            # {GUID} 形态的隐藏系统文件夹
            if name.startswith("{") and name.endswith("}"):
                continue
            if low in _SYSTEM_FOLDER_NAMES:
                continue
            cc = getattr(f, "CONTAINER_CLASS", None)
            if cc not in _MAIL_CONTAINER_CLASSES:
                continue                # 日历/联系人/任务：本轮不在范围内
            if low in self.skip_folders:
                continue
            try:
                if not f.total_count:
                    continue
            except Exception:                          # noqa: BLE001
                continue
            out.append(f)
        return out

    def _folder_label(self, acct, f) -> str:
        """服务端文件夹名 → 库里的 folder 值。收件箱统一成 INBOX_NAME，
        与 Outlook/IMAP 两条路径对齐，避免同一个概念出现两种写法。"""
        try:
            if acct.inbox is not None and f.id == acct.inbox.id:
                return INBOX_NAME
        except Exception:                              # noqa: BLE001
            pass
        return (getattr(f, "name", "") or "").strip() or INBOX_NAME

    # ------------------------------------------------------------------ #
    # 取件
    # ------------------------------------------------------------------ #
    def fetch(self, password: str, since_days: Optional[int] = None,
              max_per_folder: Optional[int] = None,
              on_progress=None) -> List[Dict]:
        """增量取件。不改服务端任何状态；水位只暂存，需调用方 commit_state()。"""
        cap = int(max_per_folder or self.max_per_folder)
        acct = self._connect(password)
        self._acct = acct
        records: List[Dict] = []

        for f in self._folders(acct):
            label = self._folder_label(acct, f)
            wm = self.folder_watermark(label)
            if wm is None:
                days = since_days if since_days is not None else self.initial_days
                if days:
                    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(days))
                else:
                    start = None
            else:
                # 重叠窗口：宁可重复取，不可漏
                start = _dt.datetime.fromtimestamp(
                    max(0.0, wm - OVERLAP_SECONDS), _dt.timezone.utc)

            got = 0
            newest = wm or 0.0
            try:
                # ── 第一步：只查轻量字段，拿到待取清单 ──────────────────
                # 刻意不在这里带 mime_content：整封原文很重，上千封一次拉
                # 会撞上 IIS 的响应上限，服务端直接 reset（
                # 症状是只有最大的文件夹失败、小文件夹都正常）。
                qs = f.all() if start is None else f.filter(datetime_received__gte=start)
                heads = list(qs.only("id", "datetime_received")
                               .order_by("datetime_received")[:cap])
            except Exception as exc:                   # noqa: BLE001
                # 偶发 Connection aborted 会让整个文件夹这轮归零，重试一次。
                # 只重试一次：这是取件而非认证，不涉及账户锁定，但也不该无限重试。
                logger.info("[EWS %s] 文件夹 %s 列举失败（%s），重试一次",
                            self.account, label, type(exc).__name__)
                try:
                    qs = (f.all() if start is None
                          else f.filter(datetime_received__gte=start))
                    heads = list(qs.only("id", "datetime_received")
                                   .order_by("datetime_received")[:cap])
                except Exception as exc2:              # noqa: BLE001
                    logger.warning("[EWS %s] 文件夹 %s 列举失败: %s",
                                   self.account, label, exc2)
                    continue

            # ── 第二步：分小批取正文，每批独立容错 ──────────────────────
            # 注意传的是 **item 对象本身**，不是 id 字符串：EWS 的 ItemId 由
            # id + changekey 两部分组成，只给 id 会报 ErrorInvalidIdMalformed（踩过）。
            ids = [it for it in heads if getattr(it, "id", None)]
            bad = 0
            for i in range(0, len(ids), self.chunk_size):
                batch = ids[i:i + self.chunk_size]
                try:
                    items = list(acct.fetch(
                        ids=batch,
                        only_fields=["mime_content", "is_read", "datetime_received"]))
                except Exception as exc:               # noqa: BLE001
                    # 整批失败时退化成逐封取。同样的批大小，一个文件夹全部成功、
                    # 另一个却在某一批失败 —— 说明是某一封特别大（大附件）撑爆了
                    # 单次响应，不是普遍的体积问题。逐封取只会损失那一封。
                    # 刻意**不 break**：原来一 break 整个文件夹每轮只推进 45 封，
                    # 1683 封要跑三个多小时。
                    logger.info("[EWS %s] %s 第 %d 批整批失败（%s），改逐封取",
                                self.account, label, i // self.chunk_size + 1,
                                type(exc).__name__)
                    items = []
                    for one in batch:
                        try:
                            items.extend(acct.fetch(
                                ids=[one],
                                only_fields=["mime_content", "is_read",
                                             "datetime_received"]))
                        except Exception:             # noqa: BLE001
                            bad += 1
                batch_newest = newest
                for item in items:
                    if isinstance(item, Exception):
                        # 逐封打日志会把一轮同步刷成上千行，还把真正的错误埋掉；
                        # 只计数，循环结束后汇总一条。
                        bad += 1
                        continue
                    raw = getattr(item, "mime_content", None)
                    if not raw:
                        continue
                    if isinstance(raw, str):
                        raw = raw.encode("utf-8", "replace")
                    key = ""
                    try:
                        key = str(getattr(item, "id", "") or "")[:60]
                    except Exception:                  # noqa: BLE001
                        pass
                    # is_read 为 None 时按"已读"处理：宁可少标未读，
                    # 也不要凭空把一堆旧邮件标成未读去打扰用户
                    unread = (getattr(item, "is_read", True) is False)
                    rec = self._build_record(raw, key, label, unread)
                    if not rec:
                        continue
                    records.append(rec)
                    got += 1
                    dr = getattr(item, "datetime_received", None)
                    if dr is not None:
                        try:
                            batch_newest = max(batch_newest, dr.timestamp())
                        except Exception:              # noqa: BLE001
                            pass
                # 整批成功才推进水位候选值
                newest = batch_newest

            if got:
                # 暂存水位，等入库成功后由 commit_state() 落盘
                self._pending[label] = newest
            if bad:
                logger.warning("[EWS %s] %s: 有 %d 封取正文失败（已跳过，下轮会重试）",
                               self.account, label, bad)
            logger.info("[EWS %s] %s: 取 %d/%d 封（水位 %s）", self.account,
                        label, got, len(ids), int(newest) if newest else "-")
            if on_progress:
                # daemon 的回调是 4 参 (account, folder, total, subject)，
                # 少传会 TypeError。取最后一封的主题当进度文案。
                try:
                    _subj = records[-1]["subject"] if records else ""
                    on_progress(self.account, label, got, _subj)
                except Exception:                      # noqa: BLE001
                    pass
        return records

    # 与 IMAP 后端同名的两个入口，方便 daemon 用同一套调用
    def get_all_emails(self, password: str, days_ago: Optional[int] = None,
                       **kw) -> List[Dict]:
        return self.fetch(password, since_days=days_ago, **kw)

    def get_incremental_emails(self, password: str, days_ago: int = 30,
                               **kw) -> List[Dict]:
        return self.fetch(password, since_days=days_ago, **kw)


def from_config(cfg: dict, account: str) -> EwsExtractor:
    """按 config.yaml 的 ``ews`` 段构造。签名与 imap_extractor.from_config 一致。"""
    ews = (cfg or {}).get("ews") or {}
    accounts = ews.get("accounts") or {}
    acc = accounts.get(account) or {}
    host = acc.get("host") or ews.get("host") or ""
    if not host:
        raise ValueError("ews.accounts[%s].host 未配置" % account)
    return EwsExtractor(
        host=host,
        username=acc.get("username") or account,
        account=account,
        verify_tls=bool(acc.get("verify_tls", ews.get("verify_tls", True))),  # TLS-VERIFY-DEFAULT-20260930
        timeout=int(acc.get("timeout", ews.get("timeout", 60)) or 60),
        skip_folders=acc.get("skip_folders") or ews.get("skip_folders"),
        max_per_folder=int(acc.get("max_per_folder", 2000) or 2000),
        initial_days=acc.get("initial_days"),
        chunk_size=int(acc.get("chunk_size", ews.get("chunk_size", 15)) or 15),
    )
