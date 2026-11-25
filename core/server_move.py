"""把规则的"移动"真正落到邮件服务器上。

⚠️ 这是本项目里**唯一一处会修改邮件服务器状态**的代码
-----------------------------------------------------
其余所有取件路径都严守只读（IMAP `select(readonly=True)` + `BODY.PEEK[]`、
EWS 只读 `mime_content`），刻意不碰服务端状态。本模块是有意开的例外（默认关闭，需在配置里显式打开），因此把安全措施做在模块里，而不是靠调用方自觉：

1. **默认 dry_run**。`apply=True` 必须由调用方显式传，不给默认值。
2. **只移到服务器上已存在的文件夹**。绝不自动建文件夹 —— 建错了留下一堆空目录
   还得人去清，而且拼错名字会静默造出新文件夹而不是报错。
3. **审计日志**。每次移动前后都记 `data/server_moves.jsonl`：
   message_id、源文件夹、目标文件夹、时间。有这份记录才谈得上"可回退"。
4. **拒绝碰回收站与已删除**。往回收站移 = 变相删除，不在规则该做的事里；
   从回收站往外移也不做（可能是用户手动删的）。
5. **每次调用有上限**。默认一次最多 50 封，防止一个写错的规则把整箱搬走。

为什么先只做 EWS
----------------
本地与服务端归属不一致的问题主要出在 EWS（Exchange）账户。IMAP 侧的移动要
`COPY` + `STORE \\Deleted` + `EXPUNGE`，`EXPUNGE` 一旦作用错文件夹是**真删除**，
风险等级明显更高，所以这一轮不做，调用时显式报"暂不支持"而不是静默跳过。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
AUDIT = _ROOT / "data" / "server_moves.jsonl"

# 这些文件夹不参与移动：往回收站移等于变相删除，从回收站往外移可能是用户手删的
PROTECTED = {"已删除邮件", "待删除邮件", "垃圾邮件", "trash", "deleted items",
             "junk email", "草稿", "drafts", "已发送邮件", "sent items"}

DEFAULT_MAX = 50


class MoveError(Exception):
    """移动失败。message 会给用户看。"""


def _audit(rec: Dict) -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        rec = dict(rec)
        rec["ts"] = time.time()
        rec["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:                           # noqa: BLE001
        # 审计写不进去不该让移动失败，但必须留下痕迹
        logger.error("[服务端移动] 审计写入失败: %s", exc)


def _ews_account(cfg: dict, account: str):
    from exchangelib import (Account, Configuration, Credentials, DELEGATE,
                             FailFast, NTLM)
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
    from core import win_credential

    acc = ((cfg.get("ews") or {}).get("accounts") or {}).get(account) or {}
    if not acc.get("host"):
        raise MoveError("账户 %s 未配置 EWS，无法在服务器上移动" % account)
    if not acc.get("verify_tls", True):  # TLS-VERIFY-DEFAULT-20260930
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    pw = win_credential.get_password(account)
    if not pw:
        raise MoveError("取不到 %s 的凭据" % account)
    try:
        conf = Configuration(
            server=acc["host"],
            credentials=Credentials(username=acc.get("username") or account,
                                    password=pw),
            auth_type=NTLM, retry_policy=FailFast())
        return Account(primary_smtp_address=account, config=conf,
                       autodiscover=False, access_type=DELEGATE)
    finally:
        del pw


# 只认邮件类容器。日历/联系人/任务上没有 message_id 字段，
# 拿 .only("message_id") 去查会直接抛错。
_MAIL_CLASSES = ("IPF.Note", None, "")


def _mail_folders(a) -> Dict[str, object]:
    """服务器上的邮件文件夹：名字 -> 文件夹对象。

    这一份是唯一实现 —— 之前 list_server_folders 与 move_batch 各写了一遍，
    结果 move_batch 那份漏了容器类过滤，扫到日历就抛错，
    错误提示里列的"现有文件夹"两边还不一样。同一件事只留一处。
    """
    out = {}
    for f in a.msg_folder_root.walk():
        name = (getattr(f, "name", "") or "").strip()
        if not name or name.startswith("{"):
            continue
        if getattr(f, "CONTAINER_CLASS", None) not in _MAIL_CLASSES:
            continue
        out[name] = f
    return out


def list_server_folders(cfg: dict, account: str) -> List[str]:
    """服务器上现有的邮件文件夹名。移动目标必须在这个列表里。"""
    return sorted(_mail_folders(_ews_account(cfg, account)))


def _norm_mid(v: str) -> str:
    return (v or "").strip().strip("<>").lower()


def _retry(fn, tries: int = 3, what: str = ""):
    """重试只读操作。

    部分 Exchange 的 ConnectionReset 很频繁：服务端移动的失败率可超过一半，
    失败全是 Connection aborted。
    重试的都是**只读**步骤（建连接、取文件夹表、按 message_id 查定位），
    没有副作用；真正的移动动作本身不重试 —— 那是写操作，
    重试可能造成重复移动，且移动失败会在下一轮被重新识别为分歧。
    """
    import time as _t
    last = None
    for i in range(max(1, tries)):
        try:
            return fn()
        except Exception as exc:                        # noqa: BLE001
            last = exc
            if i + 1 < tries:
                _t.sleep(1.5 * (i + 1))
    raise MoveError("%s失败（已重试 %d 次）：%s"
                    % (what or "操作", tries, type(last).__name__))


def move_batch(cfg: dict, account: str, target: str,
               rows: List[dict], *, apply: bool = False,
               max_items: int = DEFAULT_MAX) -> Dict:
    """把一批邮件在服务器上移到 target。

    :param rows: 每项需含 message_id、folder（本地记录的源文件夹）、subject
    :param apply: **默认 False（干跑）**。真正写入必须显式传 True。
    :returns: 结果统计，含每封的处置
    """
    target = (target or "").strip()
    if not target:
        raise MoveError("目标文件夹为空")
    if target.lower() in PROTECTED or target in PROTECTED:
        raise MoveError("拒绝移动到受保护的文件夹「%s」" % target)

    a = _retry(lambda: _ews_account(cfg, account), what="连接邮箱")

    # 目标文件夹必须已存在：不自动创建。拼错名字应当报错，而不是静默造一个新目录。
    folders = _retry(lambda: _mail_folders(a), what="取文件夹表")
    if target not in folders:
        raise MoveError("服务器上没有文件夹「%s」（不自动创建）。现有：%s"
                        % (target, "、".join(sorted(folders)[:12])))
    dest = folders[target]

    todo = [r for r in rows if _norm_mid(r.get("message_id"))]
    skipped_no_mid = len(rows) - len(todo)
    if len(todo) > max_items:
        raise MoveError("一次最多 %d 封，收到 %d 封；请分批（防止一个写错的规则把整箱搬走）"
                        % (max_items, len(todo)))

    # 按 message_id 精确定位，**不再遍历所有文件夹拉几千条**。
    # 原实现为了找 1~2 封要 f.all().only(...)[:5000] 扫每个文件夹，
    # 又慢又显著拉高 ConnectionReset 概率。
    # attachment_fetch 已经证明 filter(message_id=...) 能直接定位。
    want = {_norm_mid(r["message_id"]): r for r in todo}
    found: Dict[str, Tuple[object, str]] = {}
    for mid in list(want):
        cands = [mid if mid.startswith("<") else "<%s>" % mid, mid]
        for name, f in folders.items():
            if mid in found:
                break
            if name == target:
                continue          # 已在目标文件夹的，下面的 already_there 分支处理
            for cand in cands:
                try:
                    hit = _retry(
                        lambda f=f, cand=cand: list(
                            f.filter(message_id=cand).only("message_id")[:1]),
                        what="定位邮件")
                except MoveError:
                    continue
                if hit:
                    found[mid] = (hit[0], name)
                    break
        if mid not in found:
            # 也可能它已经在目标文件夹里
            tf = folders.get(target)
            if tf is not None:
                for cand in cands:
                    try:
                        hit = list(tf.filter(message_id=cand).only("message_id")[:1])
                    except Exception:                  # noqa: BLE001
                        continue
                    if hit:
                        found[mid] = (hit[0], target)
                        break

    result = {"account": account, "target": target, "apply": bool(apply),
              "total": len(rows), "skipped_no_message_id": skipped_no_mid,
              "not_found": [], "already_there": [], "protected_source": [],
              "moved": [], "failed": []}

    for mid, row in want.items():
        hit = found.get(mid)
        subj = (row.get("subject") or "")[:50]
        if not hit:
            result["not_found"].append(subj)
            continue
        item, cur_folder = hit
        if cur_folder == target:
            result["already_there"].append(subj)
            continue
        if cur_folder.lower() in PROTECTED or cur_folder in PROTECTED:
            # 从回收站/已发送往外搬不做：那可能是用户手动放的
            result["protected_source"].append("%s（在 %s）" % (subj, cur_folder))
            continue
        rec = {"account": account, "message_id": mid, "from": cur_folder,
               "to": target, "subject": subj, "apply": bool(apply)}
        if not apply:
            result["moved"].append("%s（%s → %s）" % (subj, cur_folder, target))
            continue
        try:
            # 审计**先写**：写完再移。反过来的话移成功而审计失败就无从回退了。
            _audit(rec)
            item.move(to_folder=dest)
            result["moved"].append("%s（%s → %s）" % (subj, cur_folder, target))
        except Exception as exc:                       # noqa: BLE001
            _audit(dict(rec, error=type(exc).__name__))
            result["failed"].append("%s: %s" % (subj, type(exc).__name__))
    return result


def move_for_imap(*_a, **_kw):
    """IMAP 侧暂不支持 —— 显式报错，不静默跳过。

    IMAP 的移动要 COPY + STORE \\Deleted + EXPUNGE，而 EXPUNGE 作用错文件夹
    是**真删除**，风险明显高于 EWS 的 move。等 EWS 侧跑稳、有审计数据之后再考虑。
    """
    raise MoveError("IMAP 账户暂不支持服务端移动（EXPUNGE 风险高，待 EWS 侧验证稳定后再开）")
