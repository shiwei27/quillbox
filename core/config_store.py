# -*- coding: utf-8 -*-
"""config.yaml 的唯一写入口：保留注释、键序与原有写法，只改真正变化的键。

为什么需要：server.py / daemon.py 原来用 yaml.dump(整份字典) 回写，每次保存都会
删光注释（如 smtp.accounts 那段说明）、重排键序、把 null 改写。改成：
  1. ruamel 往返读出当前文件（带注释的 CommentedMap）；
  2. 把调用方给的新字典「深合并」进去——只有值不同的键才替换，相同的子树原样保留
     （包括其中的注释）；调用方删掉的键也同步删除；
  3. 原子替换文件（Windows 上目标被占用时短暂重试，仍失败则原地写）。

调用方语义不变：仍然是 load → 改 dict → save(dict)。
ruamel 不可用时退回 yaml.dump（行为与改前一致）。
"""
from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def _yaml():
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.representer.add_representer(
        type(None), lambda r, d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


def _merge(dst, src):
    """把普通 dict/list 结构 src 合并进 ruamel 结构 dst；返回合并后的对象。"""
    from ruamel.yaml.comments import CommentedMap
    if isinstance(dst, dict) and isinstance(src, dict):
        for k in [k for k in list(dst.keys()) if k not in src]:
            del dst[k]
        for k, v in src.items():
            if k in dst:
                dst[k] = _merge(dst[k], v)
            else:
                dst[k] = v
        return dst
    if isinstance(dst, list) and isinstance(src, list):
        if len(dst) == len(src):
            for i, v in enumerate(src):
                dst[i] = _merge(dst[i], v)
            return dst
        # 长度变了（增删规则等）：逐项尽量复用已有元素，避免整段丢注释
        out = type(dst)()
        for i, v in enumerate(src):
            out.append(_merge(dst[i], v) if i < len(dst) else v)
        return out
    return src if src != dst or type(src) is not type(dst) else dst


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:      # Windows：目标正被其他进程打开
            time.sleep(0.2)
    path.write_text(text, encoding="utf-8", newline="")
    try:
        tmp.unlink()
    except OSError:
        pass


def save_config(path, data: dict) -> None:
    path = Path(path)
    with _LOCK:
        try:
            y = _yaml()
        except ImportError:
            import yaml as _py
            with open(path, "w", encoding="utf-8") as f:
                _py.dump(data, f, allow_unicode=True, default_flow_style=False)
            return
        with open(path, encoding="utf-8") as f:
            doc = y.load(f)
        if doc is None:
            doc = {}
        doc = _merge(doc, data)
        buf = io.StringIO()
        y.dump(doc, buf)
        new = buf.getvalue()
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = None
        if new != old:
            _write_atomic(path, new)
