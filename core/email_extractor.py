import sys
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import win32com.client
import pythoncom

# MAPI 属性：邮件头 Message-ID（PR_INTERNET_MESSAGE_ID）。跨后端去重用，
# 与 POP3/IMAP 后端从信头取到的是同一个值。
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
import re
import logging
import html
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Set

# 内嵌图片保存目录（data/inline/{email_id}/{filename}），由 server 通过 /api/inline 提供
INLINE_ROOT = Path(__file__).resolve().parent.parent / "data" / "inline"

# MAPI 属性标签：附件的 Content-ID（用于匹配 HTML 里的 cid: 引用）
PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"


class EmailExtractor:
    """通过 Windows COM 接口提取 Outlook 邮件（Streamlit 多线程安全版）"""

    def __init__(self, skip_accounts=()):
        """skip_accounts：这些账户（Outlook 里 Store 的显示名）不再由本后端提取。

        同一邮箱若改由 IMAP 接管：两条后端同时抓同一邮箱时，去重能防重复
        入库，但"邮件归哪个文件夹"会变成谁先入库谁说了算（Outlook 本地规则分类
        ≠ 服务端分类），故按账户二选一。
        """
        self.skip_accounts = {str(a).strip().lower() for a in (skip_accounts or ())}

    # ------------------------------------------------------------------ #
    #  内部工具方法
    # ------------------------------------------------------------------ #

    def _get_namespace(self, retries: int = 3, base_delay: float = 1.0):
        """优先连接已运行的 Outlook；失败时按指数退避重试，缓解 Outlook 重启/COM 短暂掉线。"""
        import time
        pythoncom.CoInitialize()
        last_err = None
        for attempt in range(retries):
            # 优先绑定已运行实例
            try:
                outlook = win32com.client.GetActiveObject("Outlook.Application")
                return outlook.GetNamespace("MAPI")
            except Exception as e1:
                last_err = e1
            # fallback：让 Windows 自己找到已注册的 COM 服务（不新建实例、不 Logon）
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
                return outlook.GetNamespace("MAPI")
            except Exception as e2:
                last_err = e2
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt))  # 退避：1s, 2s, 4s...
        raise RuntimeError(
            f"无法连接 Outlook（COM 错误，已重试 {retries} 次）。\n"
            f"请确认：\n"
            f"  1) Outlook 已完全打开并加载完邮件\n"
            f"  2) 用【普通权限】PowerShell 运行（不要以管理员身份）\n"
            f"  3) 没有多个 Outlook 进程（用 taskkill /IM OUTLOOK.EXE /F 全部结束再重开）\n"
            f"详情: {last_err}"
        ) from last_err

    def _get_inbox(self):
        """获取默认账户收件箱（6 = olFolderInbox）"""
        namespace = self._get_namespace()
        return namespace.GetDefaultFolder(6)

    @staticmethod
    def _clean_body(raw_body: str) -> str:
        """
        清洗邮件正文：
        1. 去除 HTML 标签
        2. 还原 HTML 实体（&nbsp; &amp; 等）
        3. 合并多余空白行
        """
        if not raw_body:
            return ""
        # 去除 HTML 标签
        text = re.sub(r'<[^>]+>', ' ', raw_body)
        # 还原 HTML 实体
        text = html.unescape(text)
        # 统一换行符
        text = re.sub(r'\r\n|\r', '\n', text)
        # 合并连续空白行（超过2行压缩为1行）
        text = re.sub(r'\n{3,}', '\n\n', text)
        # 合并行内多余空格
        text = re.sub(r'[ \t]{2,}', ' ', text)
        return text.strip()

    @staticmethod
    def _safe_filename(name: str) -> str:
        """清理文件名中的非法字符，避免路径穿越/非法路径"""
        name = (name or "").strip()
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
        name = name.strip('. ') or "img"
        return name[:120]

    def _extract_html_body(self, msg, email_id: str) -> str:
        """
        提取富文本 HTML 正文，并把内嵌图片（cid: 引用）保存到磁盘、
        将 HTML 中的 cid: 引用重写为 /api/inline/{email_id}/{filename}。
        任何步骤失败都不影响主流程（降级为可用的 HTML 或空串）。
        """
        try:
            html_body = str(msg.HTMLBody) if hasattr(msg, 'HTMLBody') else ""
        except Exception:
            html_body = ""
        if not html_body:
            return ""

        # 处理内嵌图片
        try:
            atts = msg.Attachments if hasattr(msg, 'Attachments') else None
            if atts and atts.Count > 0:
                saved_dir = INLINE_ROOT / email_id
                dir_made = False
                for i in range(1, atts.Count + 1):
                    try:
                        att = atts.Item(i)
                        cid = ""
                        try:
                            cid = str(att.PropertyAccessor.GetProperty(PR_ATTACH_CONTENT_ID) or "")
                        except Exception:
                            cid = ""
                        cid = cid.strip().strip('<>').strip()
                        if not cid:
                            continue
                        # 仅当 HTML 确实引用了该 cid 时才保存，避免落无用文件
                        if f"cid:{cid}" not in html_body and cid not in html_body:
                            continue

                        fname = self._safe_filename(str(getattr(att, 'FileName', '') or f"{cid}"))
                        if not dir_made:
                            saved_dir.mkdir(parents=True, exist_ok=True)
                            dir_made = True
                        dest = saved_dir / fname
                        try:
                            att.SaveAsFile(str(dest))
                        except Exception:
                            continue

                        url = f"/api/inline/{email_id}/{fname}"
                        # 覆盖 cid:xxx 以及少数客户端直接用 "xxx" 引用的情况
                        html_body = html_body.replace(f"cid:{cid}", url)
                    except Exception:
                        continue
        except Exception:
            pass

        return html_body

    @staticmethod
    def _safe_message_id(msg) -> str:
        """取 PR_INTERNET_MESSAGE_ID 并归一化；取不到返回空串（不抛异常）。

        草稿、部分会议项或存储异常的项目可能没有这个属性，属正常情况。
        """
        try:
            raw = msg.PropertyAccessor.GetProperty(PR_INTERNET_MESSAGE_ID)
        except Exception:
            return ""
        mid = str(raw or "").strip()
        if mid.startswith("<") and mid.endswith(">"):
            mid = mid[1:-1]
        return mid.strip().lower()

    @staticmethod
    def _to_timestamp(received_time) -> float:
        """将 COM 时间对象转为 Unix 时间戳，方便排序和过滤"""
        try:
            return received_time.replace(tzinfo=None).timestamp()
        except Exception:
            return 0.0

    def _safe_get_attachments(self, msg) -> List[str]:
        """安全提取附件文件名，单个附件出错不影响整体"""
        try:
            if hasattr(msg, 'Attachments') and msg.Attachments.Count > 0:
                names = []
                for att in msg.Attachments:
                    try:
                        names.append(att.FileName)
                    except Exception:
                        pass
                return names
        except Exception:
            pass
        return []

    @staticmethod
    def _safe_get_recipients(msg) -> List[Dict]:
        """
        提取收件人列表（To + CC），返回 [{"name": ..., "email": ..., "type": "to"|"cc"}]
        olTo=1, olCC=2, olBCC=3
        """
        result = []
        try:
            if not hasattr(msg, 'Recipients'):
                return result
            for r in msg.Recipients:
                try:
                    rtype = getattr(r, 'Type', 1)
                    if rtype not in (1, 2):   # 只要 To 和 CC
                        continue
                    result.append({
                        "name":  str(r.Name) if hasattr(r, 'Name') else "",
                        "email": str(r.Address) if hasattr(r, 'Address') else "",
                        "type":  "to" if rtype == 1 else "cc",
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------ #
    #  核心递归提取
    # ------------------------------------------------------------------ #

    # 跳过非邮件文件夹（联系人/日历/任务等没有 ReceivedTime）
    _SKIP_FOLDERS = {"联系人", "日历", "任务", "便笺", "日记", "ExternalContacts",
                     "Contacts", "Calendar", "Tasks", "Notes", "Journal"}

    def _extract_folder_emails(self, folder, cutoff_date, result, seen_ids, account_name,
                               on_progress=None):
        """递归提取文件夹中的邮件"""
        folder_name = "Unknown"
        try:
            folder_name = str(folder.Name) if hasattr(folder, 'Name') else "Unknown"

            if folder_name in self._SKIP_FOLDERS:
                return

            print(f"📁 正在扫描文件夹: {folder_name}")

            try:
                messages = folder.Items
                messages.Sort("[ReceivedTime]", True)
            except Exception:
                return

            count = messages.Count
            extracted = 0
            for idx in range(1, count + 1):
                try:
                    msg = messages.Item(idx)

                    if hasattr(msg, 'Class') and msg.Class != 43:
                        continue

                    if hasattr(msg, 'ReceivedTime'):
                        rt = msg.ReceivedTime
                        if hasattr(rt, 'tzinfo') and rt.tzinfo is not None:
                            rt = rt.replace(tzinfo=None)
                        if rt < cutoff_date:
                            # 邮件已按 ReceivedTime 降序排列，遇到第一封早于
                            # cutoff 的邮件后，后续邮件必然更早，直接 break 终止
                            # 本文件夹扫描，避免每次增量同步都全量遍历（性能关键）。
                            break

                    email_id = str(msg.EntryID)
                    if email_id in seen_ids:
                        continue
                    seen_ids.add(email_id)

                    raw_body = msg.Body if hasattr(msg, 'Body') else ""
                    subject = str(msg.Subject) if hasattr(msg, 'Subject') else "(无主题)"
                    email_data = {
                        "id":                 email_id,
                        "subject":            subject,
                        "body":               self._clean_body(raw_body),
                        "html_body":          self._extract_html_body(msg, email_id),
                        "sender":             str(msg.SenderName) if hasattr(msg, 'SenderName') else "",
                        "sender_email":       str(msg.SenderEmailAddress) if hasattr(msg, 'SenderEmailAddress') else "",
                        "recipients":         self._safe_get_recipients(msg),
                        "time":               str(msg.ReceivedTime) if hasattr(msg, 'ReceivedTime') else "",
                        "received_timestamp": self._to_timestamp(msg.ReceivedTime) if hasattr(msg, 'ReceivedTime') else 0,
                        "unread":             msg.UnRead if hasattr(msg, 'UnRead') else False,
                        "folder":             folder_name,
                        "account":            account_name,
                        "has_attachment":     msg.Attachments.Count > 0 if hasattr(msg, 'Attachments') else False,
                        "attachments":        self._safe_get_attachments(msg),
                        "message_id":         self._safe_message_id(msg),
                    }
                    result.append(email_data)
                    extracted += 1

                    if extracted % 10 == 0:
                        print(f"  [{folder_name}] 已提取 {extracted} 封，主题: {subject[:40]}")
                        if on_progress:
                            on_progress(account_name, folder_name, len(result), subject)

                except Exception as e:
                    continue

            if extracted:
                print(f"  [{folder_name}] 完成，共 {extracted} 封")
                if on_progress:
                    on_progress(account_name, folder_name, len(result), f"[{folder_name}] 完成，共 {extracted} 封")

            if hasattr(folder, 'Folders') and folder.Folders.Count > 0:
                for j in range(1, folder.Folders.Count + 1):
                    sub_folder = folder.Folders.Item(j)
                    self._extract_folder_emails(
                        sub_folder, cutoff_date, result, seen_ids, account_name,
                        on_progress=on_progress,
                    )

        except Exception as e:
            print(f"⚠️  无法访问文件夹 {folder_name}: {e}")

    # ------------------------------------------------------------------ #
    #  对外 API
    # ------------------------------------------------------------------ #

    def get_all_emails(self, days_ago: int = 1095, on_progress=None) -> List[Dict]:
        """
        获取指定天数内的全部邮件（遍历所有账户和文件夹）。
        on_progress(account, folder, total_so_far, last_subject) 每 10 封回调一次。
        """
        namespace = self._get_namespace()
        cutoff_date = datetime.now().replace(tzinfo=None) - timedelta(days=days_ago)
        result: List[Dict] = []
        seen_ids: Set[str] = set()

        stores = namespace.Stores
        try:
            store_count = int(stores.Count)
        except Exception as e:
            print(f"⚠️  无法枚举账户列表: {e}")
            logging.warning("无法枚举 Outlook 账户列表: %s", e)
            store_count = 0
        for i in range(1, store_count + 1):
            account_name = ""
            try:
                store = stores.Item(i)
                account_name = store.DisplayName
                if str(account_name).strip().lower() in getattr(self, "skip_accounts", set()):
                    print(f"⏭️  跳过账户（已由 IMAP 接管）: {account_name}")
                    logging.info("跳过账户（已由 IMAP 接管）: %s", account_name)
                    continue
                print(f"📂 正在扫描账户: {account_name}")
                root_folder = store.GetRootFolder()
                self._extract_folder_emails(
                    root_folder, cutoff_date, result, seen_ids, account_name,
                    on_progress=on_progress,
                )
            except Exception as e:
                print(f"⚠️  无法访问账户 [{account_name or 'Unknown'}]: {e}")
                logging.warning("无法访问账户 [%s]: %s", account_name or "Unknown", e)

        print(f"✅ 共提取到 {len(result)} 封唯一邮件")
        return result

    def get_incremental_emails(self, days_ago: int = 7, on_progress=None) -> List[Dict]:
        """获取近 N 天的邮件，用于增量索引"""
        return self.get_all_emails(days_ago=days_ago, on_progress=on_progress)

    def get_unread_emails(self, count: int = 10) -> List[Dict]:
        """获取未读邮件（兼容旧接口）"""
        return self.fetch_unread_emails(count)

    def fetch_unread_emails(self, count: int = 10) -> List[Dict]:
        """获取收件箱中最新的未读邮件"""
        try:
            inbox = self._get_inbox()   # ← 修复：方法现在已定义
            messages = inbox.Items
            messages.Sort("[ReceivedTime]", True)
            messages = messages.Restrict("[UnRead] = True")

            result = []
            for i, msg in enumerate(messages):
                if i >= count:
                    break
                try:
                    raw_body = msg.Body if hasattr(msg, 'Body') else ""
                    email_data = {
                        "id":                 str(msg.EntryID),
                        "subject":            str(msg.Subject) if hasattr(msg, 'Subject') else "(无主题)",
                        "body":               self._clean_body(raw_body),
                        "sender":             str(msg.SenderName) if hasattr(msg, 'SenderName') else "",
                        "sender_email":       str(msg.SenderEmailAddress) if hasattr(msg, 'SenderEmailAddress') else "",
                        "time":               str(msg.ReceivedTime),
                        "received_timestamp": self._to_timestamp(msg.ReceivedTime),
                        "unread":             True,
                        "folder":             "收件箱",
                        "account":            "",
                        "has_attachment":     False,
                        "attachments":        self._safe_get_attachments(msg),
                    }
                    result.append(email_data)
                except Exception:
                    continue

            return result

        except Exception as e:
            print(f"❌ 获取未读邮件失败: {e}")
            return []
