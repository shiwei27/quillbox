# -*- coding: utf-8 -*-
"""AI 分析用的附件文字。

取件：复用 attachment_fetch 的通道（IMAP / EWS，只读 PEEK，不改已读状态）和磁盘缓存；同一封邮件的多个附件只回服务器取一次原文。
抽取：xlsx / docx / pdf / pptx / html / txt·csv 等 → 纯文字。.doc / .xls / .ppt 等旧格式、压缩包、图片不读，如实报告。
安全：附件名与内容都是不可信输入——只当数据；zip 容器先查解压后大小（防压缩炸弹）；单文件大小、页数、行数、字数都有上限；
      日志只记文件名与字数，不记内容；任何异常都吞掉并返回原因，绝不让附件问题拖垮邮件分析。
"""
import concurrent.futures as _cf
import html as _html
import io
import json
import logging
import re
import zipfile
from collections import OrderedDict

log = logging.getLogger(__name__)

READ_EXT = (".xlsx", ".xlsm", ".docx", ".pdf", ".pptx", ".html", ".htm", ".txt", ".csv", ".md", ".json", ".log")
UNSUPPORTED_EXT = (".doc", ".xls", ".ppt", ".wps", ".zip", ".rar", ".7z", ".eml", ".msg")
MAX_FILES = 3
PER_FILE_CHARS = 6000
TOTAL_CHARS = 12000
MAX_MB = 10
FETCH_TIMEOUT = 45
_MEMO = OrderedDict()          # (email_id, name) → (text, status, reason, truncated)；进程内，不落盘
_MEMO_MAX = 128


def attach_names(email: dict) -> list:
    a = email.get("attachments")
    try:
        if isinstance(a, str):
            a = json.loads(a) if a.strip().startswith("[") else []
        out = []
        for x in a or []:
            n = (x.get("name") or x.get("filename")) if isinstance(x, dict) else x
            if n:
                out.append(str(n))
        return out
    except Exception:                                   # noqa: BLE001
        return []


def pick_names(email: dict) -> list:
    """可读的文档型附件优先，其次是已知读不了的（用来如实报告）。图片 / 无扩展名的忽略。"""
    names = attach_names(email)
    ok = [n for n in names if n.lower().endswith(READ_EXT)]
    bad = [n for n in names if n.lower().endswith(UNSUPPORTED_EXT)]
    return (ok + bad)[:MAX_FILES]


