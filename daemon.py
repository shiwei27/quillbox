"""daemon.py — 后台增量同步进程

模式：
  (默认)      循环同步，按 scheduler.interval_minutes 间隔
  --once      同步一次后退出
  --full      强制全量同步
  --ai-worker 不同步，只为没有 AI 摘要的邮件补跑 AI 分析
"""

import sys
import os
import time
import json
import yaml
import logging

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# 仅在真实控制台(tty)下重配置编码；后台/重定向场景不要动 std 流，
# 否则可能干扰 torch 加载模型时的 I/O（server 不做此操作且工作正常）。
try:
    if sys.stdout and sys.stdout.isatty() and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr and sys.stderr.isatty() and hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 必须在 import win32com / torch(sentence_transformers) 之前设置：
# 允许多 OpenMP 运行时共存，避免 torch 加载嵌入模型权重时死锁；并强制 HF 离线。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# 后台无控制台进程里，tqdm 进度条写入被重定向的句柄会导致 torch 加载权重时死锁；
# 全面禁用进度条。
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# 注意：不在模块顶层 import email_extractor（其会 import win32com/pythoncom）。
# 若在加载嵌入模型(torch)之前初始化 COM，会导致 torch 加载权重时死锁。
# 因此 EmailExtractor 改为在需要时（VectorStore 初始化之后）惰性导入。
#
# 同时：daemon 不再自行加载嵌入模型（在后台进程中 torch 加载会死锁），
# 改为把新邮件通过 HTTP 交给 server（server 已持有可用模型）做向量索引。
from core.db_manager import DBManager
from core.ai_engine import AIEngine
from core.email_sender import process_queue

from logging.handlers import RotatingFileHandler

_log_handlers = []
try:
    _fh = RotatingFileHandler(
        ROOT / "daemon.log",
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
        delay=True,   # 首次写入时才打开文件，避免与外部重定向抢占句柄导致 PermissionError
    )
    _log_handlers.append(_fh)
except Exception:
    pass
# 仅当存在真实 stdout（有控制台）时才加 StreamHandler；后台重定向到 DEVNULL 时无意义
try:
    if sys.stdout is not None:
        _log_handlers.append(logging.StreamHandler())
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers or [logging.NullHandler()],
)
log = logging.getLogger("daemon")

class _DedupFilter(logging.Filter):
    """折叠连续重复日志，避免同一错误刷屏（如 Outlook COM 掉线时的 RPC 报错）。"""
    def __init__(self):
        super().__init__(); self._last = None; self._n = 0
    def filter(self, record):
        msg = record.getMessage()
        if msg == self._last:
            self._n += 1
            if self._n % 50 == 0:
                record.msg = f"(同一日志已重复 {self._n} 次，后续同类抑制) {msg[:120]}"; record.args = ()
                return True
            return False
        self._last = msg; self._n = 0
        return True

log.addFilter(_DedupFilter())


def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


HEARTBEAT_FILE = ROOT / "data" / ".daemon_heartbeat"


def _heartbeat():
    """写入心跳时间戳，供 watchdog 判断 daemon 是否存活。"""
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


