# -*- coding: utf-8 -*-
"""core/win_credential 的双后端（dpapi / keyring）单元测试：全部用替身，不碰真实凭据库。

- keyring 后端：注入内存版 keyring 模块，验证 set/get/has/list/forget 与 key 索引文件；
- dpapi 后端：注入可逆的假 win32crypt，验证密文落盘格式与历史一致（base64 字典）；
- 后端选择：QUILLBOX_CRED_BACKEND 显式指定优先，缺省按平台。
数据目录指向临时目录，跑完即删。
"""
import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
fail = []


def check(label, got, want):
    ok = got == want
    print("  %s %-40s got=%r" % ("OK  " if ok else "FAIL", label, got))
    if not ok:
        fail.append(label)


class _MemKeyring(types.ModuleType):
    def __init__(self):
        super().__init__("keyring")
        self.store = {}

    def set_password(self, svc, key, pw):
        self.store[(svc, key)] = pw

    def get_password(self, svc, key):
        return self.store.get((svc, key))

    def delete_password(self, svc, key):
        self.store.pop((svc, key))


class _FakeWin32Crypt(types.ModuleType):
    def __init__(self):
        super().__init__("win32crypt")

    def CryptProtectData(self, data, desc, *a):
        return b"ENC:" + data[::-1]

    def CryptUnprotectData(self, blob, *a):
        return ("desc", blob[4:][::-1])


tmp = Path(tempfile.mkdtemp(prefix="credtest-"))
mem = _MemKeyring()
sys.modules["keyring"] = mem
sys.modules["win32crypt"] = _FakeWin32Crypt()

import core.win_credential as wc  # noqa: E402
wc = importlib.reload(wc)
wc._DATA = tmp
wc._STORE = tmp / ".pop3_cred.json"
wc._INDEX = tmp / ".cred_index.json"

print("=== 后端选择 ===")
os.environ["QUILLBOX_CRED_BACKEND"] = "keyring"
check("显式 keyring", wc.backend(), "keyring")
os.environ["QUILLBOX_CRED_BACKEND"] = "dpapi"
check("显式 dpapi", wc.backend(), "dpapi")
os.environ["QUILLBOX_CRED_BACKEND"] = ""
check("缺省按平台", wc.backend(), "dpapi" if os.name == "nt" else "keyring")

print("=== keyring 后端 ===")
os.environ["QUILLBOX_CRED_BACKEND"] = "keyring"
wc.set_password("a@example.com", "pw-a")
wc.set_password("b@example.com", "pw-b")
check("取回明文", wc.get_password("a@example.com"), "pw-a")
check("未登记返回 None", wc.get_password("x@example.com"), None)
check("has_password", wc.has_password("b@example.com"), True)
check("list_keys 来自索引", wc.list_keys(), ["a@example.com", "b@example.com"])
idx = json.loads((tmp / ".cred_index.json").read_text(encoding="utf-8"))
check("索引文件不含密码", "pw-a" in json.dumps(idx), False)
check("forget 已登记", wc.forget("a@example.com"), True)
check("forget 后取不到", wc.get_password("a@example.com"), None)
check("forget 未登记", wc.forget("a@example.com"), False)
check("store_path 指向索引", wc.store_path().name, ".cred_index.json")
try:
    wc.set_password("c@example.com", "")
    check("空密码拒绝写入", False, True)
except ValueError:
    check("空密码拒绝写入", True, True)

print("=== dpapi 后端（假 win32crypt）===")
os.environ["QUILLBOX_CRED_BACKEND"] = "dpapi"
wc.set_password("w@example.com", "pw-w")
raw = json.loads((tmp / ".pop3_cred.json").read_text(encoding="utf-8"))
check("密文库是 base64 字典", sorted(raw.keys()), ["w@example.com"])
check("密文库不含明文", "pw-w" in json.dumps(raw), False)
check("取回明文", wc.get_password("w@example.com"), "pw-w")
check("list_keys", wc.list_keys(), ["w@example.com"])
check("forget", wc.forget("w@example.com"), True)
check("store_path 指向密文库", wc.store_path().name, ".pop3_cred.json")

for p in tmp.iterdir():
    p.unlink()
tmp.rmdir()
print("\nRESULT", "OK" if not fail else "FAILED: %s" % fail)
sys.exit(1 if fail else 0)
