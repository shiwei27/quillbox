"""Quillbox 邮箱密码登记（图形版，无需命令行）。

双击 register-mail-password-gui.bat 即可。特点：
  - 密码框带掩码（输入可见为圆点），不像 getpass 那样"看着没反应"；
  - 保存走 core.win_credential（Windows 缺省 DPAPI 加密落 data/.pop3_cred.json，其他平台存系统凭据库）；
  - 明文只在内存中出现：不写日志、不进命令行参数、不显示在界面上；
  - 保存后自动对 POP3 账户做一次真实登录验证，结果写 cred_register.log（只记成败，不记密码）。
"""
from __future__ import annotations

import datetime
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOG = ROOT / "cred_register.log"

def _accounts_from_config():
    """要登记的账户：(账户, 是否支持 POP3 登录验证)，取自 config.yaml 的 pop3 / imap / ews 账户。"""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ImportError):
        return []

    def names(section):
        a = (cfg.get(section) or {}).get("accounts") or {}
        return [str(k) for k in a] if isinstance(a, dict) else []

    out, seen = [], set()
    for sec, can_check in (("pop3", True), ("imap", False), ("ews", False)):
        for acct in names(sec):
            if acct.lower() not in seen:
                seen.add(acct.lower())
                out.append((acct, can_check))
    return out


ACCOUNTS = _accounts_from_config()


def log(msg: str) -> None:
    line = "%s  %s" % (datetime.datetime.now().isoformat(timespec="seconds"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line)


def main() -> int:
    import tkinter as tk
    from tkinter import ttk

    from core import win_credential

    root = tk.Tk()
    root.title("Quillbox 邮箱密码登记")
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=16)
    frm.grid()

    ttk.Label(frm, text="密码交给系统凭据后端加密保存（Windows：DPAPI；macOS/Linux：系统钥匙串）。",
              foreground="#555").grid(column=0, row=0, columnspan=2, sticky="w", pady=(0, 12))

    entries = {}
    for i, (acct, _pop3) in enumerate(ACCOUNTS):
        ttk.Label(frm, text=acct).grid(column=0, row=1 + i, sticky="w", padx=(0, 10), pady=4)
        ent = ttk.Entry(frm, show="●", width=30)
        ent.grid(column=1, row=1 + i, pady=4)
        entries[acct] = ent
    if ACCOUNTS:
        entries[ACCOUNTS[0][0]].focus_set()
    else:
        ttk.Label(frm, text="config.yaml 里还没有配置任何邮箱账户（pop3 / imap / ews）。").grid(column=0, row=1, columnspan=2)

    status = tk.Text(frm, height=8, width=64, state="disabled", wrap="word")
    status.grid(column=0, row=1 + len(ACCOUNTS) + 1, columnspan=2, pady=(12, 0))

    def say(msg: str) -> None:
        status.configure(state="normal")
        status.insert("end", msg + "\n")
        status.see("end")
        status.configure(state="disabled")
        root.update_idletasks()
        log(msg)

    def submit() -> None:
        saved = 0
        for acct, can_check in ACCOUNTS:
            pw = entries[acct].get()
            if not pw:
                say("跳过 %s（未填）" % acct)
                continue
            try:
                win_credential.set_password(acct, pw)
                saved += 1
                say("已保存 %s" % acct)
            except Exception as exc:                      # noqa: BLE001
                say("保存失败 %s：%s" % (acct, exc))
                continue
            finally:
                entries[acct].delete(0, "end")
            if not can_check:
                say("  %s 跳过 POP3 验证（该账户不走 POP3）" % acct)
                continue
            say("  正在验证 %s 的 POP3 登录…" % acct)
            try:
                from core.pop3_extractor import from_config
                import yaml
                cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
                try:
                    ext = from_config(cfg, acct)
                except KeyError:
                    say("  验证跳过：config.yaml 的 pop3.accounts 下没有 %s" % acct)
                else:
                    count, size = ext.check_login(win_credential.get_password(acct))
                    say("  登录成功：服务器上 %d 封，%.1f MB" % (count, size / 1048576.0))
            except Exception as exc:                      # noqa: BLE001
                say("  登录验证失败：%s" % exc)
        say("已登记账号：%s" % (win_credential.list_keys() or "（空）"))
        if saved:
            say("完成。可以关闭本窗口。")

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=1 + len(ACCOUNTS), columnspan=2, sticky="e", pady=(10, 0))
    ttk.Button(btns, text="保存并验证", command=submit).grid(column=0, row=0, padx=(0, 8))
    ttk.Button(btns, text="关闭", command=root.destroy).grid(column=1, row=0)

    root.bind("<Return>", lambda _e: submit())
    log("图形登记窗口已打开")
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log("崩溃：\n" + traceback.format_exc())
        raise
