"""email_sender.py — 通过 Outlook COM 发送/转发/删除邮件，兼容跨 session 队列模式"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional


# ── 队列文件路径（FastAPI server 写入，interactive session 的 daemon 消费）─────
QUEUE_DIR = Path(__file__).parent.parent / "data" / "action_queue"


def _enqueue(action: dict) -> str:
    """把操作写入队列文件，返回 action_id"""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    action_id = str(uuid.uuid4())[:8]
    action["id"] = action_id
    action["status"] = "pending"
    action["created_at"] = time.time()
    path = QUEUE_DIR / f"{action_id}.json"
    path.write_text(json.dumps(action, ensure_ascii=False), encoding="utf-8")
    return action_id


def _dequeue_all() -> list[Path]:
    """返回所有 pending 队列文件路径"""
    if not QUEUE_DIR.exists():
        return []
    return sorted(QUEUE_DIR.glob("*.json"))


def _mark_done(path: Path, result: str = "ok", error: str = ""):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "error" if error else "done"
    data["result"] = result
    data["error"] = error
    data["finished_at"] = time.time()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def get_action_status(action_id: str) -> dict:
    path = QUEUE_DIR / f"{action_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"status": "not_found"}


# ── 队列写入（FastAPI 调用，不依赖 Outlook session）────────────────────────────

def queue_send(to: str, subject: str, body: str,
               cc: str = "", reply_to_entry_id: str = "",
               is_html: bool = False, attachments: Optional[list] = None) -> str:
    return _enqueue({"type": "send", "to": to, "subject": subject,
                     "body": body, "cc": cc, "reply_to_entry_id": reply_to_entry_id,
                     "is_html": bool(is_html), "attachments": attachments or []})


def queue_forward(entry_id: str, to: str, note: str = "",
                  is_html: bool = False, attachments: Optional[list] = None) -> str:
    return _enqueue({"type": "forward", "entry_id": entry_id, "to": to, "note": note,
                     "is_html": bool(is_html), "attachments": attachments or []})


def queue_delete(entry_id: str, permanent: bool = False) -> str:
    return _enqueue({"type": "delete", "entry_id": entry_id, "permanent": permanent})


# ── 实际 COM 操作（由 interactive session 的 daemon 调用）────────────────────────

def _get_outlook():
    import win32com.client
    ol = win32com.client.Dispatch("Outlook.Application")
    return ol, ol.GetNamespace("MAPI")


def _find_item(ns, entry_id: str):
    try:
        return ns.GetItemFromID(entry_id)
    except Exception:
        return None


def _add_attachments(mail, attachments):
    """把本地文件作为附件添加到邮件；单个失败不影响其余。"""
    for p in (attachments or []):
        try:
            if p and os.path.exists(p):
                mail.Attachments.Add(p)
        except Exception:
            pass


def execute_send(to: str, subject: str, body: str,
                 cc: str = "", reply_to_entry_id: str = "",
                 is_html: bool = False, attachments=None) -> str:
    ol, ns = _get_outlook()
    if reply_to_entry_id:
        original = _find_item(ns, reply_to_entry_id)
        if original:
            mail = original.Reply()
        else:
            mail = ol.CreateItem(0)
    else:
        mail = ol.CreateItem(0)
    mail.To      = to
    mail.Subject = subject
    if cc:
        mail.CC = cc
    if is_html:
        # 回复时把用户内容置于顶部，保留 Outlook 生成的原文引用
        existing = ""
        if reply_to_entry_id:
            try:
                existing = mail.HTMLBody or ""
            except Exception:
                existing = ""
        mail.HTMLBody = (body or "") + existing
    else:
        mail.Body = body
    _add_attachments(mail, attachments)
    mail.Send()
    return "sent"


def execute_forward(entry_id: str, to: str, note: str = "",
                    is_html: bool = False, attachments=None) -> str:
    ol, ns = _get_outlook()
    original = _find_item(ns, entry_id)
    if not original:
        raise ValueError(f"找不到邮件: {entry_id}")
    fwd = original.Forward()
    fwd.To = to
    if note:
        if is_html:
            try:
                fwd.HTMLBody = note + (fwd.HTMLBody or "")
            except Exception:
                fwd.Body = note + "\n\n" + fwd.Body
        else:
            fwd.Body = note + "\n\n" + fwd.Body
    _add_attachments(fwd, attachments)
    fwd.Send()
    return "forwarded"


def execute_delete(entry_id: str, permanent: bool = False) -> str:
    ol, ns = _get_outlook()
    item = _find_item(ns, entry_id)
    if not item:
        raise ValueError(f"找不到邮件: {entry_id}")
    if permanent:
        item.Delete()  # 移到废纸篓
        # 若需要永久删，再从废纸篓 Delete 一次（简化：移到废纸篓即可）
    else:
        item.Delete()
    return "deleted"


# ── daemon 消费循环（在 interactive session 里调用）──────────────────────────────

def process_queue(max_age_seconds: float = 3600) -> int:
    """处理所有 pending 队列项，返回处理数量。

    max_age_seconds: 超过该时长仍未执行的动作视为“陈旧”，标记为 expired 而
    不执行。这可以避免队列长期未被消费时，重新启用后突然批量执行陈旧的
    破坏性操作（如误删邮件）。传入 0 或负数可关闭该保护。
    """
    files = _dequeue_all()
    done = 0
    now = time.time()
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("status") != "pending":
            continue
        # 陈旧动作保护
        if max_age_seconds and max_age_seconds > 0:
            created = data.get("created_at", 0)
            if created and (now - created) > max_age_seconds:
                data["status"] = "expired"
                data["error"] = f"动作超过 {int(max_age_seconds)}s 未执行，已跳过"
                data["finished_at"] = now
                f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                continue
        try:
            t = data["type"]
            if t == "send":
                execute_send(data["to"], data["subject"], data["body"],
                             data.get("cc",""), data.get("reply_to_entry_id",""),
                             is_html=data.get("is_html", False),
                             attachments=data.get("attachments") or [])
            elif t == "forward":
                execute_forward(data["entry_id"], data["to"], data.get("note",""),
                                is_html=data.get("is_html", False),
                                attachments=data.get("attachments") or [])
            elif t == "delete":
                execute_delete(data["entry_id"], data.get("permanent", False))
            _mark_done(f, result="ok")
        except Exception as e:
            _mark_done(f, error=str(e))
        done += 1
    return done
