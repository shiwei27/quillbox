"""令牌门禁离线自检：只抽取 server.py 中的门禁代码块单独跑，不导入 server（不加载嵌入模型、不开业务库）。

用法：python test_auth_gate.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BEGIN = "# >>> QUILLBOX-AUTH-GATE BEGIN (token gate; loopback exempt) >>>"
END = "# <<< QUILLBOX-AUTH-GATE END <<<"

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "server.py"
FAILED: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ((" -> " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    text = SRC.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print("FAIL: server.py 中找不到门禁代码块标记")
        return 2
    block = text.split(BEGIN, 1)[1].split(END, 1)[0]

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="authgate_"))
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    ns = {"__name__": "authgate_probe", "ROOT": tmp, "app": app}
    exec(compile("import os\n" + block, "<auth-gate>", "exec"), ns)

    token = ns["UI_TOKEN"]
    tokfile = tmp / "data" / ".auth_token"
    print("令牌自动生成：%s 位十六进制；落盘 %s" % (len(token), tokfile.is_file()))
    check("令牌长度=32 hex", len(token) == 32 and all(c in "0123456789abcdef" for c in token))
    check("令牌已写入 data/.auth_token", tokfile.is_file() and tokfile.read_text().strip() == token)

    # 远程来源（TestClient 默认 client host = "testclient"，非 loopback）
    remote = TestClient(app)
    r = remote.get("/ping")
    check("远程无令牌 -> 401", r.status_code == 401, "got %s" % r.status_code)

    r = remote.get("/ping", params={"token": "deadbeef" * 4})
    check("远程错误令牌 -> 401", r.status_code == 401, "got %s" % r.status_code)

    fresh = TestClient(app)
    r = fresh.get("/ping", params={"token": token})
    check("远程 ?token 正确 -> 200", r.status_code == 200, "got %s" % r.status_code)
    check("首次带 ?token 会下发 cookie", "quillbox_token" in r.headers.get("set-cookie", ""),
          r.headers.get("set-cookie", "<none>"))
    check("cookie 带 HttpOnly", "HttpOnly" in r.headers.get("set-cookie", ""))
    r2 = fresh.get("/ping")
    check("后续凭 cookie 免带令牌 -> 200", r2.status_code == 200, "got %s" % r2.status_code)

    r = remote.get("/ping", headers={"X-Quillbox-Token": token})
    check("请求头 X-Quillbox-Token -> 200", r.status_code == 200, "got %s" % r.status_code)

    # loopback 豁免（daemon.py 走 127.0.0.1 内部调用）
    # AUTHGATE-LOOPBACK-HARDEN-20260930：豁免还要求 Host 为本机（防 DNS rebinding）、
    # Origin/Referer 若有必须本机、Sec-Fetch-Site 若有只能 same-origin/none（防跨站 CSRF）。
    local = TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8765")
    r = local.get("/ping")
    check("loopback 127.0.0.1 免令牌 -> 200", r.status_code == 200, "got %s" % r.status_code)
    check("loopback 不下发 cookie", "set-cookie" not in {k.lower() for k in r.headers})
    r = local.get("/ping", headers={"Host": "127.0.0.1:18765"})
    check("loopback 隧道端口 Host=127.0.0.1:18765 -> 200", r.status_code == 200, "got %s" % r.status_code)
    r = local.get("/ping", headers={"Host": "[::1]:8765"})
    check("loopback Host=[::1] -> 200", r.status_code == 200, "got %s" % r.status_code)
    r = local.get("/ping", headers={"Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"})
    check("loopback 同源浏览器请求 -> 200", r.status_code == 200, "got %s" % r.status_code)
    rebind = TestClient(app, client=("127.0.0.1", 51234))   # Host=testserver，模拟 rebinding 域名
    r = rebind.get("/ping")
    check("loopback 但 Host 非本机（DNS rebinding）-> 401", r.status_code == 401, "got %s" % r.status_code)
    r = rebind.get("/ping", headers={"X-Quillbox-Token": token})
    check("loopback Host 非本机但带正确令牌 -> 200", r.status_code == 200, "got %s" % r.status_code)
    for label, hdr in (("Origin=外站", {"Origin": "http://evil.example"}),
                       ("Referer=外站", {"Referer": "http://evil.example/x"}),
                       ("Origin=null", {"Origin": "null"}),
                       ("Sec-Fetch-Site=cross-site", {"Sec-Fetch-Site": "cross-site"})):
        r = local.get("/ping", headers=hdr)
        check("loopback %s 无令牌 -> 401" % label, r.status_code == 401, "got %s" % r.status_code)

    # 来源地址缺失：门禁取来源的写法会把它变成 ""，
    # 改前 "" 在 _LOCAL_HOSTS 里 => 匿名放行。这两条钉死它必须走鉴权。
    try:
        anon = TestClient(app, client=None)
        r = anon.get("/ping")
        check("来源为 None 时不免令牌 -> 401", r.status_code == 401, "got %s" % r.status_code)
        r = anon.get("/ping", params={"token": token})
        check("来源为 None 但带正确令牌 -> 200", r.status_code == 200, "got %s" % r.status_code)
    except TypeError as e:
        # 某些 starlette 版本不接受 client=None，退化为直接驱动 ASGI 应用
        print("  (TestClient 不支持 client=None，改为直接构造 scope: %s)" % repr(e)[:60])
        import asyncio

        async def _call(scope_extra: dict):
            sent = []
            scope = {"type": "http", "http_version": "1.1", "method": "GET",
                     "path": "/ping", "raw_path": b"/ping", "query_string": b"",
                     "root_path": "", "scheme": "http",
                     "headers": [(b"host", b"testserver")], "server": ("testserver", 80)}
            scope.update(scope_extra)

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(msg):
                sent.append(msg)

            await app(scope, receive, send)
            starts = [m for m in sent if m["type"] == "http.response.start"]
            return starts[0]["status"] if starts else 0

        st = asyncio.get_event_loop().run_until_complete(_call({}))
        check("scope 无 client 时不免令牌 -> 401", st == 401, "got %s" % st)
        st = asyncio.get_event_loop().run_until_complete(_call({"client": None}))
        check("client=None 时不免令牌 -> 401", st == 401, "got %s" % st)

    # 豁免集合本身不该再含空字符串
    check("_LOCAL_HOSTS 不含空字符串", "" not in ns["_LOCAL_HOSTS"],
          repr(sorted(ns["_LOCAL_HOSTS"])))

    print("\n结果：%s" % ("全部通过" if not FAILED else "失败 %d 项 -> %s" % (len(FAILED), FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
