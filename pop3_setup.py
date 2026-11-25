"""邮箱凭据登记工具（IMAP / POP3 / EWS / SMTP 共用；在运行本服务的用户的本地会话中执行）。

用法::

    python pop3_setup.py set     you@example.com
    python pop3_setup.py check   you@example.com
    python pop3_setup.py list
    python pop3_setup.py forget  you@example.com

`set` 会交互式提示输入密码（getpass，不回显、不进命令行历史）并交给凭据后端：
Windows 缺省用 DPAPI 加密写入 data/.pop3_cred.json（只能由本机本用户解开）；
macOS / Linux 缺省存入系统凭据库（钥匙串 / Secret Service）。见 core/win_credential.py。

注意：请在本机直接运行（本地终端 / RDP 会话），不要通过远程 SSH 会话输入密码——
远程会话中键入的明文会经网络传输并可能被记录。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from core import win_credential  # noqa: E402
from core.pop3_extractor import from_config  # noqa: E402

CONFIG = Path(__file__).resolve().parent / "config.yaml"


def _load_cfg() -> dict:
    if not CONFIG.is_file():
        return {}
    try:
        return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"⚠️  config.yaml 解析失败：{exc}")
        return {}


def cmd_set(account: str) -> int:
    if not sys.stdin.isatty():
        print("✗ 需要交互式终端输入密码，拒绝从管道读取。")
        return 2
    pwd = getpass.getpass(f"请输入 {account} 的邮箱密码（不回显）：")
    if not pwd:
        print("✗ 密码为空，未写入。")
        return 2
    again = getpass.getpass("请再输入一次确认：")
    if pwd != again:
        print("✗ 两次输入不一致，未写入。")
        return 2
    win_credential.set_password(account, pwd)
    del pwd, again
    print(f"✓ 已保存（后端 {win_credential.backend()}，{win_credential.store_path()}）")
    print("  下一步验证登录：python pop3_setup.py check " + account)
    return 0


def cmd_check(account: str) -> int:
    if not win_credential.has_password(account):
        print(f"✗ 未登记 {account}，先执行：python pop3_setup.py set {account}")
        return 2
    cfg = _load_cfg()
    try:
        ext = from_config(cfg, account)
    except KeyError as exc:
        print(f"✗ {exc}")
        return 2
    pwd = win_credential.get_password(account)
    print(f"→ 连接 {ext.host}:{ext.port} ssl={ext.use_ssl} user={ext.username}")
    try:
        count, size = ext.check_login(pwd)
    except Exception as exc:
        print(f"✗ 登录失败：{exc}")
        return 1
    finally:
        del pwd
    print(f"✓ 登录成功：服务器上 {count} 封邮件，合计 {size / 1048576:.1f} MB")
    print("  下一步空跑体检：python test_pop3.py " + account + " --days 7")
    return 0


def cmd_list() -> int:
    keys = win_credential.list_keys()
    if not keys:
        print("（凭据库为空）")
    for k in keys:
        print(f"  {k}")
    print(f"凭据后端：{win_credential.backend()}；位置：{win_credential.store_path()}")
    return 0


def cmd_forget(account: str) -> int:
    ok = win_credential.forget(account)
    print(("✓ 已删除 " if ok else "－ 本来就不存在 ") + account)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Quillbox POP3 凭据登记")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("set", "check", "forget"):
        p = sub.add_parser(name)
        p.add_argument("account")
    sub.add_parser("list")
    args = ap.parse_args()

    if args.cmd == "set":
        return cmd_set(args.account)
    if args.cmd == "check":
        return cmd_check(args.account)
    if args.cmd == "forget":
        return cmd_forget(args.account)
    return cmd_list()


if __name__ == "__main__":
    raise SystemExit(main())
