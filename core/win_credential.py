"""邮箱凭据存取（密码不落明文）。模块名沿用历史名称，调用方无需改动。

后端按平台自动选择，也可用环境变量 QUILLBOX_CRED_BACKEND=dpapi|keyring 指定：
  - dpapi（Windows 缺省）：DPAPI 用户态加密，密文落 data/.pop3_cred.json，
    只能由**本机、本 Windows 用户**解开；文件拷走解不开。
  - keyring（其他平台缺省）：交给系统凭据库——macOS 钥匙串、Linux Secret Service、
    Windows 凭据管理器。keyring 不支持枚举，已登记的 key（邮箱地址，非密码）另记在
    data/.cred_index.json，供 list_keys() 使用。

通用约束：明文只在进程内存中出现；本模块不打印、不写日志、不返回给远程调用方。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

_DATA = Path(__file__).resolve().parent.parent / "data"
_STORE = _DATA / ".pop3_cred.json"        # dpapi 密文库（历史文件名，保持兼容）
_INDEX = _DATA / ".cred_index.json"       # keyring 后端的 key 索引（只有账户名）
_DESC = "Quillbox POP3"
_SERVICE = "quillbox"                        # keyring 服务名


def backend() -> str:
    b = (os.environ.get("QUILLBOX_CRED_BACKEND") or "").strip().lower()
    if b in ("dpapi", "keyring"):
        return b
    return "dpapi" if os.name == "nt" else "keyring"


# ── 公共小工具 ──────────────────────────────────────────────────────────────

def _read_json(p: Path) -> dict:
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ── dpapi 后端（Windows）────────────────────────────────────────────────────

def _dpapi():
    try:
        import win32crypt  # pywin32：Windows extra
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少 pywin32，无法使用 DPAPI 保存凭据：pip install pywin32"
            "（或设 QUILLBOX_CRED_BACKEND=keyring 改用系统凭据库）"
        ) from exc
    return win32crypt


def _dpapi_set(key: str, password: str) -> None:
    blob = _dpapi().CryptProtectData(password.encode("utf-8"), _DESC, None, None, None, 0)
    data = _read_json(_STORE)
    data[key] = base64.b64encode(blob).decode("ascii")
    _write_json(_STORE, data)


def _dpapi_get(key: str) -> Optional[str]:
    enc = _read_json(_STORE).get(key)
    if not enc:
        return None
    try:
        blob = base64.b64decode(enc)
    except (ValueError, TypeError):
        return None
    _desc, plain = _dpapi().CryptUnprotectData(blob, None, None, None, 0)
    return plain.decode("utf-8")


# ── keyring 后端（macOS / Linux / 可选 Windows）─────────────────────────────

def _keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 keyring，无法访问系统凭据库：pip install keyring") from exc
    return keyring


def _index_keys() -> list[str]:
    keys = _read_json(_INDEX).get("keys") or []
    return [k for k in keys if isinstance(k, str)]


def _index_save(keys: list[str]) -> None:
    _write_json(_INDEX, {"service": _SERVICE, "keys": sorted(set(keys))})


# ── 公共 API（与历史签名一致）────────────────────────────────────────────────

def store_path() -> Path:
    """凭据存放位置的说明性路径：dpapi 为密文文件，keyring 为 key 索引文件。"""
    return _STORE if backend() == "dpapi" else _INDEX


def set_password(key: str, password: str) -> None:
    """加密保存 key（建议用邮箱地址）对应的密码。"""
    if not password:
        raise ValueError("密码为空，拒绝写入")
    if backend() == "dpapi":
        _dpapi_set(key, password)
        return
    _keyring().set_password(_SERVICE, key, password)
    _index_save(_index_keys() + [key])


def get_password(key: str) -> Optional[str]:
    """返回明文密码；未登记返回 None。

    调用方必须把返回值当敏感数据：不得打印、不得写日志、不得进异常消息。
    """
    if backend() == "dpapi":
        return _dpapi_get(key)
    return _keyring().get_password(_SERVICE, key)


def has_password(key: str) -> bool:
    if backend() == "dpapi":
        return bool(_read_json(_STORE).get(key))
    return key in _index_keys() or _keyring().get_password(_SERVICE, key) is not None


def list_keys() -> list[str]:
    if backend() == "dpapi":
        return sorted(_read_json(_STORE).keys())
    return sorted(_index_keys())


def forget(key: str) -> bool:
    if backend() == "dpapi":
        data = _read_json(_STORE)
        if key not in data:
            return False
        del data[key]
        _write_json(_STORE, data)
        return True
    kr = _keyring()
    existed = kr.get_password(_SERVICE, key) is not None
    if existed:
        try:
            kr.delete_password(_SERVICE, key)
        except Exception:  # keyring.errors.PasswordDeleteError 等
            pass
    keys = _index_keys()
    if key in keys:
        keys.remove(key)
        _index_save(keys)
        existed = True
    return existed