class RemoteIndex:
    """把向量索引委托给 server（server 已加载嵌入模型）。

    daemon 自身不加载 torch/嵌入模型——在后台无控制台进程中加载会死锁。
    add_email 通过 HTTP 调用 server 的 /api/vector/add（upsert 幂等）。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8765"):
        self.base_url = base_url.rstrip("/")

    def get_indexed_ids(self) -> set:
        # 不预取已索引 id；add 走 upsert，天然幂等。
        return set()

    def add_email(self, email: dict):
        import urllib.request
        try:
            data = json.dumps(email, ensure_ascii=False, default=str).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/api/vector/add", data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=30).read()
            return True
        except Exception as e:
            log.warning(f"远程向量索引失败 {email.get('id', '?')}: {e}")
            return False

    def add_emails_batch(self, emails: list) -> int:
        """批量索引：server 侧只有单封 upsert 入口，这里顺序委托。

        与 VectorStore.add_emails_batch 同名，避免 daemon 按 duck typing
        调用时抛 AttributeError；返回失败封数，供调用方如实汇报，
        不再把失败批次记成完成。
        """
        failed = 0
        for _e in emails:
            if not self.add_email(_e):
                failed += 1
        return failed


def parse_cfg(cfg: dict) -> dict:
    vdb = cfg.get("vector_db", {})
    ol  = cfg.get("ollama", {})
    em  = cfg.get("email", {})
    sch = cfg.get("scheduler", {})
    ai  = cfg.get("ai", {})
    llm = cfg.get("llm", {})
    api = llm.get("api", {})
    return {
        "db_path":               str(ROOT / "data" / "emails.db"),
        "chroma_path":           vdb.get("path", str(ROOT / "data" / "chromadb")),
        "embed_model":           vdb.get("embedding_model_path", "paraphrase-multilingual-MiniLM-L12-v2"),
        "ollama_url":            ol.get("base_url", "http://127.0.0.1:11434"),
        "ollama_model":          ol.get("model", "qwen2.5:7b"),
        # LLM 后端（与 server 保持一致，支持 ollama / api 双后端）
        "llm_provider":          llm.get("provider", "ollama"),
        "api_base_url":          api.get("base_url", ""),
        "api_key":               os.environ.get("QUILLBOX_API_KEY") or api.get("api_key", ""),
        "api_model":             api.get("model", ""),
        "api_timeout":           api.get("timeout", 120),
        "full_sync_days":        em.get("default_days", 1095),
        "incremental_days":      em.get("incremental_days", 7),
        # 同步间隔：优先读设置界面写入的 email.sync_interval，
        # 兼容旧的 scheduler.interval_minutes 配置，默认 15 分钟。
        "sync_interval_minutes": em.get("sync_interval", sch.get("interval_minutes", 15)),
        "skip_accounts":         ai.get("skip_accounts", []),
        "skip_folders":          ai.get("skip_folders", []),
        "skip_sender_patterns":  ai.get("skip_sender_patterns", []),
        "prompts":               cfg.get("prompts", {}),
        # IMAP 后端整段带进来：哪些账户由 IMAP 接管、各自的服务器与首轮回看窗口
        "imap":                  cfg.get("imap", {}) or {},
        # EWS 后端整段带进来（常见于 Exchange：IMAP 未启用、POP3 只能读收件箱）。
        # 注意 p 是逐键挑选构建的，漏这一行 ews_accounts 就恒为空，
        # 症状是"接线都对但日志里一行 EWS 都没有"——很安静，别再漏。
        "ews":                   cfg.get("ews", {}) or {},
        # 规则相关开关。apply_on_server 决定"移动"是否也落到邮件服务器上，
        # 默认关闭 —— 持续自动写入邮箱的风险是"一个写错的规则会一直错下去"。
        "rules":                 cfg.get("rules", {}) or {},
        # Outlook COM 后端总开关：MAPI 异常（共享资源耗尽等）时可临时关掉，
        # 避免整轮同步阻塞把 IMAP 那条健康链路一起拖住。
        "outlook_enabled":       bool(em.get("outlook_enabled", True)),
    }


def should_skip_ai(email: dict, p: dict) -> bool:
    """判断该邮件是否跳过 AI 分析（自动化告警、垃圾邮件等）"""
    account = email.get("account", "")
    folder  = email.get("folder", "")
    sender  = email.get("sender_email", "").lower()

    if account in p["skip_accounts"]:
        return True
    if any(f in folder for f in p["skip_folders"]):
        return True
    if any(pat in sender for pat in p["skip_sender_patterns"]):
        return True
    return False


# ── 向量索引自愈 ─────────────────────────────────────────────────────
SELFHEAL_LIMIT = 200          # 每轮最多补齐封数，避免拖长同步周期


def _chroma_indexed_ids(chroma_path: str) -> set:
    """读 chroma 的 sqlite 取已索引文档 id。只读，不影响 server 的连接。"""
    import sqlite3
    db = os.path.join(chroma_path, "chroma.sqlite3")
    if not os.path.isfile(db):
        return set()
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        cols = [r[1] for r in con.execute("pragma table_info(embeddings)")]
        for cand in ("embedding_id", "id", "document_id"):
            if cand in cols:
                return {str(r[0]) for r in
                        con.execute("select %s from embeddings" % cand)}
        return set()
    finally:
        con.close()


def _active_email_ids(db_path: str) -> list:
    """未删除邮件的 id，按 received_timestamp 倒序（新邮件优先补）。

    口径刻意与 get_emails_paginated() 的默认过滤一致（deleted=0，不过滤归档），
    保证与旧自愈逻辑补的是同一批邮件，只是不再把整行读出来。只读连接。
    """
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        return [str(r[0]) for r in con.execute(
            "SELECT id FROM emails WHERE deleted=0 "
            "ORDER BY received_timestamp DESC")]
    finally:
        con.close()


def _selfheal_vectors(p: dict, db: DBManager, vs) -> None:
    """补齐历史漏索引的邮件。

    存在意义：单轮失败此前没有任何重试路径，一次 server 抖动就是永久缺口。
    """
    try:
        indexed = _chroma_indexed_ids(p["chroma_path"])
        if not indexed:
            return                      # 未启用语义检索或读不到，静默跳过
        # 只取 id 求差集，只对本轮要补的那几封读整行。
        # 旧做法每轮 get_emails_paginated(page_size=100000) 把全库连 body/html_body
        # 一起读进内存（万封级邮箱约 2s、峰值数百 MB），而绝大多数轮次
        # 缺口为 0 —— 每 5 分钟白付一次。id-only 同口径查询约 0.05s。
        missing_ids = [i for i in _active_email_ids(p["db_path"])
                       if i not in indexed]
        if not missing_ids:
            return
        batch = []
        for _id in missing_ids[:SELFHEAL_LIMIT]:
            _e = db.get_email_by_id(_id)
            if _e is not None and not _e.get("deleted"):   # 求差集后被删/被清的跳过
                batch.append(_e)
        if not batch:
            return
        log.info("向量索引自愈：发现 %d 封漏索引，本轮补 %d 封",
                 len(missing_ids), len(batch))
        failed = vs.add_emails_batch(batch) or 0
        if failed:
            log.warning("向量索引自愈：成功 %d 封，失败 %d 封（下轮继续）",
                        len(batch) - failed, failed)
        else:
            log.info("向量索引自愈：%d 封已补齐", len(batch))
    except Exception as e:
        log.warning("向量索引自愈跳过（不影响本轮同步）: %s", e)


def _apply_rules(new_emails, ai, db, p=None):
    """对新到邮件按规则执行动作：分类/星标/已读/跳过AI/移动文件夹。

    "移动"默认**只改本地库**。若 `rules.apply_on_server` 为真，则在本轮结束后
    把同一批移动也落到邮件服务器上（EWS 账户）。

    为什么默认不动服务器：接上之后影响面从"手动跑一次"变成"每封新邮件都写一次邮箱"，
    风险在于**一个写错的规则会一直错下去，而且错在无人看着的时候**。
    """
    if not new_emails:
        return
    applied = 0
    # 攒批：一轮同步可能几十封，逐封建连接 + 逐封 walk 全部文件夹去定位太贵，
    # 对服务器也不友好。按 (账户, 目标文件夹) 收集，本轮结束统一提交。
    pending_server = {}
    for e in new_emails:
        # 「无需处理」规则独立于首个命中链：命中就打标签（之后不标待处理、不提取待办）
        try:
            _na = e.get("id") and ai.match_no_action(e)
            if _na:
                db.add_tag(e["id"], ai.NO_ACTION_TAG)
                if ((_na.get("actions") if isinstance(_na, dict) else None) or {}).get("skip_ai"):
                    db.add_tag(e["id"], "不分析")      # 停止提取选了「不分析」：前端不再为它调 AI
        except Exception as ex:
            log.error(f"无需处理规则应用失败 {e.get('id')}: {ex}")
    for e in new_emails:
        try:
            rule = ai._match_rule(e)
            if not rule:
                continue
            acts = rule.get("actions") or {}
            eid = e.get("id")
            if not eid:
                continue
            cat = (acts.get("category") or "").strip()
            if acts.get("star"):
                db.star_email(eid, True)
            if acts.get("mark_read"):
                db.mark_read(eid, True)
            if acts.get("skip_ai"):
                db.update_ai_fields(eid, cat or "📢内部通知",
                                    f"（已按规则「{rule.get('name','')}」跳过AI摘要）", [])
                e["category"] = cat or "📢内部通知"
            elif cat:
                db.bulk_update_category([eid], cat)
                e["category"] = cat   # 供后续 AI worker 保留该分类
            fld = (acts.get("folder") or "").strip()
            if fld:
                # 本地先改：用户立刻能看到的是这一层，
                # 服务端那层失败不该把本地也拖掉
                prev = e.get("folder") or ""
                db.move_folder(eid, fld)
                e["folder"] = fld
                if fld != prev:
                    pending_server.setdefault(
                        ((e.get("account") or ""), fld), []).append(
                        {"message_id": e.get("message_id") or "",
                         "folder": prev, "subject": e.get("subject") or ""})
            applied += 1
        except Exception as ex:
            log.error(f"规则动作应用失败 {e.get('id')}: {ex}")
    if applied:
        log.info(f"规则动作已应用于 {applied} 封新邮件")
    _apply_rules_on_server(pending_server, p)


def _apply_rules_on_server(pending, p):
    """把规则产生的移动也落到邮件服务器上。默认不做。"""
    if not pending:
        return
    rules_cfg = (p or {}).get("rules") or {}
    if not rules_cfg.get("apply_on_server"):
        n = sum(len(v) for v in pending.values())
        log.info("规则移动只改了本地库（%d 封）；"
                 "如需同时移动邮箱服务器上的邮件，打开 rules.apply_on_server", n)
        return

    from core import server_move as _sm
    imap_warned = set()
    for (account, target), rows in pending.items():
        rows = [r for r in rows if r.get("message_id")]
        if not account or not rows:
            continue
        # 单批上限由 server_move 强制（50），这里按它切片
        for i in range(0, len(rows), _sm.DEFAULT_MAX):
            chunk = rows[i:i + _sm.DEFAULT_MAX]
            try:
                res = _sm.move_batch(p, account, target, chunk, apply=True)
                log.info("[服务端移动] %s → %s：成功 %d，已在 %d，未找到 %d，失败 %d",
                         account, target, len(res["moved"]),
                         len(res["already_there"]), len(res["not_found"]),
                         len(res["failed"]))
                for bad in res["failed"][:5]:
                    log.warning("[服务端移动] 失败：%s", bad)
            except _sm.MoveError as ex:
                # IMAP 账户会走到这里（EXPUNGE 风险高、未实现）。
                # 每个账户只提醒一次，不逐封刷屏，也不中断本轮同步。
                key = (account, str(ex)[:40])
                if key not in imap_warned:
                    imap_warned.add(key)
                    log.warning("[服务端移动] %s 跳过：%s", account, ex)
                break
            except Exception as ex:                    # noqa: BLE001
                log.error("[服务端移动] %s → %s 异常：%s", account, target, ex)
                break


def _already_in_db(db, email: dict) -> str | None:
    """跨后端查重：命中返回已存在的行 id，否则 None。

    三条取件后端的主键体系互不相通（Outlook=EntryID / POP3=pop3_* / IMAP=imap_*），
    只靠 INSERT OR IGNORE 会让同一封信重复入库，必须按邮件头 Message-ID 判同一性。
    """
    mid = (email.get("message_id") or "").strip()
    if mid:
        hit = db.find_id_by_message_id(mid, account=email.get("account") or None)
        if hit:
            return hit
    # 兜底：历史行里约 5% 的 message_id 补不到（对应的 Outlook 项目已删除）
    subject = email.get("subject") or ""
    ts = email.get("received_timestamp") or 0.0
    if not subject or not ts:
        return None
    try:
        with db._conn() as conn:                      # noqa: SLF001
            row = conn.execute(
                "SELECT id FROM emails WHERE account=? AND subject=? "
                "AND ABS(received_timestamp-?)<=2 LIMIT 1",
                (email.get("account") or "", subject, ts)).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


def sync_once(p: dict, db: DBManager, vs, ai: AIEngine, force_full: bool = False):
    """同步邮件：提取 → 入库 → 向量索引。AI 摘要默认异步补跑，不阻塞同步流程。"""
    log.info("--- 开始同步 ---")
    db.set_sync_status("sync_running", "1")
    db.set_sync_status("sync_progress", "正在连接邮箱…")
    db.set_sync_status("last_sync_start", datetime.now(timezone.utc).isoformat())

    imap_cfg = p.get("imap") or {}
    imap_accounts = (list((imap_cfg.get("accounts") or {}).keys())
                     if imap_cfg.get("enabled") else [])
    ews_cfg = p.get("ews") or {}
    ews_accounts = (list((ews_cfg.get("accounts") or {}).keys())
                    if ews_cfg.get("enabled") else [])
    is_first_run = db.count() == 0 or force_full
    days_ago = p["full_sync_days"] if is_first_run else p["incremental_days"]
    if not is_first_run:
        # 自愈：若距上次成功同步已超过增量窗口（daemon 曾停机/掉线），自动扩大回看范围
        # 覆盖整个停机期，避免那段邮件在超过 incremental_days 后永远不再回补（历史"中间空洞"根因）。
        # 封顶 full_sync_days。
        _last_end = db.get_sync_status("last_sync_end")
        if _last_end:
            try:
                _gap_days = (datetime.now(timezone.utc) - datetime.fromisoformat(_last_end)).total_seconds() / 86400.0
                days_ago = min(p["full_sync_days"], max(days_ago, int(_gap_days) + 2))
            except Exception:
                pass

    def on_progress(account, folder, total, subject):
        msg = f"📥 提取中 [{account}/{folder}] 共 {total} 封 | {str(subject)[:40]}"
        db.set_sync_status("sync_progress", msg)
        _heartbeat()
        log.info(msg)

    emails = []
    imap_exts = []          # [(账户, extractor)]，入库成功后才推进 UID 水位
    imap_failed = []
    ews_exts = []           # 同上，EWS 用时间戳水位
    ews_failed = []

    # ── 后端一：IMAP（接管的账户）──────────────────────────────────────
    for _acct in imap_accounts:
        try:
            from core import win_credential
            from core.imap_extractor import from_config as _imap_from_config

            _pw = win_credential.get_password(_acct)
            if not _pw:
                log.warning("[IMAP %s] 凭据库里没有密码，跳过（先运行：python pop3_setup.py set <账户>）",
                            _acct)
                continue
            _acc_cfg = (imap_cfg.get("accounts") or {}).get(_acct) or {}
            _ext = _imap_from_config({"imap": imap_cfg}, _acct)
            _first = not _ext.has_state()
            # 首轮：initial_days 为空表示不限时间（全量导入服务端现存邮件）
            _since = _acc_cfg.get("initial_days") if _first else None
            _cap = int(_acc_cfg.get("max_per_folder", 2000 if _first else 600))
            db.set_sync_status("sync_progress",
                               f"IMAP {'首轮全量' if _first else '增量'}取件 {_acct}…")
            _got = _ext.fetch(_pw, since_days=_since, max_per_folder=_cap,
                              on_progress=on_progress)
            log.info("[IMAP %s] 取到 %d 封（%s）", _acct, len(_got),
                     "首轮" if _first else "增量")
            emails.extend(_got)
            imap_exts.append((_acct, _ext))
        except Exception as e:
            imap_failed.append(_acct)
            log.error(f"[IMAP {_acct}] 取件失败: {e}")

    # ── 后端二：EWS（Exchange 账户）────────────────────────────────────
    # 很多 Exchange 没开 IMAP，POP3 又只能读收件箱
    # （收件箱之外的邮件走 POP3 会直接丢掉），所以走 EWS。
    for _acct in ews_accounts:
        try:
            from core import win_credential
            from core.ews_extractor import from_config as _ews_from_config

            _pw = win_credential.get_password(_acct)
            if not _pw:
                log.warning("[EWS %s] 凭据库里没有密码，跳过", _acct)
                continue
            _acc_cfg = (ews_cfg.get("accounts") or {}).get(_acct) or {}
            _ext = _ews_from_config({"ews": ews_cfg}, _acct)
            _first = not _ext.has_state()
            _since = _acc_cfg.get("initial_days") if _first else None
            _cap = int(_acc_cfg.get("max_per_folder", 2000 if _first else 600))
            db.set_sync_status("sync_progress",
                               f"EWS {'首轮全量' if _first else '增量'}取件 {_acct}…")
            _got = _ext.fetch(_pw, since_days=_since, max_per_folder=_cap,
                              on_progress=on_progress)
            log.info("[EWS %s] 取到 %d 封（%s）", _acct, len(_got),
                     "首轮" if _first else "增量")
            emails.extend(_got)
            ews_exts.append((_acct, _ext))
        except Exception as e:
            ews_failed.append(_acct)
            log.error(f"[EWS {_acct}] 取件失败: {e}")
        finally:
            try:
                del _pw
            except Exception:
                pass

    # ── 后端三：Outlook COM（其余账户）────────────────────────────────
    if not p.get("outlook_enabled", True):
        log.warning("Outlook 后端已按配置关闭（email.outlook_enabled=false），本轮只用 IMAP 结果")
        db.set_sync_status("sync_progress", "Outlook 后端已关闭，仅 IMAP")
    else:
      try:
          # Outlook COM 只在启用时才导入（需要 Windows + pywin32）；关闭时不触碰 win32com，
          # 非 Windows 平台因此可以只跑 IMAP / EWS / POP3。
          from core.email_extractor import EmailExtractor  # 惰性导入：在嵌入模型加载之后才初始化 COM
          # 两条原生后端接管的账户都要让 Outlook 跳过，否则同一个邮箱会被取两遍。
          extractor = EmailExtractor(skip_accounts=imap_accounts + ews_accounts)
          db.set_sync_status("sync_progress", f"开始{'全量' if is_first_run else '增量'}提取（{days_ago}天）…")
          emails.extend(
              extractor.get_all_emails(days_ago=days_ago, on_progress=on_progress)
              if is_first_run
              else extractor.get_incremental_emails(days_ago=days_ago, on_progress=on_progress)
          )
      except Exception as e:
          # 一条后端挂掉不该让整轮同步失败：IMAP 有产出就继续走入库
          log.error(f"Outlook 邮件提取失败: {e}")
          if not emails:
              db.set_sync_status("sync_running", "0")
              db.set_sync_status("sync_progress", f"❌ 提取失败: {e}")
              return
          db.set_sync_status("sync_progress", f"⚠️ Outlook 提取失败（{e}），仅使用 IMAP 结果")

    # ── 阶段1：快速入库（INSERT OR IGNORE 自动去重，无需加载全量 ID）──
    log.info(f"总计提取 {len(emails)} 封，开始入库去重…")
    db.set_sync_status("sync_progress", f"提取完成，共 {len(emails)} 封，开始写入…")
    new_emails = []
    dup_cross = 0
    for i, email in enumerate(emails):
        try:
            if _already_in_db(db, email):
                dup_cross += 1
                continue
            is_new = db.insert_email_if_new(email)
            if is_new:
                db.upsert_people(email)
                new_emails.append(email)
        except Exception as e:
            log.warning(f"入库邮件 {email.get('id','?')} 出错: {e}")

        if (i + 1) % 50 == 0:
            pct = int((i + 1) / len(emails) * 100)
            msg = f"💾 入库 {i+1}/{len(emails)} ({pct}%) 新增 {len(new_emails)}"
            db.set_sync_status("sync_progress", msg)
            log.info(msg)

    log.info(f"入库完成，新增 {len(new_emails)} 封，跨后端去重跳过 {dup_cross} 封")

    # 入库完成后才推进 IMAP 的 UID 水位：先推进再入库的话，中途失败这批邮件会被永久跳过
    for _acct, _ext in list(imap_exts) + list(ews_exts):
        try:
            _n = _ext.commit_state()
            if _n:
                log.info("[%s %s] 已推进 %d 个文件夹的水位",
                         getattr(_ext, "ID_PREFIX", "IMAP").upper(), _acct, _n)
        except Exception as e:
            log.warning(f"[IMAP {_acct}] 水位推进失败（下轮会重取）: {e}")

    if not new_emails:
        # 即便本轮没有新邮件，也要给历史漏索引一个补齐的机会
        _selfheal_vectors(p, db, vs)
        _finish(db, 0)
        return

    # ── 阶段2：批量向量索引 ──
    db.set_sync_status("sync_progress", f"建立向量索引…（{len(new_emails)} 封）")
    indexed_vs_ids = vs.get_indexed_ids()
    to_index = [e for e in new_emails if e["id"] not in indexed_vs_ids]
    if to_index:
        BATCH_SIZE = 50
        failed_total = 0
        for i in range(0, len(to_index), BATCH_SIZE):
            batch = to_index[i:i + BATCH_SIZE]
            try:
                failed_total += vs.add_emails_batch(batch) or 0
            except Exception as e:
                failed_total += len(batch)
                log.warning(f"向量索引批次 {i//BATCH_SIZE} 出错: {e}")
            if (i + BATCH_SIZE) % 100 == 0 or i == 0:
                log.info(f"🔍 向量索引 {min(i+BATCH_SIZE, len(to_index))}/{len(to_index)}")
        if failed_total:
            log.warning(
                f"向量索引未全部完成：成功 {len(to_index) - failed_total} 封，"
                f"失败 {failed_total} 封"
            )
        else:
            log.info(f"向量索引完成，共 {len(to_index)} 封")

    _selfheal_vectors(p, db, vs)
    _finish(db, len(new_emails))

    # ── 应用邮件规则动作（分类/星标/已读/跳过AI）──
    try:
        _apply_rules(new_emails, ai, db, p)
    except Exception as e:
        log.error(f"规则动作应用异常: {e}")

    # ── 阶段3：异步启动 AI 分析（可选）──
    ai_ok = ai.is_running()
    if ai_ok:
        try:
            _pre_ids = _pre_insight_candidates(new_emails, db, p)
        except Exception as _ex:
            _pre_ids = []
            log.warning("预先分析候选计算失败（不影响同步）: %s", _ex)
        if _pre_ids:
            import threading as _th
            log.info("预先分析：%d 封新邮件排队", len(_pre_ids))
            _th.Thread(target=_pre_insight_worker, args=(_pre_ids,), daemon=True, name="pre-insight").start()
        log.info("同步完成，启动异步 AI 分析…")
        import threading
        threading.Thread(
            target=run_ai_worker, args=(p, db, vs, ai),
            daemon=True, name="post-sync-ai-worker"
        ).start()
    else:
        log.info("Ollama 未运行，AI 摘要将在下次 --ai-worker 时补跑")


# ── 预先结构化分析─────────────────────────────────────
# 打开邮件时现算 insight 要 3–12s。同步完成后对「可能要我处理」的新邮件先算好：
#   收件箱 + 未读 + 未标「无需处理」+ 不在 skip_accounts/skip_folders/skip_sender_patterns；
#   每轮最多 ai.pre_insight_max 封（默认 20），新到优先；ai.pre_insight: false 可关闭。
# 调 server 的 insight 端点而不是自己调 AI：缓存、并发锁、失败不落库都只有一份实现。
_PRE_INSIGHT_FOLDERS = ("收件箱", "inbox", "INBOX")


def _pre_insight_candidates(new_emails, db: DBManager, p: dict) -> list:
    cfg_ai = (load_config().get("ai") or {})
    if cfg_ai.get("pre_insight") is False:
        return []
    _cap = cfg_ai.get("pre_insight_max", 20)
    try:
        cap = max(0, int(20 if _cap is None else _cap))   # 0 是合法值（=不做），不能用 `or` 回落默认
    except (TypeError, ValueError):
        cap = 20
    if cap == 0:
        return []
    out = []
    for e in sorted(new_emails or [], key=lambda x: -(x.get("received_timestamp") or 0)):
        eid = e.get("id")
        if not eid or not e.get("unread"):
            continue
        if (e.get("folder") or "") not in _PRE_INSIGHT_FOLDERS:
            continue
        if should_skip_ai(e, p):
            continue
        try:
            if db.has_tag(eid, "无需处理") or db.get_insight(eid):
                continue
        except Exception:
            continue
        out.append(eid)
        if len(out) >= cap:
            break
    return out


def _pre_insight_worker(ids, base_url: str = "http://127.0.0.1:8765") -> None:
    import urllib.request
    ok = fail = 0
    t0 = time.time()
    for eid in ids:
        try:
            req = urllib.request.Request("%s/api/emails/%s/insight" % (base_url, urllib.request.quote(eid, safe="")),
                                         method="POST", data=b"", headers={"Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=150).read().decode("utf-8"))
            ok += 1 if r.get("ok") else 0
            fail += 0 if r.get("ok") else 1
        except Exception as ex:
            fail += 1
            log.warning("预先分析失败 %s: %s", eid[:24], ex)
    log.info("预先分析完成：%d 封成功，%d 封失败，耗时 %.0fs", ok, fail, time.time() - t0)
    if (load_config().get("ai") or {}).get("pre_compare") is not False:
        _pre_compare(ids, base_url)


# 系列邮件顺带把「与上期对比」算好（阅读区「变化」卡打开即显示）。ai.pre_compare: false 可关闭。
# 走 server 接口：缓存 / 可追溯校验只有一份实现；不属于系列或第一期的，接口直接返回、不调 AI。
_PRE_COMPARE_SKIP = ("这封邮件不属于任何系列", "这是系列的第一期，没有上期可比")


def _pre_compare(ids, base_url: str = "http://127.0.0.1:8765") -> None:
    import urllib.request
    n = fail = 0
    t0 = time.time()
    for eid in ids:
        try:
            req = urllib.request.Request("%s/api/series/compare" % base_url, method="POST",
                                         data=json.dumps({"email_id": eid}).encode("utf-8"), headers={"Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=180).read().decode("utf-8"))
            if r.get("ok"):
                n += 1
            elif r.get("error") not in _PRE_COMPARE_SKIP:
                fail += 1
        except Exception as ex:
            fail += 1
            log.warning("预先对比失败 %s: %s", eid[:24], ex)
    if n or fail:
        log.info("预先对比（系列与上期）：%d 封完成，%d 封失败，耗时 %.0fs", n, fail, time.time() - t0)


def run_ai_worker(p: dict, db: DBManager, vs, ai: AIEngine):
    """为没有 AI 摘要的真实邮件（排除自动化账户/文件夹）补跑 AI 分析"""
    if not ai.is_running():
        log.error("Ollama 未运行，无法启动 AI worker")
        return

    batch = db.get_emails_without_ai(limit=200, skip_accounts=p["skip_accounts"],
                                      skip_folders=p["skip_folders"])
    if not batch:
        log.info("AI worker: 所有真实邮件均已有 AI 摘要，无需处理")
        return

    log.info(f"AI worker: 开始处理 {len(batch)} 封无摘要邮件")
    failed = 0
    for i, email in enumerate(batch):
        try:
            result = ai.analyze_email(email.get("subject", ""), email.get("body", ""))

            # 失败绝不落库：analyze_email 改前会回一个假的
            # {"summary": "AI 分析失败", "category": "📢内部通知"}，
            # 落库后摘要端点命中缓存，那封邮件就永久停在这句话上。
            # 现在跳过落库，summary 保持为空，下一轮 get_emails_without_ai 会再捞它。
            if result.get("ok") is False:
                failed += 1
                continue

            db.update_ai_fields(
                email["id"],
                email.get("category") or result.get("category", "📢内部通知"),
                result.get("summary", ""),
                result.get("action_items", []),
            )
            # 同时更新向量索引中的摘要
            email["summary"]  = result.get("summary", "")
            email["category"] = result.get("category", "")
            vs.add_email(email)

            if (i + 1) % 10 == 0:
                log.info(f"AI worker: {i+1}/{len(batch)} 完成")
        except Exception as e:
            failed += 1
            log.warning(f"AI worker 处理 {email.get('id','?')} 出错: {e}")
    if failed:
        log.warning("AI worker: %d/%d 封因 AI 故障未落库，下一轮会重试",
                    failed, len(batch))

    log.info(f"AI worker: 完成，处理了 {len(batch)} 封")


def _finish(db: DBManager, new_count: int):
    now = datetime.now(timezone.utc).isoformat()
    db.set_sync_status("last_sync_end", now)
    db.set_sync_status("last_sync_count", str(new_count))
    db.set_sync_status("total_count", str(db.count()))
    db.set_sync_status("sync_running", "0")
    db.set_sync_status("sync_progress", f"✅ 同步完成，新增 {new_count} 封，共 {db.count()} 封")
    log.info(f"--- 同步完成，新增 {new_count} 封，DB 共 {db.count()} 封 ---")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",      action="store_true", help="同步一次后退出")
    parser.add_argument("--full",      action="store_true", help="强制全量同步")
    parser.add_argument("--ai-worker", action="store_true", help="不同步，只补跑缺失的 AI 分析")
    args = parser.parse_args()

    cfg = load_config()
    p   = parse_cfg(cfg)

    # 尽早写心跳：加载嵌入模型可能耗时较久，避免 watchdog 在启动期间误判 daemon 已死而重复拉起
    if not args.once and not args.ai_worker:
        _heartbeat()

    db = DBManager(p["db_path"])
    vs = RemoteIndex()   # 向量索引委托给 server，daemon 不加载嵌入模型
    ai = AIEngine(
        base_url     = p["ollama_url"],
        model        = p["ollama_model"],
        provider     = p["llm_provider"],
        api_base_url = p["api_base_url"],
        api_key      = p["api_key"],
        api_model    = p["api_model"],
        api_timeout  = p["api_timeout"],
        prompts      = p.get("prompts", {}),
    )

    if args.ai_worker:
        log.info("Quillbox AI worker 启动")
        run_ai_worker(p, db, vs, ai)
        return

    if not ai.is_running():
        log.warning("Ollama 未运行，AI 分析将跳过")

    if args.once:
        log.info("Quillbox daemon --once 模式")
        sync_once(p, db, vs, ai, force_full=args.full)
        return

    log.info(f"Quillbox daemon 启动 | 同步间隔 {p['sync_interval_minutes']} 分钟")
    SIGNAL_FILE = ROOT / "data" / ".sync_trigger"
    _heartbeat()
    first = True
    while True:
        try:
            try:
                ai.prompts = load_config().get("prompts", {})  # 热加载最新规则(含导入的Outlook规则)
            except Exception:
                pass
            sync_once(p, db, vs, ai, force_full=(args.full and first))
        except Exception as e:
            log.error(f"同步异常: {e}")
        first = False

        # 等待下次同步，期间每 5 秒检查信号文件
        wait_seconds = p["sync_interval_minutes"] * 60
        log.info(f"等待 {p['sync_interval_minutes']} 分钟后下次同步...")
        elapsed = 0
        while elapsed < wait_seconds:
            time.sleep(5)
            elapsed += 5
            _heartbeat()
            # 消费邮件动作队列（发送/转发/删除）。daemon 运行在可访问
            # Outlook COM 的交互式 session 中，是执行这些操作的正确位置。
            try:
                n = process_queue()
                if n:
                    log.info(f"已处理 {n} 个邮件动作队列项（发送/转发/删除）")
            except Exception as e:
                log.error(f"处理动作队列异常: {e}")
            # 处理"导入 Outlook 规则"触发（daemon 拥有 Outlook COM）
            _imp_req = ROOT / "data" / ".import_outlook_req"
            if _imp_req.exists():
                try:
                    from core.outlook_rules import read_outlook_rules
                    rr = read_outlook_rules()
                    if rr.get("ok"):
                        cfg2 = load_config(); pr = cfg2.setdefault("prompts", {})
                        existing = pr.get("rules", []) or []
                        names = {x.get("name") for x in existing}
                        added = 0
                        for rule in rr.get("rules", []):
                            base = rule.get("name") or "Outlook规则"; nm = base; k = 1
                            while nm in names:
                                nm = f"{base} ({k})"; k += 1
                            rule["name"] = nm; names.add(nm); existing.append(rule); added += 1
                        pr["rules"] = existing
                        from core.config_store import save_config as _save_config   # 保留注释与格式
                        _save_config(ROOT / "config.yaml", cfg2)
                        ai.prompts = pr
                        _result = {"ok": True, "imported": added, "total": len(existing)}
                    else:
                        _result = rr
                except Exception as _e:
                    _result = {"ok": False, "error": str(_e)[:120]}
                try:
                    (ROOT / "data" / ".import_outlook_res").write_text(json.dumps(_result, ensure_ascii=False), encoding="utf-8")
                    _imp_req.unlink()
                    log.info(f"Outlook 规则导入处理完成: {_result}")
                except Exception:
                    pass

            # 检测 server 发来的即时同步信号
            if SIGNAL_FILE.exists():
                try:
                    content = SIGNAL_FILE.read_text(encoding="utf-8").strip()
                    SIGNAL_FILE.unlink()
                    force = (content == "full")
                    log.info(f"收到同步信号（{'全量' if force else '增量'}），立即执行")
                    sync_once(p, db, vs, ai, force_full=force)
                except Exception as e:
                    log.error(f"信号触发同步异常: {e}")
                break  # 信号处理后重置等待计时


if __name__ == "__main__":
    main()