# ── 抽取 ─────────────────────────────────────────────────────────────
def _check_zip(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > 200 * 1024 * 1024 or any(i.file_size > 100 * 1024 * 1024 for i in z.infolist()):
            raise ValueError("压缩容器解压后过大")


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return ("%d" % v) if v == int(v) else ("%g" % round(v, 6))
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%Y-%m-%d") if not getattr(v, "hour", 0) and not getattr(v, "minute", 0) else v.strftime("%Y-%m-%d %H:%M")
        except Exception:                               # noqa: BLE001
            return str(v)
    return " ".join(str(v).split())


def _xlsx(data: bytes, limit: int) -> str:
    _check_zip(data)
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out, used = [], 0
    try:
        for ws in wb.worksheets[:5]:
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 300:
                    break
                cells = [_cell(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    rows.append(" | ".join(cells))
                    used += len(rows[-1])
                if used > limit:
                    break
            if rows:
                out.append("〔工作表 %s〕" % ws.title + "；".join(rows))
            if used > limit:
                break
    finally:
        wb.close()
    return "\n".join(out)


def _docx(data: bytes, limit: int) -> str:
    _check_zip(data)
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = Document(io.BytesIO(data))
    parts, used = [], 0
    for ch in d.element.body.iterchildren():
        if ch.tag.endswith("}p"):
            t = Paragraph(ch, d).text.strip()
            if t:
                parts.append(t)
        elif ch.tag.endswith("}tbl"):
            tb = Table(ch, d)
            for r in tb.rows[:200]:
                seen, cells = set(), []
                for c in r.cells:                       # 合并单元格会重复出现同一个 cell
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc)); cells.append(" ".join(c.text.split()))
                if any(cells):
                    parts.append(" | ".join(cells))
        used = sum(len(p) for p in parts)
        if used > limit:
            break
    return "\n".join(parts)


def _pdf(data: bytes, limit: int) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        try:
            if not r.decrypt(""):
                raise ValueError("PDF 有密码")
        except Exception:                               # noqa: BLE001
            raise ValueError("PDF 有密码")
    out, used = [], 0
    for p in r.pages[:25]:
        t = (p.extract_text() or "").strip()
        if t:
            out.append(t); used += len(t)
        if used > limit:
            break
    return "\n".join(out)


def _pptx(data: bytes, limit: int) -> str:
    _check_zip(data)
    out, used = [], 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
        for i, n in enumerate(slides[:60], 1):
            xml = z.read(n).decode("utf-8", "replace")
            t = " ".join(_html.unescape(x).strip() for x in re.findall(r"<a:t>([^<]*)</a:t>", xml) if x.strip())
            if t:
                out.append("〔幻灯片%d〕%s" % (i, t)); used += len(t)
            if used > limit:
                break
    return "\n".join(out)


def _htmltext(data: bytes, limit: int) -> str:
    t = _decode(data)
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<(br|/p|/div|/tr|/li|/h\d)[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"</t[dh]>", " | ", t, flags=re.I)
    return _html.unescape(re.sub(r"<[^>]+>", " ", t))


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def extract_text(name: str, data: bytes, limit: int = PER_FILE_CHARS):
    """→ (text, status, reason, truncated)。status: read / empty / scan / unsupported / failed。"""
    low = name.lower()
    try:
        if low.endswith((".xlsx", ".xlsm")):
            raw = _xlsx(data, limit)
        elif low.endswith(".docx"):
            raw = _docx(data, limit)
        elif low.endswith(".pdf"):
            raw = _pdf(data, limit)
            if len(raw.strip()) < 30:
                return "", "scan", "PDF 里没有可抽取的文字（可能是扫描件 / 图片）", False
        elif low.endswith(".pptx"):
            raw = _pptx(data, limit)
        elif low.endswith((".html", ".htm")):
            raw = _htmltext(data, limit)
        elif low.endswith(READ_EXT):
            raw = _decode(data)
        else:
            return "", "unsupported", "暂不支持这种格式", False
    except Exception as e:                              # noqa: BLE001
        return "", "failed", "解析失败（%s）" % (str(e)[:40] or type(e).__name__), False
    text = re.sub(r"[ \t\u00a0]+", " ", raw.replace("\r", "\n"))
    text = re.sub(r"\s*\n\s*", "；", text.strip())
    if not text:
        return "", "empty", "文件里没有文字内容", False
    return (text[:limit], "read", "", len(text) > limit)


# ── 取件（同一封邮件只回服务器取一次原文）───────────────────────────────
def fetch_many(cfg: dict, row: dict, names: list, max_bytes: int, timeout: int = FETCH_TIMEOUT) -> dict:
    """→ {name: (bytes|None, error)}。先看磁盘缓存；缺的合并成一次取原文，再逐个抽。"""
    from core import attachment_fetch as af
    res, missing = {}, []
    for n in names:
        try:
            cp = af.cache_path(row.get("id"), n)
            if cp.exists() and cp.stat().st_size > 0:
                res[n] = (None, "附件超过 %d MB，未读取" % (max_bytes // 1048576)) if cp.stat().st_size > max_bytes else (cp.read_bytes(), "")
                continue
        except Exception:                               # noqa: BLE001
            pass
        missing.append(n)
    if not missing:
        return res
    if not (row.get("message_id") or "").strip():
        return dict(res, **{n: (None, "这封邮件没有 Message-ID，无法取回附件") for n in missing})

    def work():
        account, folder, mid = (row.get("account") or "").strip(), (row.get("folder") or "").strip(), row["message_id"].strip()
        if account in ((cfg.get("ews") or {}).get("accounts") or {}):
            raw = af._raw_via_ews(cfg, account, folder, mid)         # noqa: SLF001
        elif account in ((cfg.get("imap") or {}).get("accounts") or {}):
            raw = af._raw_via_imap(cfg, account, folder, mid, float(row.get("received_timestamp") or 0))   # noqa: SLF001
        else:
            raise af.AttachmentError("账户 %s 没有可用的取件通道" % account)
        out = {}
        for n in missing:
            got = af.extract_from_mime(raw, n)
            if not got:
                out[n] = (None, "邮件原文里找不到这个附件"); continue
            real, data = got
            if len(data) > max_bytes:
                out[n] = (None, "附件超过 %d MB，未读取" % (max_bytes // 1048576)); continue
            try:
                cp = af.cache_path(row.get("id"), real)
                cp.parent.mkdir(parents=True, exist_ok=True)
                tmp = cp.with_suffix(cp.suffix + ".part"); tmp.write_bytes(data); tmp.replace(cp)
            except Exception:                           # noqa: BLE001
                pass
            out[n] = (data, "")
        return out
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(work)
    try:
        res.update(fut.result(timeout=timeout))
    except _cf.TimeoutError:
        res.update({n: (None, "取回附件超时") for n in missing})
    except Exception as e:                              # noqa: BLE001
        msg = "在邮件服务器上找不到这封邮件，可能已被移动或删除" if "找不到这封邮件" in str(e) else "取回附件失败：%s" % (str(e)[:60] or type(e).__name__)
        res.update({n: (None, msg) for n in missing})
    finally:
        ex.shutdown(wait=False)
    return res


# ── 对外入口 ─────────────────────────────────────────────────────────
def read_for_analysis(cfg: dict, row: dict, total_chars: int = TOTAL_CHARS):
    """→ (文字块, info 列表)。文字块形如「【附件：x.xlsx】…」；info 每项 {name,status,chars,truncated,reason}。
    关闭开关 / 系统发件人 / 命中排除名单 / 没有可读附件时返回 ("", [])。"""
    ai = (cfg.get("ai") or {})
    if ai.get("read_attachments", True) is False:
        return "", []
    se = str(row.get("sender_email") or "").lower()
    subj = str(row.get("subject") or "")
    skips = [str(x).lower() for x in (ai.get("skip_sender_patterns") or []) + (ai.get("attachment_skip_senders") or []) if x]
    if any(p in se for p in skips) or any(str(k) and str(k) in subj for k in (ai.get("attachment_skip_subjects") or [])):
        return "", []
    names = pick_names(row)
    if not names:
        return "", []
    max_bytes = int(ai.get("attachment_max_mb", MAX_MB)) * 1048576
    eid = str(row.get("id") or "")
    todo = [n for n in names if (eid, n) not in _MEMO]
    got = fetch_many(cfg, row, [n for n in todo if n.lower().endswith(READ_EXT)], max_bytes) if todo else {}
    blocks, info, left = [], [], total_chars
    for n in names:
        key = (eid, n)
        if key in _MEMO:
            text, status, reason, trunc = _MEMO[key]
        elif not n.lower().endswith(READ_EXT):
            text, status, reason, trunc = "", "unsupported", "暂不支持这种格式", False
        else:
            data, err = got.get(n, (None, "未取到"))
            if data is None:
                text, status, reason, trunc = "", "failed", err, False
            else:
                text, status, reason, trunc = extract_text(n, data, min(PER_FILE_CHARS, max(left, 800)))
            if status in ("read", "empty", "scan", "unsupported") or "超过" in reason:
                _MEMO[key] = (text, status, reason, trunc)           # 取件失败（超时 / 连不上）不记，下次再试
                while len(_MEMO) > _MEMO_MAX:
                    _MEMO.popitem(last=False)
        if status == "read" and left > 0:
            t = text[:left]
            trunc = trunc or len(t) < len(text)
            blocks.append("【附件：%s】%s%s" % (n, t, " …（附件内容过长，已截断）" if trunc else ""))
            left -= len(t)
            info.append({"name": n[:80], "status": "read", "chars": len(t), "truncated": bool(trunc), "reason": ""})
        else:
            info.append({"name": n[:80], "status": status if status != "read" else "failed", "chars": 0, "truncated": False, "reason": reason or "总字数已达上限"})
        log.info("[附件分析] %s … %s %s %d字", eid[:16], n[:40], info[-1]["status"], info[-1]["chars"])
    return "\n".join(blocks), info
