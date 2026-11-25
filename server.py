"""server.py — Quillbox FastAPI 后端"""

import os, sys, json, asyncio, queue, threading, base64, re, uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, Response, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn, yaml

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# 必须在 import torch / sentence_transformers 之前设置。
# watchdog 以 CREATE_NO_WINDOW 拉起 server 并把 stdout 重定向进 svr.log；
# 这种无控制台 + 句柄被重定向的场景下，tqdm 写进度条会让 torch 加载嵌入模型
# 权重时死锁（卡在 "Loading weights: 100%"，CPU 归零、
# 长时间不绑定 :8765）。daemon.py 早已有同一组守护，server 此前漏了。
# 注：刻意不设 OMP_NUM_THREADS=1 —— server 要跑向量检索，单线程会拖慢查询。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import logging

from core.db_manager import DBManager
from core.vector_store import open_vector_store
from core.ai_engine import AIEngine, generation_failed

# server.py 原本没有 logger —— 草稿回写的 except 分支里写了 log.warning，
# 结果异常处理本身抛 NameError 并把 500 传回调用方。
log = logging.getLogger("quillbox.server")

# ── 初始化（启动时加载一次，不像 Streamlit 每次重跑）─────────────────
def _load_cfg():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)

def _resolve_api_key(api_cfg: dict) -> str:
    """优先读环境变量 QUILLBOX_API_KEY，回退到 config.yaml（兼容旧配置，避免明文长期驻留）。"""
    return os.environ.get("QUILLBOX_API_KEY") or (api_cfg or {}).get("api_key", "")

cfg      = _load_cfg()
_vdb     = cfg.get("vector_db", {})
_ol      = cfg.get("ollama", {})
_ai_cfg  = cfg.get("ai", {})
_llm     = cfg.get("llm", {})
_llm_api = _llm.get("api", {})

db = DBManager(str(ROOT / "data" / "emails.db"))
vs = open_vector_store(_vdb.get("path", str(ROOT / "data" / "chromadb")),
                 _vdb.get("embedding_model_path", "paraphrase-multilingual-MiniLM-L12-v2"))
ai = AIEngine(
    base_url    = _ol.get("base_url", "http://127.0.0.1:11434"),
    model       = _ol.get("model", "qwen2.5:7b"),
    provider    = _llm.get("provider", "ollama"),
    api_base_url= _llm_api.get("base_url", ""),
    api_key     = _resolve_api_key(_llm_api),
    api_model   = _llm_api.get("model", ""),
    api_timeout = _llm_api.get("timeout", 120),
    prompts     = cfg.get("prompts", {}),
    signature   = cfg.get("signature", ""),
)

from core.health_probe import BackgroundHealthProbe
_ai_health = BackgroundHealthProbe(ai.is_running)

SKIP_ACCOUNTS = _ai_cfg.get("skip_accounts", [])
SKIP_FOLDERS  = _ai_cfg.get("skip_folders", [])

app = FastAPI(title="Quillbox")
# CORS：只允许本机来源。
# 注意：服务监听 server.host（缺省 127.0.0.1，可配置为对外监听），
# 因此这里的 CORS 白名单不构成访问控制，真正的门禁是下面的 _AuthGate。
_ALLOWED_ORIGINS = ["http://127.0.0.1:8765", "http://localhost:8765"]
app.add_middleware(CORSMiddleware, allow_origins=_ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/frontend", StaticFiles(directory=ROOT / "frontend"), name="frontend")
# >>> QUILLBOX-AUTH-GATE BEGIN (token gate; loopback exempt) >>>
import hmac as _hmac
import secrets as _secrets

_TOKEN_FILE = ROOT / "data" / ".auth_token"
_COOKIE_NAME = "quillbox_token"
# 刻意不含空字符串：门禁取来源用 `((scope.get("client") or ("", 0))[0]) or ""`，
# scope 里没有 client 时会得到 ""，把它放进豁免集合等于"取不到来源即放行"。
# uvicorn 走 TCP 时 client 恒有值，所以这是纵深防御而非现实漏洞——
# 但换 transport 或中间件改写 scope 就会变成匿名放行，没有保留的理由。
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_AUTH_HINT = (
    "<!doctype html><meta charset=utf-8><title>401</title>"
    "<h3>401 - 需要访问令牌</h3>"
    "<p>本服务不对外开放匿名访问。请在地址后附加一次 <code>?token=&lt;令牌&gt;</code>，"
    "浏览器会记住 cookie（一年）。</p>"
    "<p>令牌位置：服务所在机器上的 <code>data/.auth_token</code>；"
    "也可用请求头 <code>X-Quillbox-Token</code>。本机 127.0.0.1 访问免令牌。</p>"
)


# 页面导航（Accept 含 text/html）时给登录表单：嵌在工作台 iframe 里也能直接登录，
# 且令牌走 POST 正文，不进地址栏、浏览器历史与反代访问日志（?token= 会）。
_AUTH_FORM = (
    "<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Quillbox · 需要访问令牌</title>"
    "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#161826;color:#e9e9ed;"
    "font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif}form{width:min(360px,90vw);padding:28px;border-radius:14px;"
    "background:#232532;box-shadow:0 0 0 1px #595d6c,0 16px 40px rgba(0,0,0,.5)}h1{font-size:18px;font-weight:500;margin:0 0 6px}"
    "p{margin:0 0 16px;color:#b2b6ca;font-size:13px}input{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;"
    "border:1px solid #3f424d;background:#161826;color:inherit;font:inherit}button{margin-top:12px;width:100%;padding:9px;"
    "border-radius:8px;border:1px solid #9184d9;background:transparent;color:#d2cefd;font:inherit;cursor:pointer}"
    ".e{color:#f5a3a3;margin-top:10px;font-size:12px}</style>"
    "<form method=post action='/auth/login'><h1>Quillbox</h1><p>请输入访问令牌（服务所在机器上的 <code>data/.auth_token</code>）。"
    "浏览器会记住一年。</p><input type=password name=token autocomplete=current-password autofocus required "
    "aria-label=访问令牌><input type=hidden name=next value='__NEXT__'><button type=submit>进入</button>__ERR__</form></html>"
)


def _auth_headers() -> list:
    """登录页 / 401 的安全头。之前缺 frame-ancestors：任何站点都能把登录页嵌进去诱导输入令牌。
    只允许自身与 embed.allowed_origins 嵌入（与正常页面同一口径）；页面只有内联样式和一个同源表单，其余一律禁止。"""
    try:
        extra = _embed_origins()
    except NameError:          # 门禁代码被单独加载时（test_auth_gate）没有白名单函数：只允许自身
        extra = []
    fa = " ".join(["'self'"] + extra)
    csp = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors " + fa)
    return [(b"content-security-policy", csp.encode("latin-1")), (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer")]


def _safe_next(v: str) -> str:
    """只接受站内相对路径，防开放重定向。"""
    v = (v or "").strip()
    if not v.startswith("/") or v.startswith("//") or "\\" in v or any(c in v for c in "\r\n"):
        return "/app"
    return v


def _auth_cookie(scope) -> bytes:
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in (scope.get("headers") or [])}
    secure = "; Secure" if (h.get("x-forwarded-proto") or "").lower() == "https" else ""
    return (_COOKIE_NAME + "=" + UI_TOKEN + "; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax" + secure).encode("latin-1")


def _load_ui_token() -> str:
    tok = (os.environ.get("QUILLBOX_UI_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        if _TOKEN_FILE.is_file():
            tok = _TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        tok = ""
    if not tok:
        tok = _secrets.token_hex(16)
        try:
            _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _tmp = _TOKEN_FILE.with_name(_TOKEN_FILE.name + ".tmp")
            _tmp.write_text(tok + "\n", encoding="utf-8")
            os.replace(_tmp, _TOKEN_FILE)
        except Exception:
            pass
    return tok


UI_TOKEN = _load_ui_token()


def _scope_token(scope) -> tuple:
    """返回 (令牌, 是否来自查询串)。"""
    from urllib.parse import parse_qs

    headers = {}
    for k, v in scope.get("headers") or []:
        headers[k.decode("latin-1").lower()] = v.decode("latin-1")
    tok = (headers.get("x-quillbox-token") or "").strip()
    if tok:
        return tok, False
    for part in (headers.get("cookie") or "").split(";"):
        if "=" in part:
            name, val = part.split("=", 1)
            if name.strip() == _COOKIE_NAME:
                return val.strip(), False
    qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    vals = qs.get("token") or []
    return ((vals[0] or "").strip(), True) if vals else ("", False)


def _host_of(value: str) -> str:
    """从 Host 头或 URL 取小写主机名；解析失败返回空串。"""  # AUTHGATE-LOOPBACK-HARDEN-20260930
    from urllib.parse import urlsplit
    v = (value or "").strip()
    if not v or v.lower() == "null":
        return ""
    if "://" not in v:
        v = "http://" + v
    try:
        return (urlsplit(v).hostname or "").lower()
    except ValueError:
        return ""


def _loopback_trusted(scope) -> bool:
    """loopback 来源也要满足：Host 为本机、Origin/Referer（若有）为本机、非跨站 fetch。
    防 DNS rebinding（Host 被换成攻击者域名）与跨站 CSRF（Origin 为外站）。"""
    h = {}
    for k, v in scope.get("headers") or []:
        h[k.decode("latin-1").lower()] = v.decode("latin-1")
    if _host_of(h.get("host", "")) not in _LOCAL_HOSTS:
        return False
    # 经反向代理转来的请求（例如嵌入门户时：浏览器 → 反向代理 → 隧道 → 本机）在 TCP 层也是 loopback。
    # 本页又发 Referrer-Policy: no-referrer，同源 GET 既无 Origin 也无 Referer——若代理把 Host 写成 127.0.0.1，
    # 这类请求会被误判为「本机可信」而绕过令牌。带任何转发头一律按远程处理。
    if any(k in h for k in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip")):
        return False
    for key in ("origin", "referer"):
        if key in h and _host_of(h[key]) not in _LOCAL_HOSTS:
            return False
    sfs = (h.get("sec-fetch-site") or "").lower()
    if sfs and sfs not in ("same-origin", "none"):
        return False
    return True


class _AuthGate:
    """远程来源令牌门禁；loopback 免认证。纯 ASGI，保持 SSE 流式语义。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        # 登录端点先于 loopback 豁免处理：表单从 401 页同源提交，会被判为本机可信而直接落到路由层（404）
        if scope.get("path") == "/auth/login" and scope.get("method") == "POST":
            return await self._login(scope, receive, send)
        client = ((scope.get("client") or ("", 0))[0]) or ""
        if client in _LOCAL_HOSTS and _loopback_trusted(scope):  # AUTHGATE-LOOPBACK-HARDEN-20260930
            return await self.app(scope, receive, send)
        supplied, from_query = _scope_token(scope)
        if not (supplied and _hmac.compare_digest(supplied, UI_TOKEN)):
            return await self._deny(scope, send)
        if not from_query:
            return await self.app(scope, receive, send)

        _cookie = _auth_cookie(scope)

        async def _send_with_cookie(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers") or []) + [(b"set-cookie", _cookie)]
            await send(message)

        await self.app(scope, receive, _send_with_cookie)

    @staticmethod
    def _wants_html(scope) -> bool:
        for k, v in scope.get("headers") or []:
            if k.decode("latin-1").lower() == "accept":
                return "text/html" in v.decode("latin-1").lower()
        return False

    async def _deny(self, scope, send, err: str = ""):
        import html as _h
        if scope.get("method") == "GET" and self._wants_html(scope):
            qs = scope.get("query_string", b"").decode("latin-1")
            nxt = _safe_next(scope.get("path", "/app") + (("?" + qs) if qs else ""))
            body = _AUTH_FORM.replace("__NEXT__", _h.escape(nxt, quote=True)).replace("__ERR__", err).encode("utf-8")
        else:
            body = _AUTH_HINT.encode("utf-8")
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode("latin-1")),
            (b"cache-control", b"no-store")] + _auth_headers()})
        await send({"type": "http.response.body", "body": body})

    async def _login(self, scope, receive, send):
        """POST /auth/login（application/x-www-form-urlencoded：token, next）。正文上限 4KB。"""
        import html as _h
        from urllib.parse import parse_qs
        body, more = b"", True
        while more and len(body) <= 4096:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        form = parse_qs(body[:4096].decode("utf-8", "replace"))
        tok = ((form.get("token") or [""])[0]).strip()
        nxt = _safe_next((form.get("next") or ["/app"])[0])
        if not (tok and _hmac.compare_digest(tok, UI_TOKEN)):
            await asyncio.sleep(0.5)   # 放慢在线猜测
            body = _AUTH_FORM.replace("__NEXT__", _h.escape(nxt, quote=True)).replace("__ERR__", "<div class=e>令牌不正确</div>").encode("utf-8")
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode("latin-1")), (b"cache-control", b"no-store")] + _auth_headers()})
            await send({"type": "http.response.body", "body": body})
            return
        await send({"type": "http.response.start", "status": 303, "headers": [
            (b"location", nxt.encode("utf-8")), (b"set-cookie", _auth_cookie(scope)), (b"cache-control", b"no-store"), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})


app.add_middleware(_AuthGate)
# <<< QUILLBOX-AUTH-GATE END <<<


# >>> QUILLBOX-TRANSFER-OPT >>>
class _VersionedAssetCache:
    """带 ?v= 的 /frontend 资源（版本号随发布变化）给长缓存；只对 200 生效，401/404 不会被缓存。纯 ASGI，不碰响应体。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/frontend/") \
                or not any(p.startswith(b"v=") for p in (scope.get("query_string") or b"").split(b"&")):
            return await self.app(scope, receive, send)

        async def _send(msg):
            if msg.get("type") == "http.response.start" and msg.get("status") == 200:
                hs = [(k, v) for k, v in (msg.get("headers") or []) if k.lower() != b"cache-control"]
                hs.append((b"cache-control", b"public, max-age=31536000, immutable"))
                msg = dict(msg, headers=hs)
            await send(msg)
        return await self.app(scope, receive, _send)


# 压缩放在最外层：401 登录页、静态资源、JSON 都能压。Starlette 1.0 的 GZipMiddleware 已内置
# 跳过 text/event-stream（SSE 逐块直通，不缓冲）；只在客户端声明 Accept-Encoding: gzip 时生效，
# daemon / mcp_server 用 urllib（不声明）拿到的仍是原文。
from starlette.middleware.gzip import GZipMiddleware as _GZip
app.add_middleware(_VersionedAssetCache)
app.add_middleware(_GZip, minimum_size=1024, compresslevel=6)
# <<< QUILLBOX-TRANSFER-OPT <<<


@app.on_event("startup")
async def _quiet_conn_reset():
    """吞掉客户端断开 SSE 流时的 ConnectionResetError 噪声（WinError 10054）。"""
    _loop = asyncio.get_event_loop()
    _orig = _loop.get_exception_handler()
    def _handler(loop, context):
        if isinstance(context.get("exception"), ConnectionResetError):
            return
        (_orig or loop.default_exception_handler)(context)
    _loop.set_exception_handler(_handler)

# ── SSE 工具 ──────────────────────────────────────────────────────────

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def _bridge(gen_fn, *args, **kwargs):
    """把同步流式生成器通过队列桥到 async，返回 (queue, thread)"""
    q = queue.Queue()
    def _worker():
        try:
            for chunk in gen_fn(*args, **kwargs):
                q.put(("chunk", chunk))
        except Exception as e:
            q.put(("error", str(e)))
        finally:
            q.put(("done", None))
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return q, t

async def _drain(q, t):
    """从桥接队列中逐个读取，yield SSE 字符串"""
    loop = asyncio.get_event_loop()
    while True:
        kind, val = await loop.run_in_executor(None, q.get)
        if kind == "chunk":
            yield _sse({"text": val})
        elif kind == "error":
            yield _sse({"error": val, "done": True})
            break
        elif kind == "done":
            yield _sse({"done": True})
            break
    t.join(timeout=2)

# ── 前端入口 ──────────────────────────────────────────────────────────

from fastapi import Request as _IdxReq


@app.get("/", response_class=RedirectResponse)
async def index(request: _IdxReq):
    # 保留查询串：/?embed=1、/?view=inbox 之前会在跳转时丢掉
    q = request.url.query
    return RedirectResponse(url="/app" + ("?" + q if q else ""), status_code=302)


@app.get("/favicon.ico")
def favicon():
    """浏览器每次加载都要 favicon，没有就是一条常驻的红色 404。

    static/ 下没有图标资源，不为此造二进制文件；回 204 语义正确
    （确实没有图标），且不再产生错误级日志把真正的报错淹掉。
    """
    return Response(status_code=204)


# SANITIZE-HARDEN-20260930：CSP 是净化器之外的第二道闸——即使有 HTML 漏过净化，connect-src 'self'
# 也让脚本无法把数据发往外站；object/base/form/frame 全部收死。
# 'unsafe-inline'/'unsafe-eval' 是 Alpine 标准构建与页内内联脚本所需，暂不能去。
_APP_CSP = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: cid: http: https:; font-src 'self' data:; connect-src 'self'; media-src 'self' blob: data:; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-src 'none'; frame-ancestors 'none'"


# ── 嵌入：被其他门户页面以 iframe 嵌入 ─────────────────────
# config.yaml：
#   embed:
#     allowed_origins: ["https://portal.example.com:8443"]   # 允许嵌入本页的父页面来源
# 未配置时只允许同源嵌入（frame-ancestors 'self'），与原来「不可被外站嵌入」等价。
# 同一份列表也是前端 postMessage 的收发白名单（注入到页面里），两处不会分叉。
_EMBED_CACHE = {"mtime": None, "origins": []}


def _valid_origin(o) -> str:
    from urllib.parse import urlsplit
    o = str(o or "").strip().rstrip("/")
    try:
        u = urlsplit(o)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname or u.path or u.query or u.fragment or "*" in o or u.username or u.password:
        return ""
    return "%s://%s" % (u.scheme, u.netloc.lower())


def _embed_origins() -> list:
    """读 config.embed.allowed_origins（按 mtime 缓存）；非法条目丢弃（含通配、带路径、null）。"""
    try:
        mt = (ROOT / "config.yaml").stat().st_mtime
    except OSError:
        mt = None
    if mt != _EMBED_CACHE["mtime"]:
        try:
            raw_list = ((_load_cfg().get("embed") or {}).get("allowed_origins")) or []
        except Exception:
            raw_list = []
        out = []
        for o in raw_list if isinstance(raw_list, list) else []:
            v = _valid_origin(o)
            if v and v not in out:
                out.append(v)
        _EMBED_CACHE.update(mtime=mt, origins=out)
    return list(_EMBED_CACHE["origins"])


def _page_csp() -> str:
    fa = " ".join(["'self'"] + _embed_origins())
    return _APP_CSP.replace("frame-ancestors 'none'", "frame-ancestors " + fa)


def _serve_page(name: str) -> HTMLResponse:
    content = (ROOT / "frontend" / name).read_text(encoding="utf-8")
    if "__QUILLBOX_EMBED_ORIGINS__" in content:
        import html as _html
        content = content.replace("__QUILLBOX_EMBED_ORIGINS__", _html.escape(" ".join(_embed_origins()), quote=True))
    if "__QUILLBOX_EMBED_SEND__" in content:
        # 嵌入门户时是否允许发送（默认允许；config.embed.allow_send: false 可关）。读不到配置按允许处理。
        try:
            _es = (_load_cfg().get("embed") or {}).get("allow_send", True) is not False
        except Exception:
            _es = True
        content = content.replace("__QUILLBOX_EMBED_SEND__", "on" if _es else "off")
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Content-Security-Policy": _page_csp(),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/app", response_class=HTMLResponse)
async def app_frontend():
    """默认入口 = 新界面。"""
    return _serve_page("app2.html")


@app.get("/app2", response_class=HTMLResponse)
async def app2_frontend():
    """新界面的旧书签别名，与 /app 相同。"""
    return _serve_page("app2.html")


@app.get("/classic", response_class=HTMLResponse)
async def classic_frontend():
    """经典界面（旧版 /app），保留作备用；同一份 CSP 与头。"""
    return _serve_page("index.html")


@app.post("/api/clientlog")
async def client_log(body: dict):
    """接收前端上报的错误/诊断事件，写入 client.log 供排查。"""
    from datetime import datetime
    try:
        line = f"{datetime.now().isoformat()} [{body.get('level', 'log')}] {str(body.get('msg', ''))[:2000]}\n"
        with open(ROOT / "client.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    return {"ok": True}

# ── 邮件 ──────────────────────────────────────────────────────────────

import re as _re
_SEARCH_OP_RE = _re.compile(r'\b(from|to|subject|body|has|before|after):("[^"]*"|[^\s]+)', _re.I)

def _parse_search(q: str):
    """解析搜索操作符，返回 (ops_dict, 剩余自由文本)。"""
    ops: dict = {}
    def _repl(m):
        ops.setdefault(m.group(1).lower(), []).append(m.group(2).strip('"'))
        return ' '
    free = _SEARCH_OP_RE.sub(_repl, q or '')
    return ops, ' '.join(free.split()).strip()


# ── 列表轻量模式：给 Agent/MCP 用，不回全量正文 ─────────────────────
_LIGHT_HEAD_CHARS = 200


def _to_light(payload: dict, head_chars: int = _LIGHT_HEAD_CHARS) -> dict:
    """去掉 body/html_body，补一个 body_head（压平空白后的前 N 字）。

    mcp_server 的 _summarize 优先取 body_head，因此这里给了它就不会再要正文。
    """
    out = []
    for e in (payload.get("emails") or []):
        e = dict(e)
        raw = e.get("body") or ""
        if not raw and e.get("html_body"):
            raw = _re2.sub(r"<[^>]+>", " ", e["html_body"])
        head = _re2.sub(r"\s+", " ", raw).strip()[:head_chars]
        e.pop("body", None)
        e.pop("html_body", None)
        if head:
            e["body_head"] = head
        out.append(e)
    payload = dict(payload)
    payload["emails"] = out
    return payload


def _own_domains() -> set:
    """本组织邮件域名：mailbox_labels 规则里的 domains + 各账户自身域名。

    用于「外部发件人」判定（新界面筛选与提示）。取不到配置时退回只用账户域名。
    """
    doms = set()
    try:
        from core import mailbox_labels as _ml
        for r in _ml.load_rules(_load_cfg()) or []:
            for d in (r.get("domains") or []):
                if d:
                    doms.add(str(d).strip().lower())
    except Exception:
        pass
    try:
        for a in db.get_accounts() or []:
            a = str(a or "").lower()
            if "@" in a:
                doms.add(a.split("@", 1)[1])
    except Exception:
        pass
    return doms


def _is_own_sender(addr, own: set) -> bool:
    a = str(addr or "").strip().lower()
    if "@" not in a:
        return True          # 无地址（系统/退信）不算外部，避免误报
    d = a.rsplit("@", 1)[1]
    return any(d == x or d.endswith("." + x) for x in own)


def _maybe_light(payload: dict, light) -> dict:
    """light 为真时转成轻量投影；默认原样返回，保证既有调用方零影响。"""
    _attach_ai_priority(payload.get("emails") if isinstance(payload, dict) else None)
    try:
        on = bool(int(light))
    except (TypeError, ValueError):
        on = False
    return _to_light(payload) if on else payload


@app.get("/api/emails")
def list_emails(
    view: str = "inbox",
    account: Optional[str] = None,
    mailbox: Optional[str] = None,
    folder: Optional[str] = None,
    filter: str = "all",
    page: int = 0,
    page_size: int = 30,
    q: Optional[str] = None,
    tag: Optional[str] = None,
    light: int = 0,
):
    if mailbox and not account:
        # 按邮箱来源标签筛选：标签→实际账户列表（标签本身就是从 account 派生的，这里是反查）
        from core import mailbox_labels as _ml
        _rules = _ml.load_rules(_load_cfg())
        _hit = [a for a in db.get_accounts()
                if (_ml.resolve(a, _rules) or {}).get("name") == mailbox]
        # 命中为空时给一个不存在的账户，避免退化成"不过滤"（那会误导用户）
        account = _hit or ["__no_such_account__"]

    if q:
        _ops, _free = _parse_search(q)
        if _ops:
            hits = db.search_advanced(_ops, _free, limit=100)
            return _maybe_light({"emails": hits, "total": len(hits), "has_more": False}, light)
        # 无操作符：FTS5 关键词搜索（毫秒级）+ 不足时补向量语义
        kw_hits = db.search_by_keyword(q, limit=50)
        try:   # 处理记录（完成说明）也参与搜索：命中的邮件补在后面
            _have = {e["id"] for e in kw_hits}
            for _nid in db.search_notes(q, limit=30):
                if _nid not in _have:
                    _e = db.get_email_by_id(_nid)
                    if _e and not _e.get("deleted"):
                        _e["note_hit"] = True; kw_hits.append(_e); _have.add(_nid)
        except Exception:
            pass

        # 关键词结果已经够用（≥10条），直接返回，跳过向量搜索（省时间）
        if len(kw_hits) >= 10:
            return _maybe_light({"emails": kw_hits, "total": len(kw_hits), "has_more": False}, light)

        # 关键词结果不足，补充向量语义搜索
        vs_hits   = vs.search(q, n_results=15)
        seen      = {e["id"] for e in kw_hits}
        extra_ids = [r["id"] for r in vs_hits if r["id"] not in seen and r.get("score", 0) > 0.3]
        extra     = [e for eid in extra_ids if (e := db.get_email_by_id(eid))]
        for _e in extra:
            _e["semantic"] = True     # 关键词没命中、按语义相近补充的：界面如实标注（P2-08）
        emails    = kw_hits + extra
        return _maybe_light({"emails": emails, "total": len(emails), "has_more": False}, light)

    filters: dict = {}
    if view == "starred":  filters["starred"] = True
    elif view == "unread": filters["unread_only"] = True
    elif view == "trash":  filters["deleted_only"] = True
    elif view == "archived": filters["archived"] = True
    if account: filters["account"] = account
    if folder:  filters["folder"]  = folder
    # 未读视图与收件箱视图必须同口径：收件箱列表排除已归档，未读视图若把归档邮件
    # 算进来，侧边栏徽标就会出现"点不掉的未读"（未读都点完了，
    # 收件箱仍显示数字）。归档里的未读从"归档"视图看。
    if view in ("inbox", "unread") and not account and not folder:
        filters["archived"] = False
    if tag:
        filters["tag"] = tag

    # 顶部筛选栏（filter 参数）与视图叠加。前端发送的值为 all/unread/starred/attachments。
    # unread/starred 下推到 DB 查询，保证分页与计数准确；附件为页内过滤。
    if filter == "unread":    filters["unread_only"] = True
    elif filter == "starred": filters["starred"] = True

    _ex = None
    if view in ("inbox", "unread") and not folder and not account:
        _m = _muted_ids()
        if _m:
            _ex = _m
    if view == "inbox" and not folder and not tag and filter == "all":
        try:
            _hid = _series_hidden_ids()      # 同一系列只保留最新一期，往期在右侧「系列」里看
            if _hid:
                _ex = list(set(_ex or []) | set(_hid))
        except Exception:
            pass
    emails = db.get_emails_paginated(page=page, page_size=page_size, exclude_ids=_ex, **filters)
    # has_more 必须基于「本页原始条数是否满页」判断，而非页内过滤后的条数，
    # 否则 attachments/action/notify 等页内过滤会误判为没有下一页，导致翻页中断。
    raw_len = len(emails)

    if filter in ("attachments", "attach"):
        emails = [e for e in emails if e.get("has_attachment")]
    elif filter == "action":
        # 「待处理」= AI 分析判为 act/reply；尚未分析的邮件退回分类口径（🚨重要行动）。
        # AI 判断优先于分类：分类是同步时的粗分，分析结果更贴近「要不要我动手」。
        _pp = db.get_insight_priorities([e.get("id") for e in emails])
        def _needs_me(e):
            if _is_no_action(e):
                return False
            p = _pp.get(e.get("id"))
            return p in ("act", "reply") if p else ("重要行动" in (e.get("category") or ""))
        emails = [e for e in emails if _needs_me(e)]
    elif filter == "notify":
        emails = [e for e in emails if "通知" in (e.get("category") or "")]
    elif filter == "external":
        # 新界面「外部」筛选：发件人域名不在本组织域名内（口径与前端外部标识同源）
        _own = _own_domains()
        emails = [e for e in emails if not _is_own_sender(e.get("sender_email"), _own)]

    # total 按当前视图/账户/文件夹过滤条件统计（而非全库 count），使分页准确
    return _maybe_light({"emails": emails,
                         "total": db.count_filtered(exclude_ids=_ex, **filters),
                         "has_more": raw_len == page_size}, light)


@app.get("/api/emails/{email_id}")
def get_email(email_id: str):
    return db.get_email_by_id(email_id) or {}


@app.post("/api/vector/delete")
def vector_delete(body: dict):
    """从向量索引里摘除若干 id（幂等；不存在的 id 忽略）。

    chroma 的持久化目录由本进程独占，外部进程直接打开有并发写风险，
    因此删除统一走这个接口。
    """
    ids = [str(i) for i in (body or {}).get("ids") or [] if str(i).strip()]
    if not ids:
        return {"ok": False, "error": "缺少 ids"}
    try:
        col = getattr(vs, "collection", None)
        if col is None:
            return {"ok": False, "error": "向量库未就绪"}
        before = vs.count()
        col.delete(ids=ids)
        after = vs.count()
        return {"ok": True, "requested": len(ids), "removed": max(0, before - after),
                "count_before": before, "count_after": after}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/mailbox-labels")
def get_mailbox_labels():
    """邮箱来源标签规则 + 可选配色。配置缺失时返回种子规则（不写盘）。"""
    from core import mailbox_labels as _ml
    c = _load_cfg()
    return {"rules": _ml.load_rules(c), "palette": _ml.PALETTE}


@app.post("/api/mailbox-labels")
def save_mailbox_labels(body: dict):
    """保存标签规则。校验不通过时整批拒绝，不做部分保存。"""
    from core import mailbox_labels as _ml
    rules, errors = _ml.validate_rules((body or {}).get("rules"))
    if errors:
        return {"ok": False, "errors": errors}
    c = _load_cfg()
    c["mailbox_labels"] = {"rules": rules}
    _save_cfg(c)
    return {"ok": True, "rules": rules}


@app.post("/api/vector/add")
def vector_add(body: dict):
    """供 daemon 调用：把邮件加入向量索引（server 持有嵌入模型）。upsert 幂等。"""
    if not body or not body.get("id"):
        return {"ok": False, "error": "缺少邮件 id"}
    try:
        vs.add_email(body)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# 邮件内嵌图片（cid 图片提取后保存在 data/inline/{email_id}/{filename}）
_INLINE_DIR = (ROOT / "data" / "inline").resolve()


@app.get("/api/inline/{email_id}/{filename}")
def get_inline_image(email_id: str, filename: str):
    """提供邮件内嵌图片。做路径穿越防护，仅允许 data/inline 目录下的文件。"""
    from pathlib import Path as _P
    # 只取纯文件名/账户段，剥离任何路径分隔符
    safe_id = _P(email_id).name
    safe_name = _P(filename).name
    target = (_INLINE_DIR / safe_id / safe_name).resolve()
    try:
        if _INLINE_DIR not in target.parents:
            return Response(status_code=403)
    except Exception:
        return Response(status_code=404)
    if target.is_file():
        import mimetypes as _mt
        mime = _mt.guess_type(target.name)[0] or ""
        # SANITIZE-HARDEN-20260930：文件名来自邮件（攻击者可控），被当页面直接打开也不得执行脚本
        hdr = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"}
        if not mime.startswith("image/"):
            return FileResponse(str(target), media_type="application/octet-stream",
                                headers=dict(hdr, **{"Content-Disposition": "attachment"}))
        return FileResponse(str(target), media_type=mime, headers=hdr)
    return Response(status_code=404)


def _known_folders() -> set:
    """可作为移动目标的文件夹集合：库里已有的 + config 里自建的。"""
    names = set()
    try:
        names |= {f for f in db.get_folders() if f}
    except Exception:
        pass
    try:
        uf = (_load_cfg().get("user_folders") or {})
        for lst in uf.values():
            names |= {f for f in (lst or []) if f}
    except Exception:
        pass
    return names


@app.patch("/api/emails/{email_id}")
async def patch_email(email_id: str, body: dict):
    if "starred" in body: db.star_email(email_id, body["starred"])
    if "unread"  in body: db.mark_read(email_id, not body["unread"])
    if "deleted" in body: db.soft_delete(email_id, body["deleted"])
    if "tags" in body: db.set_tags(email_id, body["tags"])
    if "archived" in body: db.set_archived(email_id, body["archived"])
    if "folder" in body:
        # 移动到文件夹。必须校验：前端的文件夹列表是从 folder 列 distinct 派生的，
        # 名字打错会造出一个无法删除的"幽灵文件夹"。
        target = (body.get("folder") or "").strip()
        if not target:
            return {"ok": False, "error": "folder 不能为空"}
        known = _known_folders()
        if target not in known:
            return {"ok": False, "error": "未知文件夹：%s" % target,
                    "known_folders": sorted(known)}
        db.move_folder(email_id, target)
    return {"ok": True}

# ── 账户 / 统计 ───────────────────────────────────────────────────────

def _account_sort_key(name: str, order=None) -> int:
    """账户排序：按 config 的 account_order 排。

    列表项是完整地址，或以 @ 结尾的前缀（如 "noc@"）；"*" 代表其余账户的位置，缺省其余排在最后。
    例：account_order: [me@example.com, "*", "noc@"] → 自己的邮箱在前、告警邮箱在最后。
    """
    if order is None:
        order = _load_cfg().get("account_order") or []
    order = [str(x).strip().lower() for x in order]
    rest = order.index("*") if "*" in order else len(order)
    n = (name or "").strip().lower()
    for i, pat in enumerate(order):
        if pat != "*" and (n == pat or (pat.endswith("@") and n.startswith(pat))):
            return i
    return rest

@app.get("/api/accounts")
def get_accounts():
    _order = _load_cfg().get("account_order") or []
    names = sorted(db.get_accounts(), key=lambda n: _account_sort_key(n, _order))
    _uf = (_load_cfg().get("user_folders") or {})
    result = []
    for name in names:
        folders = list(db.get_folders(name))
        for f in (_uf.get(name) or []):
            if f not in folders:
                folders.append(f)
        result.append({
            "name": name,
            "folders": folders,
            "unread": db.get_unread_count_by_folder(name),
        })
    return result


# ── Quillbox 自定义文件夹 + 规则(本地) ──
@app.get("/api/user-folders")
def get_user_folders():
    return _load_cfg().get("user_folders") or {}


@app.post("/api/user-folders")
def add_user_folder(body: dict):
    account = (body.get("account") or "").strip()
    name = (body.get("name") or "").strip()
    if not account or not name:
        return {"ok": False, "error": "账户和文件夹名必填"}
    c = _load_cfg()
    uf = c.setdefault("user_folders", {})
    lst = uf.setdefault(account, [])
    if name not in lst:
        lst.append(name)
    _save_cfg(c)
    return {"ok": True, "user_folders": uf}


@app.post("/api/user-folders/delete")
def del_user_folder(body: dict):
    account = (body.get("account") or "").strip()
    name = (body.get("name") or "").strip()
    c = _load_cfg()
    uf = c.get("user_folders") or {}
    if account in uf:
        uf[account] = [x for x in uf[account] if x != name]
    c["user_folders"] = uf
    _save_cfg(c)
    return {"ok": True, "user_folders": uf}


@app.get("/api/emails/{email_id}/attachment")
def get_attachment(email_id: str, name: str = ""):
    """下载某封邮件的一个附件（按需回邮件服务器取，取到后缓存到本地）。

    边界：**这是 Web 面能力，不是 MCP 能力。** Quillbox 界面跑在本机、用户经 RDP
    操作，附件下到本机磁盘不构成外流；经 Agent 送出 Windows 是红线，
    MCP 工具清单里已移除 download_attachment（见 test_mcp_redlines.py）。
    """
    from fastapi.responses import Response
    from core import attachment_fetch as _af
    import urllib.parse as _up

    e = db.get_email_by_id(email_id)
    if not e:
        return {"ok": False, "error": "邮件不存在"}
    try:
        fname, data, mime = _af.fetch(_load_cfg(), e, name)
    except _af.AttachmentError as ex:
        # 刻意回 JSON 而不是空文件：给个 0 字节文件用户会以为下载成功了
        return {"ok": False, "error": str(ex)}
    except Exception as ex:                            # noqa: BLE001
        return {"ok": False, "error": "取附件失败：%s" % type(ex).__name__}

    # 中文文件名在纯 filename= 下会乱码，用 RFC 5987 的 filename*
    quoted = _up.quote(fname)
    return Response(
        content=data, media_type=mime,
        headers={"Content-Disposition":
                 "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                 % (quoted, quoted),
                 "Content-Length": str(len(data)),
                 "Cache-Control": "private, max-age=86400",
                 "X-Content-Type-Options": "nosniff",  # SANITIZE-HARDEN-20260930
                 "Content-Security-Policy": "default-src 'none'; sandbox"})


@app.get("/api/senders")
def list_senders():
    """可用于发信的账户列表（供撰写窗的发件人下拉）。

    只列取件侧已配置的账户 —— 发件人必须是其中之一，不接受任意地址。
    """
    accs = _sendable_accounts()
    out = []
    for a in accs:
        sc = _smtp_conf_for(a)
        out.append({"account": a,
                    "sender": (sc.get("sender") or a),
                    "host": sc.get("host") or "",
                    "ready": bool(sc.get("host"))})
    return {"senders": out, "default": accs[0] if accs else ""}


@app.get("/api/version")
def app_version():
    """返回当前前端资源版本号。

    不额外维护版本文件：版本号本来就由 _bump_bundle_ver.py 写进
    index.html 的 `bundle.js?v=XXX`，这里直接从磁盘读回来，
    少一个会漂的东西。前端拿它和自己的版本比，不同就提示刷新。
    """
    try:
        p = Path(__file__).resolve().parent / "frontend" / "index.html"
        import re as _rev   # server.py 顶层只有 re 的别名导入，这里局部导入避免 NameError
        txt = p.read_text(encoding="utf-8", errors="replace")
        m = _rev.search(r"bundle\.js\?v=([\w\.-]+)", txt)
        return {"version": m.group(1) if m else ""}
    except Exception as exc:                       # noqa: BLE001
        return {"version": "", "error": type(exc).__name__}


@app.get("/api/rules")
def list_rules():
    return (_load_cfg().get("prompts", {}) or {}).get("rules", []) or []


@app.post("/api/rules")
def add_rule(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "规则名必填"}
    c = _load_cfg()
    pr = c.setdefault("prompts", {})
    rules = [r for r in (pr.get("rules") or []) if r.get("name") != name]
    rules.append({"name": name, "enabled": True,
                  "match_mode": body.get("match_mode", "all"),
                  "conditions": body.get("conditions") or [],
                  "actions": body.get("actions") or {}})
    pr["rules"] = rules
    _save_cfg(c)
    globals()["cfg"] = c
    try: ai.prompts = pr
    except Exception: pass
    return {"ok": True, "rules": rules}


@app.delete("/api/rules/{name:path}")
def del_rule(name: str):
    c = _load_cfg()
    pr = c.setdefault("prompts", {})
    pr["rules"] = [r for r in (pr.get("rules") or []) if r.get("name") != name]
    _save_cfg(c)
    globals()["cfg"] = c
    try: ai.prompts = pr
    except Exception: pass
    return {"ok": True, "rules": pr["rules"]}


@app.post("/api/rules/apply")
def apply_rules_existing(body: dict = None):
    """把现有规则应用到已有邮件(移入文件夹/打分类/星标)。"""
    try:
        ai.prompts = (_load_cfg().get("prompts", {}) or {})
    except Exception:
        pass
    # 分页遍历全部邮件：原实现固定只取前 5000 封，库里早已超过该量，
    # 导致「应用规则」静默漏掉后面的邮件。
    PAGE = 1000
    page = 0
    scanned = matched = 0
    moved = categorized = starred = read_marked = 0
    no_action_tagged = 0
    while True:
        rows = db.get_emails_paginated(page=page, page_size=PAGE)
        if not rows:
            break
        for e in rows:
            scanned += 1
            try:
                if ai.match_no_action(e) and db.add_tag(e.get("id"), _NO_TAG):
                    no_action_tagged += 1
                rule = ai._match_rule(e)
                if not rule:
                    continue
                matched += 1
                acts = rule.get("actions") or {}
                eid = e.get("id")
                if not eid:
                    continue
                fld = (acts.get("folder") or "").strip()
                cat = (acts.get("category") or "").strip()
                if fld and e.get("folder") != fld:
                    db.move_folder(eid, fld)
                    moved += 1
                if cat:
                    db.bulk_update_category([eid], cat)
                    categorized += 1
                if acts.get("star"):
                    db.star_email(eid, True)
                    starred += 1
                if acts.get("mark_read"):
                    db.mark_read(eid, True)
                    read_marked += 1
                # 刻意不执行 skip_ai：它会把已有 AI 摘要覆盖成占位串，
                # 对历史邮件是破坏性的；该动作只在新邮件入库时生效。
            except Exception:
                pass
        if len(rows) < PAGE:
            break
        page += 1
    return {"ok": True, "applied": moved, "scanned": scanned,
            "matched": matched, "moved": moved, "categorized": categorized,
            "starred": starred, "marked_read": read_marked, "no_action_tagged": no_action_tagged}


# ── 给 Agent/MCP 工具面补齐的只读端点 ─────────────────────────────
def _contact_tally(tally: dict, name, addr, kind: str):
    """按邮箱地址归并联系人；同一地址出现多次时保留第一个非空显示名。"""
    addr = (addr or "").strip()
    if not addr or "@" not in addr:
        return
    key = addr.lower()
    name = (name or "").strip().strip("'\"")
    item = tally.get(key)
    if item is None:
        tally[key] = {"email": addr, "name": name, "count": 1,
                      "as_sender": 1 if kind == "from" else 0}
    else:
        item["count"] += 1
        if kind == "from":
            item["as_sender"] += 1
        if not item["name"] and name:
            item["name"] = name


@app.get("/api/folders")
def api_folders(account: str = ""):
    """列出文件夹及各自邮件数/未读数（MCP list_folders 用）。"""
    import sqlite3
    where, params = "deleted=0", []
    if account:
        where += " AND account=?"
        params.append(account)
    con = sqlite3.connect(db.db_path)
    try:
        rows = con.execute(
            "SELECT folder, COUNT(*), SUM(CASE WHEN unread=1 THEN 1 ELSE 0 END)"
            " FROM emails WHERE " + where + " GROUP BY folder ORDER BY 2 DESC",
            params).fetchall()
    finally:
        con.close()
    return [{"name": r[0] or "", "total": r[1], "unread": r[2] or 0}
            for r in rows]


@app.get("/api/contacts")
def api_contacts(q: str = "", limit: int = 20):
    """通讯录：从发件人与 recipients(to/cc) 聚合（MCP list_contacts 用）。"""
    import sqlite3
    limit = max(1, min(int(limit or 20), 100))
    tally: dict = {}
    con = sqlite3.connect(db.db_path)
    try:
        for name, addr in con.execute(
                "SELECT sender, sender_email FROM emails WHERE deleted=0"
                " AND sender_email IS NOT NULL AND TRIM(sender_email)<>''"):
            _contact_tally(tally, name, addr, "from")
        for (raw,) in con.execute(
                "SELECT recipients FROM emails WHERE deleted=0"
                " AND recipients IS NOT NULL"
                " AND TRIM(recipients) NOT IN ('', '[]', '{}')"):
            try:
                for r in json.loads(raw or "[]"):
                    if isinstance(r, dict):
                        _contact_tally(tally, r.get("name"), r.get("email"),
                                       (r.get("type") or "to"))
            except Exception:
                continue
    finally:
        con.close()
    items = sorted(tally.values(), key=lambda x: (-x["count"], x["email"]))
    kw = (q or "").strip().lower()
    if kw:
        items = [i for i in items
                 if kw in i["email"].lower() or kw in (i["name"] or "").lower()]
    return {"total": len(items), "contacts": items[:limit]}


@app.get("/api/emails/{email_id}/thread")
def api_thread(email_id: str):
    """同一规范化主题的往来，时间正序、仅摘要（MCP get_thread 用）。

    win 这套库没有 conversation_id / References 头，只能按规范化主题聚合，
    与 /api/topics 的口径保持一致（复用 _norm_subject）。
    """
    import sqlite3
    con = sqlite3.connect(db.db_path)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute("SELECT subject, account FROM emails WHERE id=?",
                          (email_id,)).fetchone()
        if not cur:
            return {"ok": False, "error": "邮件不存在"}
        norm = _norm_subject(cur["subject"])
        # 两段式：先在覆盖索引里按规范化主题筛出 id（约 5ms，不读整行），
        # 再只取命中的几封。原来一次取全账户七千多行摘要再过滤，85–140ms。
        _sql1 = (" FROM emails {} WHERE deleted=0 AND account=?")
        try:
            cand = con.execute("SELECT id, subject" + _sql1.format("INDEXED BY idx_thread_subj"),
                               (cur["account"],)).fetchall()
        except sqlite3.OperationalError:   # 索引尚未建（旧库首启前）时退回普通查询
            cand = con.execute("SELECT id, subject" + _sql1.format(""), (cur["account"],)).fetchall()
        ids = [r["id"] for r in cand if _norm_subject(r["subject"]) == norm]
        rows = []
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows += con.execute(
                "SELECT id, subject, sender, sender_email, time, received_timestamp,"
                " folder, account, unread, starred, has_attachment, summary"
                " FROM emails WHERE id IN (%s)" % ",".join("?" * len(part)), part).fetchall()
    finally:
        con.close()
    # 与原来一致：按 received_timestamp 正序（同值保持 rowid 顺序）
    msgs = sorted((dict(r) for r in rows), key=lambda m: (m["received_timestamp"] is not None, m["received_timestamp"] or 0))
    return {"subject": norm, "account": cur["account"],
            "count": len(msgs), "messages": msgs}


# ── 结构化 AI 分析（新界面 AI 侧栏）────────────────────────────────────
import hashlib as _hl
_INSIGHT_LOCKS: dict = {}
_INSIGHT_LOCKS_GUARD = threading.Lock()


def _insight_hash(e: dict) -> str:
    return _hl.sha1(((e.get("subject") or "") + "\x00" + (e.get("body") or e.get("html_body") or "")).encode("utf-8", "replace")).hexdigest()


def _insight_out(d: dict | None, e: dict | None) -> dict | None:
    if not d:
        return None
    ex = {k: d.get(k) or "" for k in ("due", "due_src", "next", "link", "headline", "meta")}
    ex["actions"] = d.get("actions") or []
    ex["v2"] = d.get("v2")
    ex["level"] = d.get("level") or ""
    try:
        ex["action_states"] = db.action_states(d.get("email_id") or "").get(d.get("email_id") or "", {})
    except Exception:
        ex["action_states"] = {}
    if _is_no_action(e):
        return {"priority": "fyi", "summary": d.get("summary") or "", "points": d.get("points") or [], "todos": [], "drafts": [],
                "created_at": d.get("created_at"), "no_action": True, "stale": False, "v2": None, "level": "",
                "due": "", "due_src": "", "next": "", "link": ex["link"], "headline": "", "meta": ex["meta"], "actions": []}
    return {"priority": d.get("priority"), "summary": d.get("summary") or "", "points": d.get("points") or [],
            "todos": d.get("todos") or [], "drafts": d.get("drafts") or [], "created_at": d.get("created_at"),
            "stale": bool(e is not None and d.get("body_hash") and d.get("body_hash") != _insight_hash(e)), **ex}


@app.get("/api/emails/{email_id}/insight")
def get_email_insight(email_id: str):
    """读缓存，不触发 AI。insight 为 null 表示尚未分析（前端再 POST 生成）。"""
    e = db.get_email_by_id(email_id)
    if not e:
        return {"ok": False, "error": "邮件不存在"}
    return {"ok": True, "insight": _insight_out(db.get_insight(email_id), e)}


@app.post("/api/emails/{email_id}/insight")
def make_email_insight(email_id: str, refresh: int = 0):
    """生成（或在缓存有效时直接返回）结构化分析。同一封邮件并发请求只调一次 AI。

    同步 def：FastAPI 放线程池执行，不阻塞事件循环。生成失败返回 ok=False 且不写缓存，
    避免像旧 analyze_email 那样把"AI 分析失败"永久落库。
    """
    e = db.get_email_by_id(email_id)
    if not e:
        return {"ok": False, "error": "邮件不存在"}
    with _INSIGHT_LOCKS_GUARD:
        lk = _INSIGHT_LOCKS.setdefault(email_id, threading.Lock())
    with lk:
        cur = db.get_insight(email_id)
        h = _insight_hash(e)
        if cur and not refresh and cur.get("body_hash") == h:
            return {"ok": True, "insight": _insight_out(cur, e), "cached": True}
        r = ai.analyze_insight(e)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "generation_failed"}
        r.pop("ok", None)
        r = _apply_correction(e, _calibrate_priority(e, r))
        db.save_insight(email_id, r, body_hash=h, model=str(getattr(ai, "api_model", "") or getattr(ai, "ollama_model", "") or ""))
        with _INSIGHT_LOCKS_GUARD:
            _INSIGHT_LOCKS.pop(email_id, None)
        return {"ok": True, "insight": _insight_out(db.get_insight(email_id), e), "cached": False}


# ── 今日分拣────────────────────────────────────────
# 范围：最近 days 天、未归档、不在已发送/草稿/垃圾/已删除；外加更早但仍有未完成待办、且 AI 判为 act/reply 的。
# 分组：act 需要你处理（截止日升序）/ reply 等你回复（等得久的在前）/ notice 告警与通知（同发件人 + 同告警对象折叠）/
#       other 其他（fyi、无需处理）/ pending 尚未分析（可分析但还没有 insight）。跳过 AI 名单内的邮件直接算通知，不进「尚未分析」。
_TRI_SKIP_FOLDERS = {"已发送邮件", "已发送", "Sent", "Sent Items", "Sent Messages", "草稿", "草稿箱", "Drafts",
                     "垃圾邮件", "Junk", "Junk Email", "已删除邮件", "Deleted Items", "Trash"}
import re as _re_tri
_ALERT_OFF = _re_tri.compile(r"RESOLVED|RECOVER(?:ED|Y)?|恢复|已解决|已消除|\bOK\b", _re_tri.I)
_ALERT_ON = _re_tri.compile(r"PROBLEM|ALARM|FIRING|CRITICAL|WARNING|告警|报警|故障|异常|触发|宕机|\bDOWN\b", _re_tri.I)


def _alert_state(subject) -> str:
    s = str(subject or "")
    if _ALERT_OFF.search(s):        # 「告警恢复」同时含两类词，以恢复为准
        return "resolved"
    if _ALERT_ON.search(s):
        return "alerting"
    return ""


def _alert_key(e: dict) -> str:
    from core.ai_engine import AIEngine as _AE
    p = _AE.subject_pattern(e.get("subject") or "")
    p = _ALERT_OFF.sub(" ", _ALERT_ON.sub(" ", p))
    p = _re_tri.sub(r"[\[\]【】()（）:：,，.。|/\\\-_#]+", " ", p)
    return (e.get("sender_email") or "").lower() + "|" + " ".join(p.split())


@app.get("/api/triage")
def triage(days: int = 7, account: Optional[str] = None):
    import time as _t
    days = max(1, min(int(days or 7), 30))
    cutoff = _t.time() - days * 86400
    cfg_ai = (_load_cfg().get("ai") or {})
    sk_acc = set(cfg_ai.get("skip_accounts") or [])
    sk_fold = [str(x) for x in (cfg_ai.get("skip_folders") or []) if x]
    sk_snd = [str(x).lower() for x in (cfg_ai.get("skip_sender_patterns") or []) if x]
    muted = None if account else (_muted_ids() or None)
    rows, seen = [], set()
    for page in range(0, 25):
        batch = db.get_emails_paginated(page=page, page_size=200, archived=False, account=account or None, exclude_ids=muted)
        stop = False
        for e in batch:
            if (e.get("received_timestamp") or 0) < cutoff:
                stop = True
                break
            if (e.get("folder") or "") in _TRI_SKIP_FOLDERS:
                continue
            rows.append(e); seen.add(e["id"])
        if stop or len(batch) < 200:
            break
    todo_by = {}
    for t in db.get_todos(limit=5000):
        if t.get("status") != "done" and t.get("source_email_id"):
            todo_by.setdefault(t["source_email_id"], []).append(t["id"])
    old_ids = [i for i in todo_by if i not in seen]
    old_pri = db.get_insight_priorities(old_ids)
    for i in old_ids:
        if old_pri.get(i) not in ("act", "reply"):
            continue
        e = db.get_email_by_id(i)
        if not e or e.get("deleted") or e.get("archived") or (account and e.get("account") != account):
            continue
        if (e.get("folder") or "") in _TRI_SKIP_FOLDERS or (muted and i in muted):
            continue
        e["_old"] = True; rows.append(e); seen.add(i)
    # 稍后提醒：未到期的从分拣里拿掉；最近 3 天内到期的即使超出时间窗也拉进来，并标 reminded
    snz_all = db.get_snoozes()
    now = _t.time()
    for i, u in snz_all.items():
        if i in seen or not (now - _SNOOZE_REMIND_WINDOW < u <= now):
            continue
        e = db.get_email_by_id(i)
        if e and not e.get("deleted") and not e.get("archived") and (not account or e.get("account") == account) \
                and (e.get("folder") or "") not in _TRI_SKIP_FOLDERS:
            rows.append(e); seen.add(i)
    snoozed_n = 0
    keep = []
    for e in rows:
        u = snz_all.get(e["id"])
        if u and u > now:
            snoozed_n += 1; seen.discard(e["id"]); continue
        keep.append(e)
    rows = keep
    mutes = db.list_mutes(active_only=True)
    mute_obj = {m["key"]: m for m in mutes if m["scope"] == "object"}
    mute_snd = {m["key"]: m for m in mutes if m["scope"] == "sender"}
    try:
        _si = _series_index()["by_id"]
    except Exception:
        _si = {}
    brief = db.get_insights_brief(list(seen))
    _ast = db.action_states(list(seen))
    _todo_txt = {}
    for t in db.get_todos(limit=5000):
        if t.get("source_email_id"):
            _todo_txt.setdefault(t["source_email_id"], set()).add(db.akey(t.get("content")))
    light = _to_light({"emails": rows})["emails"]
    sec = {"act": [], "reply": [], "watch": [], "notice": [], "other": [], "pending": []}
    out_rows, analyzable, analyzed = [], 0, 0
    for x in light:
        is_old = bool(x.pop("_old", False))
        e_id = x["id"]; b = brief.get(e_id)
        noact = _is_no_action(x)
        snd = (x.get("sender_email") or "").lower()
        skip = (x.get("account") in sk_acc or any(f in (x.get("folder") or "") for f in sk_fold)
                or any(p in snd for p in sk_snd))
        if noact:
            g = "other"
        elif b and b.get("priority"):
            g = b["priority"] if b["priority"] in ("act", "reply", "notice") else "other"
            if g == "notice" and b.get("level") == "watch":
                g = "watch"          # insight.v2：不需回复、但涉及你负责范围的变更 / 风险 / 截止日
            _acts = b.get("actions") or []
            if g in ("act", "reply") and _acts:
                _st = _ast.get(e_id, {}); _have = _todo_txt.get(e_id, set())
                _left = [a for a in _acts if db.akey(a.get("text")) not in _have and _st.get(db.akey(a.get("text"))) != "not_mine"]
                x["acts_total"] = len(_acts); x["acts_left"] = len(_left)
                if not _left:
                    g = "other"; x["acts_done"] = True       # 行动项都处理了（加入待办或不是我的）→ 移出「需要你处理」
        elif skip:
            g = "notice"
        else:
            g = "pending"
        can = not noact and not skip
        analyzable += 1 if can else 0
        analyzed += 1 if (can and b) else 0
        x.update(group=g, analyzable=can, open_todos=todo_by.get(e_id, []), old=is_old)
        if b:
            x.update(ai_priority="fyi" if noact else b.get("priority"), ai_summary=b.get("summary") or "",
                     due=b.get("due") or "", due_src=b.get("due_src") or "", next=b.get("next") or "", link=b.get("link") or "")
        u = snz_all.get(e_id)
        if u and now - _SNOOZE_REMIND_WINDOW < u <= now:
            x["reminded"] = u
        s_ = _si.get(e_id) if isinstance(_si, dict) else None
        if s_:
            x["series_key"] = s_["key"]; x["series_n"] = s_["n"]; x["series_idx"] = s_["idx"]
        if g == "notice":
            x["alert_state"] = _alert_state(x.get("subject")); x["alert_key"] = _alert_key(x)
            mm = mute_obj.get(x["alert_key"]) or mute_snd.get((x.get("sender_email") or "").lower())
            if mm:
                x["mute_id"] = mm["id"]; x["mute_until"] = mm["until_ts"]; x["mute_scope"] = mm["scope"]
        sec[g].append(x); out_rows.append(x)
    ts = lambda r: r.get("received_timestamp") or 0
    # 系列折叠（§10）：同一系列只留最新一期。「告警与通知」组不参与（它有自己的告警折叠）
    _latest = {}
    for k in ("act", "reply", "watch", "other", "pending"):
        for r in sec[k]:
            sk = r.get("series_key")
            if sk and (sk not in _latest or ts(r) > ts(_latest[sk])):
                _latest[sk] = r
    _drop = {r["id"] for k in ("act", "reply", "watch", "other", "pending") for r in sec[k]
             if r.get("series_key") in _latest and _latest[r["series_key"]] is not r}
    if _drop:
        for k in ("act", "reply", "watch", "other", "pending"):
            sec[k] = [r for r in sec[k] if r["id"] not in _drop]
        out_rows = [r for r in out_rows if r["id"] not in _drop]
    rem = lambda r: 0 if r.get("reminded") else 1        # 到期的稍后提醒排在所在分组最前
    sec["act"].sort(key=lambda r: (rem(r), r.get("due") or "9999-99-99", -ts(r)))
    sec["reply"].sort(key=lambda r: (rem(r), ts(r)))
    for k in ("watch", "other", "pending"):
        sec[k].sort(key=lambda r: (rem(r), -ts(r)))
    clusters = {}
    for r in sorted(sec["notice"], key=ts, reverse=True):
        c = clusters.setdefault(r["alert_key"], {"key": r["alert_key"], "ids": [], "sender": r.get("sender") or r.get("sender_email") or ""})
        c["ids"].append(r["id"])
        if len(c["ids"]) == 1:
            c.update(state=r.get("alert_state") or "", subject=r.get("subject") or "", latest=ts(r))
    for c in clusters.values():
        head = next(r for r in sec["notice"] if r["id"] == c["ids"][0])
        if head.get("mute_id"):
            c.update(mute_id=head["mute_id"], mute_until=head["mute_until"], mute_scope=head["mute_scope"])
    muted = [c for c in clusters.values() if c.get("mute_id")]
    cl = sorted((c for c in clusters.values() if not c.get("mute_id")), key=lambda c: (c["state"] != "alerting", -c["latest"]))
    muted.sort(key=lambda c: -c["latest"])
    return {"ok": True, "days": days, "generated_at": _t.time(),
            "sections": {k: [r["id"] for r in v] for k, v in sec.items() if k != "notice"},
            "clusters": cl, "muted_clusters": muted, "rows": out_rows,
            "counts": {"total": len(out_rows), "analyzable": analyzable, "analyzed": analyzed, "snoozed": snoozed_n,
                       "muted": sum(len(c["ids"]) for c in muted), **{k: len(v) for k, v in sec.items()}}}

# ── 稍后提醒 / 静音告警────────────────────────────────────────────
_SNOOZE_REMIND_WINDOW = 3 * 86400     # 到期后「提醒」标记保留 3 天（或直到处理掉）


# ── 完成说明 / 处理记录──────────────────────────────────────────────
import shutil
_NOTE_DIR = ROOT / "data" / "archive_notes"
_NOTE_OUTCOMES = ("已处理", "已回复", "已转交", "不处理", "其他")
_NOTE_MAX_FILE = 25 * 1024 * 1024


def _note_text(html: str) -> str:
    t = _re_tri.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", html or "", flags=_re_tri.I)
    t = _re_tri.sub(r"<[^>]+>", "", t)
    import html as _h
    return "\n".join(" ".join(l.split()) for l in _h.unescape(t).splitlines() if l.strip())[:20000]


def _note_out(n: dict) -> dict:
    n = dict(n)
    n["attachments"] = [{"name": a.get("name"), "size": a.get("size")} for a in (n.get("attachments") or [])]
    return n


def _note_links(ids) -> list:
    out = []
    for i in ids or []:
        i = str(i or "").strip()
        if i and i not in out and db.get_email_by_id(i):
            out.append(i)
    return out[:20]


def _link_brief(ids) -> list:
    out = []
    for i in ids:
        e = db.get_email_by_id(i)
        if e:
            out.append({"id": i, "subject": e.get("subject") or "", "sender": e.get("sender") or e.get("sender_email") or "", "time": e.get("time")})
    return out


def _staged_take(token: str, note_id: int) -> dict | None:
    """把暂存上传的文件移进该 note 的目录。token 只能是暂存区里的文件名，防路径穿越。"""
    tok = Path(str(token or "")).name
    src = _NOTE_DIR / "_staging" / tok
    if not tok or not src.is_file():
        return None
    dst_dir = _NOTE_DIR / str(int(note_id)); dst_dir.mkdir(parents=True, exist_ok=True)
    name = tok.split("_", 1)[1] if "_" in tok else tok
    dst = dst_dir / name
    k = 1
    while dst.exists():
        dst = dst_dir / ("%s(%d)%s" % (Path(name).stem, k, Path(name).suffix)); k += 1
    os.replace(src, dst)
    return {"name": dst.name, "size": dst.stat().st_size}


def _purge_notes():
    try:
        for nid in db.purge_deleted_notes():
            shutil.rmtree(_NOTE_DIR / str(int(nid)), ignore_errors=True)
        st = _NOTE_DIR / "_staging"
        if st.is_dir():
            import time as _t
            for f in st.iterdir():   # 暂存超过 1 天没被引用的上传
                if f.is_file() and _t.time() - f.stat().st_mtime > 86400:
                    f.unlink(missing_ok=True)
    except Exception as e:
        log.warning("清理处理记录附件失败: %s", e)


@app.post("/api/notes/upload")
def note_upload(body: dict):
    name = Path(str(body.get("name") or "file")).name
    data = str(body.get("data") or "")
    if "," in data and data[:5].lower() == "data:":
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:
        raise HTTPException(400, "解码失败")
    if not raw:
        raise HTTPException(400, "空文件")
    if len(raw) > _NOTE_MAX_FILE:
        raise HTTPException(413, "附件超过 25MB")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "file"
    st = _NOTE_DIR / "_staging"; st.mkdir(parents=True, exist_ok=True)
    tok = "%s_%s" % (uuid.uuid4().hex[:12], safe[:120])
    (st / tok).write_bytes(raw)
    return {"ok": True, "token": tok, "name": safe[:120], "size": len(raw)}


@app.get("/api/emails/{email_id}/notes")
def email_notes(email_id: str):
    notes = [_note_out(n) for n in db.notes_for(email_id)]
    for n in notes:
        n["links"] = _link_brief(n.get("links") or [])
    back = [{"note_id": n["id"], "email_id": n["email_id"], "outcome": n.get("outcome"),
             **(_link_brief([n["email_id"]]) or [{}])[0]} for n in db.notes_linking(email_id)]
    return {"ok": True, "notes": notes, "linked_from": back}


def _note_payload(body: dict) -> dict:
    out = {}
    if "outcome" in body:
        oc = str(body.get("outcome") or "")
        out["outcome"] = oc if oc in _NOTE_OUTCOMES else ""
    if "note_html" in body:
        h = str(body.get("note_html") or "")[:200000]
        out["note_html"] = h; out["note_text"] = _note_text(h)
    if "links" in body:
        out["links"] = _note_links(body.get("links"))
    return out


@app.post("/api/emails/{email_id}/notes")
def add_email_note(email_id: str, body: dict):
    if not db.get_email_by_id(email_id):
        raise HTTPException(404, "邮件不存在")
    p = _note_payload(body)
    nid = db.add_note(email_id, p.get("outcome", ""), p.get("note_html", ""), p.get("note_text", ""), [], p.get("links", []))
    atts = [a for a in (_staged_take(t, nid) for t in (body.get("attachments") or [])) if a]
    if atts:
        db.update_note(nid, attachments=atts)
    return {"ok": True, "id": nid, "note": _note_out(db.get_note(nid))}


@app.patch("/api/notes/{note_id}")
def patch_note(note_id: int, body: dict):
    n = db.get_note(note_id)
    if not n:
        raise HTTPException(404, "处理记录不存在")
    p = _note_payload(body)
    if "deleted" in body:
        p["deleted"] = 1 if body.get("deleted") else 0
    atts = list(n.get("attachments") or [])
    if "remove_attachments" in body:
        rm = set(str(x) for x in body.get("remove_attachments") or [])
        for a in atts:
            if a.get("name") in rm:
                (_NOTE_DIR / str(int(note_id)) / Path(a["name"]).name).unlink(missing_ok=True)
        atts = [a for a in atts if a.get("name") not in rm]
    atts += [a for a in (_staged_take(t, note_id) for t in (body.get("attachments") or [])) if a]
    if atts != (n.get("attachments") or []):
        p["attachments"] = atts
    db.update_note(note_id, **p)
    return {"ok": True, "note": _note_out(db.get_note(note_id))}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int):
    ok = db.update_note(note_id, deleted=1)
    _purge_notes()
    return {"ok": True, "removed": ok}


@app.get("/api/notes/{note_id}/attachment")
def note_attachment(note_id: int, name: str):
    from fastapi.responses import FileResponse
    n = db.get_note(note_id)
    nm = Path(str(name or "")).name
    if not n or nm not in [a.get("name") for a in (n.get("attachments") or [])]:
        raise HTTPException(404, "附件不存在")
    f = _NOTE_DIR / str(int(note_id)) / nm
    if not f.is_file():
        raise HTTPException(404, "附件文件不存在")
    from urllib.parse import quote as _q
    q = _q(nm)
    return FileResponse(str(f), media_type="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + q, "X-Content-Type-Options": "nosniff"})


# ── 行动项状态 / 纠正 / 系列──────────────────────────────
import threading as _th10
_SERIES = {"ts": 0.0, "n": -1, "by_id": {}, "groups": {}}
_SERIES_LOCK = _th10.Lock()
_SERIES_MIN = 3          # 至少 3 期才算系列
_SERIES_TTL = 120


def _series_key(sender: str, subject: str) -> str:
    from core.ai_engine import AIEngine as _AE
    return (sender or "").lower() + "|" + _AE.subject_pattern(subject or "")


def _series_index() -> dict:
    """id → {key, idx(第几期, 1 起), n, latest}；groups：key → [id 新到旧]。告警 / 跳过 AI 的发件人与已拆分的 key 不算系列（告警另有折叠）。"""
    import time as _t
    with _SERIES_LOCK:
        if _t.time() - _SERIES["ts"] < _SERIES_TTL:
            return _SERIES
        cfg_ai = (_load_cfg().get("ai") or {})
        sk_acc = set(cfg_ai.get("skip_accounts") or [])
        sk_fold = [str(x) for x in (cfg_ai.get("skip_folders") or []) if x]
        sk_snd = [str(x).lower() for x in (cfg_ai.get("skip_sender_patterns") or []) if x]
        splits = {s["skey"] for s in db.list_splits()}
        groups = {}
        unread = {}
        for i, snd, subj, ts, folder, acc, un in db.series_rows():
            unread[i] = bool(un)
            if not snd or folder in _TRI_SKIP_FOLDERS or acc in sk_acc or any(f in folder for f in sk_fold) or any(p in snd for p in sk_snd):
                continue
            k = _series_key(snd, subj)
            if k in splits or k.endswith("|"):
                continue
            groups.setdefault(k, []).append((ts, i))
        by_id, keep = {}, {}
        for k, L in groups.items():
            if len(L) < _SERIES_MIN:
                continue
            L.sort(reverse=True)
            ids = [i for _, i in L]
            keep[k] = ids
            for pos, i in enumerate(ids):
                by_id[i] = {"key": k, "idx": len(ids) - pos, "n": len(ids), "latest": ids[0], "unread": unread.get(i, False)}
        _SERIES.update(ts=_t.time(), by_id=by_id, groups=keep)
        return _SERIES


def _series_invalidate():
    _SERIES["ts"] = 0.0


def _series_hidden_ids() -> list:
    """收件箱折叠用：每个系列只露最新一期，其余隐藏。往期里还没读的不藏（否则未读徽标会出现「点不到的未读」）。"""
    return [i for i, s in _series_index()["by_id"].items() if s["latest"] != i and not s.get("unread")]


@app.get("/api/series")
def series_detail(key: str, cur: str = ""):
    """各期（新→旧，最多 60）。headline = 那期 AI 简报的结论句（变化卡的基准列表用，没有则用摘要）；
    给了 cur 时，比 cur 更早的各期带 cmp：与 cur 的对比已缓存且两期正文都没变。"""
    g = _series_index()["groups"].get(key)
    if not g:
        return {"ok": True, "key": key, "n": 0, "items": []}
    ce = db.get_email_by_id(cur) if cur and cur in g else None
    ch = _insight_hash(ce) if ce else ""
    cpos = g.index(cur) if ce else -1
    items = []
    for pos, i in enumerate(g[:60]):
        e = db.get_email_by_id(i)
        if not e:
            continue
        ins = db.get_insight(i) or {}
        it = {"id": i, "subject": e.get("subject") or "", "time": e.get("time"), "received_timestamp": e.get("received_timestamp"),
              "unread": bool(e.get("unread")), "archived": bool(e.get("archived")),
              "headline": " ".join(str(ins.get("headline") or ins.get("summary") or "").split())[:80]}
        if ce and pos > cpos:
            c = db.get_compare(cur if pos - cpos == 1 else cur + "|" + i)
            it["cmp"] = bool(c and c["hash"] == _hl.sha1((ch + _insight_hash(e)).encode()).hexdigest())
        items.append(it)
    return {"ok": True, "key": key, "n": len(g), "items": items}


# ── 与上期对比（§10 第三批）─────────────────────────────────────────────────────
def _norm_txt(s) -> str:
    return "".join(str(s or "").split())


def _traceable(quote, body) -> bool:
    q = _norm_txt(quote)
    return len(q) >= 4 and q in _norm_txt(body)


@app.post("/api/series/compare")
def series_compare(body: dict):
    """本期 vs 上期：new / continued / followups。每条结论必须能在对应正文里找到原文（quote），找不到的丢弃；
    上期行动的状态优先看本人待办（已完成 / 进行中），其次看本期原文，都没有就是「状态未知」——绝不推断为完成。"""
    eid = str(body.get("email_id") or "")
    e = db.get_email_by_id(eid)
    if not e:
        raise HTTPException(404, "邮件不存在")
    si = _series_index()
    s_ = si["by_id"].get(eid)
    if not s_:   # 刚到的邮件可能还不在 120s 缓存的系列索引里（daemon 预先对比时常见）：重建一次再判
        _series_invalidate(); si = _series_index(); s_ = si["by_id"].get(eid)
    if not s_:
        return {"ok": False, "error": "这封邮件不属于任何系列"}
    g = si["groups"][s_["key"]]
    pos = g.index(eid)
    base = str(body.get("base_id") or "")
    if base:   # 用户自选对比期：必须是同一系列里的另一期；选了更新的一期就按时间先后对调
        if base == eid or base not in g:
            return {"ok": False, "error": "对比期必须是同一系列里的另一期"}
        if g.index(base) < pos:
            eid, base = base, eid
            e = db.get_email_by_id(eid)
            if not e:
                return {"ok": False, "error": "本期邮件不存在"}
            pos = g.index(eid)
        prev_id = base
    else:
        if pos + 1 >= len(g):
            return {"ok": False, "error": "这是系列的第一期，没有上期可比"}
        prev_id = g[pos + 1]
    gap = g.index(prev_id) - pos            # 1 = 相邻两期
    ckey = eid if gap == 1 else eid + "|" + prev_id   # 相邻沿用旧缓存键；跨期按两期组合各自缓存，来回切换不重复调 AI
    prev = db.get_email_by_id(prev_id)
    if not prev:
        return {"ok": False, "error": "上期邮件不存在"}
    h = _hl.sha1((_insight_hash(e) + _insight_hash(prev)).encode()).hexdigest()
    todos = [t for t in db.get_todos(limit=5000) if t.get("source_email_id") == prev["id"]]
    cached = db.get_compare(ckey)
    if cached and cached["hash"] == h and not body.get("refresh"):
        out = cached["data"]
    else:
        pins = db.get_insight(prev["id"]) or {}
        prev_actions = [a.get("text") for a in (pins.get("actions") or []) if a.get("text")]
        for t in todos:
            if t.get("content") and t["content"] not in prev_actions:
                prev_actions.append(t["content"])
        r = ai.compare_issues(e, prev, prev_actions[:12])
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "generation_failed"}
        d, cb, pb = r["data"], r["cur_body"], r["prev_body"]
        clip = lambda x, n: " ".join(str(x or "").split())[:n]
        new = [{"text": clip(x.get("text"), 60), "quote": clip(x.get("quote"), 80)} for x in (d.get("new") or []) if isinstance(x, dict)
               and x.get("text") and _traceable(x.get("quote"), cb)][:6]
        cont = [{"text": clip(x.get("text"), 60), "quote": clip(x.get("quote"), 80), "prev_quote": clip(x.get("prev_quote"), 80)} for x in (d.get("continued") or [])
                if isinstance(x, dict) and x.get("text") and _traceable(x.get("quote"), cb) and _traceable(x.get("prev_quote"), pb)][:6]
        fu_ai = {_norm_txt(x.get("text")): x for x in (d.get("followups") or []) if isinstance(x, dict)}
        fus = []
        if not prev_actions:   # 上期没记录行动项：只收 AI 从上期正文里找到、且原文对得上的
            prev_actions = [clip(x.get("text"), 80) for x in (d.get("followups") or []) if isinstance(x, dict) and x.get("text") and _traceable(x.get("prev_quote"), pb)][:6]
        for a in prev_actions[:12]:
            x = fu_ai.get(_norm_txt(a)) or {}
            q = clip(x.get("quote"), 80)
            ok = _traceable(q, cb)
            fus.append({"text": clip(a, 80), "prev_quote": clip(x.get("prev_quote"), 80), "quote": q if ok else "", "progress": clip(x.get("progress"), 48) if ok else ""})
        dropped = (len(d.get("new") or []) - len(new)) + (len(d.get("continued") or []) - len(cont))
        out = {"new": new, "continued": cont, "followups": fus, "dropped": max(0, dropped)}
        db.save_compare(ckey, prev["id"], h, out)
        cached = db.get_compare(ckey)
    # 本人待办状态每次实时读（不进缓存）：待办完成了，对比结果立刻跟着变
    tstat = {_norm_txt(t.get("content")): t for t in todos}
    for f in out.get("followups") or []:
        t = tstat.get(_norm_txt(f["text"]))
        if t and t.get("status") == "done":
            f["status"], f["source"] = "done", "你的待办已完成"
        elif f.get("quote"):
            f["status"], f["source"] = "progress", "本期原文"
        elif t:
            f["status"], f["source"] = "open", "你的待办未完成"
        else:
            f["status"], f["source"] = "unknown", "纪要未写明"
    return {"ok": True, "cur": {"id": eid, "time": e.get("time")}, "prev": {"id": prev["id"], "time": prev.get("time"), "subject": prev.get("subject")},
            "created_at": (cached or {}).get("created_at"), "gap": gap, **out}


@app.post("/api/series/split")
def series_split(body: dict):
    k = str(body.get("key") or "")
    if "|" not in k:
        raise HTTPException(400, "key 无效")
    db.add_split(k, str(body.get("label") or "")[:120]); _series_invalidate()
    return {"ok": True}


@app.delete("/api/series/split")
def series_unsplit(key: str):
    ok = db.remove_split(key); _series_invalidate()
    return {"ok": True, "removed": ok}


@app.post("/api/emails/{email_id}/actions")
def set_action_state(email_id: str, body: dict):
    text = str(body.get("text") or "").strip()
    state = body.get("state")
    if not text or state not in (None, "", "not_mine"):
        raise HTTPException(400, "text 必填，state 只能是 not_mine 或空（撤销）")
    if not db.get_email_by_id(email_id):
        raise HTTPException(404, "邮件不存在")
    db.set_action_state(email_id, text, state or None)
    return {"ok": True, "states": db.action_states(email_id).get(email_id, {})}


@app.post("/api/corrections")
def add_correction(body: dict):
    """「判断有误，我要处理」：这封改为 to（默认 act），同一发件人 + 同一主题模板之后也按它处理。"""
    e = db.get_email_by_id(str(body.get("email_id") or ""))
    if not e:
        raise HTTPException(404, "邮件不存在")
    to = body.get("to") if body.get("to") in ("act", "reply", "notice", "fyi") else "act"
    from core.ai_engine import AIEngine as _AE
    sender = (e.get("sender_email") or "").lower(); pat = _AE.subject_pattern(e.get("subject") or "")
    cid = db.add_correction(e["id"], sender, pat, ((e.get("sender") or sender) + " · " + pat)[:120], to)
    db.set_insight_priority(e["id"], to)
    return {"ok": True, "id": cid, "priority": to}


@app.get("/api/ai-rules")
def ai_rules():
    """你对 AI 判断做过的调整，一处看全。type：stop / sender / correction / mute / split。"""
    import time as _t
    fmt = lambda ts: _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts)) if ts else ""
    items = []
    for r in ((_load_cfg().get("prompts") or {}).get("rules") or []):
        if not isinstance(r, dict):
            continue
        acts = r.get("actions") or {}
        conds = "；".join("%s %s %s" % (c.get("field"), c.get("op"), c.get("value")) for c in (r.get("conditions") or []) if isinstance(c, dict))
        if acts.get("no_action"):
            items.append({"type": "stop", "id": r.get("name"), "scope": r.get("name"), "detail": conds,
                          "effect": "直接归入「其他」，不分析" if acts.get("skip_ai") else "仍显示摘要，但不提取待办",
                          "skip": bool(acts.get("skip_ai")), "enabled": r.get("enabled", True) is not False,
                          "source": r.get("source") or "停止提取", "created": r.get("created") or ""})
        else:
            eff = [x for x in ((("分类：" + acts["category"]) if acts.get("category") else ""), (("移到：" + acts["folder"]) if acts.get("folder") else ""),
                               "星标" if acts.get("star") else "", "标已读" if acts.get("mark_read") else "", "跳过 AI" if acts.get("skip_ai") else "") if x]
            items.append({"type": "sender", "id": r.get("name"), "scope": r.get("name"), "detail": conds, "effect": "、".join(eff) or "（无动作）",
                          "enabled": r.get("enabled", True) is not False, "source": r.get("source") or "邮件规则", "created": r.get("created") or ""})
    pri = {"act": "需要处理", "reply": "等你回复", "notice": "系统通知", "fyi": "仅供阅读"}
    for c in db.list_corrections():
        items.append({"type": "correction", "id": c["id"], "scope": c["label"] or (c["sender"] + " · " + c["pattern"]), "detail": "发件人 %s；主题模板「%s」" % (c["sender"], c["pattern"]),
                      "effect": "按「%s」处理" % pri.get(c["to_priority"], c["to_priority"]), "to": c["to_priority"],
                      "source": "判断有误", "created": fmt(c["created_at"]), "email_id": c["email_id"]})
    for m in db.list_mutes(active_only=True):
        items.append({"type": "mute", "id": m["id"], "scope": m["label"] or m["key"], "detail": ("该发件人的全部告警" if m["scope"] == "sender" else "这一个告警"),
                      "effect": "今日分拣里不显示，至 " + fmt(m["until_ts"]), "source": "静音告警", "created": fmt(m["created_at"])})
    for sp in db.list_splits():
        items.append({"type": "split", "id": sp["skey"], "scope": sp["label"] or sp["skey"], "detail": sp["skey"],
                      "effect": "不再按系列折叠", "source": "拆分系列", "created": fmt(sp["created_at"])})
    return {"ok": True, "items": items}


@app.patch("/api/ai-rules/stop")
def ai_rule_stop_edit(body: dict):
    name = str(body.get("name") or "")
    c = _load_cfg(); pr = c.setdefault("prompts", {}); rules = pr.get("rules") or []
    hit = [r for r in rules if isinstance(r, dict) and r.get("name") == name and (r.get("actions") or {}).get("no_action")]
    if not hit:
        raise HTTPException(404, "规则不存在")
    for r in hit:
        r.setdefault("actions", {})["skip_ai"] = bool(body.get("skip"))
        if "enabled" in body:
            r["enabled"] = bool(body.get("enabled"))
    pr["rules"] = rules; _save_rules_preserving(rules); ai.prompts = pr
    return {"ok": True}


@app.delete("/api/ai-rules/stop")
def ai_rule_stop_remove(name: str):
    """撤销停止提取规则：只影响之后到达的邮件；已打的「无需处理」标签不动（在邮件上点「恢复」可单独去掉）。"""
    c = _load_cfg(); pr = c.setdefault("prompts", {}); rules = pr.get("rules") or []
    keep = [r for r in rules if not (isinstance(r, dict) and r.get("name") == name and (r.get("actions") or {}).get("no_action"))]
    if len(keep) == len(rules):
        raise HTTPException(404, "规则不存在")
    removed = [r for r in rules if r not in keep]
    pr["rules"] = keep; _save_rules_preserving(keep); ai.prompts = pr
    return {"ok": True, "removed": removed}


@app.post("/api/ai-rules/stop/restore")
def ai_rule_stop_restore(body: dict):
    """撤销「撤销」：把刚删掉的规则原样加回。"""
    r = body.get("rule")
    if not isinstance(r, dict) or not (r.get("actions") or {}).get("no_action"):
        raise HTTPException(400, "rule 无效")
    c = _load_cfg(); pr = c.setdefault("prompts", {}); rules = pr.get("rules") or []
    if not any(isinstance(x, dict) and x.get("name") == r.get("name") for x in rules):
        rules.append(r); pr["rules"] = rules; _save_rules_preserving(rules); ai.prompts = pr
    return {"ok": True}


@app.patch("/api/corrections/{cid}")
def edit_correction(cid: int, body: dict):
    to = body.get("to")
    if to not in ("act", "reply", "notice", "fyi"):
        raise HTTPException(400, "to 无效")
    cs = [c for c in db.list_corrections() if c["id"] == cid]
    if not cs:
        raise HTTPException(404, "纠正不存在")
    c = cs[0]
    db.add_correction(c["email_id"], c["sender"], c["pattern"], c["label"], to)
    if c.get("email_id"):
        db.set_insight_priority(c["email_id"], to)
    return {"ok": True}


@app.get("/api/corrections")
def list_corrections():
    return {"ok": True, "corrections": db.list_corrections()}


@app.delete("/api/corrections/{cid}")
def remove_correction(cid: int, restore: str = "", email_id: str = ""):
    ok = db.remove_correction(cid)
    if restore in ("act", "reply", "notice", "fyi") and email_id:
        db.set_insight_priority(email_id, restore)
    return {"ok": True, "removed": ok}


_SYS_SENDER_RE = _re_tri.compile(r"(no-?reply|noreply|notice|notify|system|alert|alarm|monitor|mailer-daemon|postmaster|newsletter)", _re_tri.I)


def _calibrate_priority(e: dict, ins: dict) -> dict:
    """兜底校准：真人发来的邮件，提取出了行动项却判成 fyi → 改为 act（周会纪要曾被判成仅供阅读）。
    系统发件人不动：它们的「行动项」多是通用提示。用户的「判断有误」纠正在这之后应用，优先级更高。"""
    try:
        if ins.get("priority") == "fyi" and (ins.get("actions") or []) and not _SYS_SENDER_RE.search(e.get("sender_email") or ""):
            ins["priority"] = "act"; ins["calibrated"] = True
    except Exception:
        pass
    return ins


def _apply_correction(e: dict, ins: dict) -> dict:
    try:
        from core.ai_engine import AIEngine as _AE
        c = db.find_correction((e.get("sender_email") or "").lower(), _AE.subject_pattern(e.get("subject") or ""))
        if c and ins.get("priority") != c["to_priority"]:
            ins["priority"] = c["to_priority"]; ins["corrected"] = True
    except Exception:
        pass
    return ins


# ── 系统通知数据源────────────────────────────────────────────────
# 前端每分钟拉一次最近 minutes 分钟收到的邮件，自己记已提醒过的 id（同步有延迟，所以按窗口而不是按「上次时间」取）。
# 判据随行返回，前端按用户设置（仅需要处理 / 全部）决定弹不弹：无需处理、静音告警、稍后提醒中的一律不弹。
@app.get("/api/notify-feed")
def notify_feed(minutes: int = 30, account: Optional[str] = None):
    import time as _t
    minutes = max(1, min(int(minutes or 30), 24 * 60))
    cutoff = _t.time() - minutes * 60
    cfg_ai = (_load_cfg().get("ai") or {})
    sk_acc = set(cfg_ai.get("skip_accounts") or [])
    sk_fold = [str(x) for x in (cfg_ai.get("skip_folders") or []) if x]
    sk_snd = [str(x).lower() for x in (cfg_ai.get("skip_sender_patterns") or []) if x]
    rows = []
    for e in db.get_emails_paginated(page=0, page_size=60, archived=False, account=account or None):
        if (e.get("received_timestamp") or 0) < cutoff:
            break
        if (e.get("folder") or "") in _TRI_SKIP_FOLDERS:
            continue
        rows.append(e)
    ids = [e["id"] for e in rows]
    pri = db.get_insight_priorities(ids)
    snz = db.get_snoozes(ids)
    mutes = db.list_mutes(active_only=True)
    mo = {m["key"] for m in mutes if m["scope"] == "object"}
    ms = {m["key"] for m in mutes if m["scope"] == "sender"}
    now = _t.time()
    out = []
    for e in rows:
        snd = (e.get("sender_email") or "").lower()
        out.append({"id": e["id"], "subject": e.get("subject") or "", "sender": e.get("sender") or e.get("sender_email") or "",
                    "account": e.get("account") or "", "time": e.get("time"), "received_timestamp": e.get("received_timestamp"),
                    "unread": bool(e.get("unread")), "ai_priority": "fyi" if _is_no_action(e) else pri.get(e["id"]),
                    "no_action": _is_no_action(e), "snoozed": (snz.get(e["id"]) or 0) > now,
                    "muted": (_alert_key(e) in mo) or (snd in ms),
                    "system": (e.get("account") in sk_acc or any(f in (e.get("folder") or "") for f in sk_fold) or any(x in snd for x in sk_snd))})
    return {"ok": True, "now": now, "minutes": minutes, "emails": out}


@app.post("/api/emails/{email_id}/snooze")
def snooze_email(email_id: str, body: dict):
    import time as _t
    try:
        until = float(body.get("until") or 0)
    except (TypeError, ValueError):
        until = 0
    now = _t.time()
    if not (now + 30 <= until <= now + 366 * 86400):
        raise HTTPException(400, "提醒时间须在 30 秒之后、一年以内")
    if not db.get_email_by_id(email_id):
        raise HTTPException(404, "邮件不存在")
    db.set_snooze(email_id, until)
    return {"ok": True, "until": until}


@app.delete("/api/emails/{email_id}/snooze")
def unsnooze_email(email_id: str):
    return {"ok": True, "removed": db.clear_snooze(email_id)}


@app.get("/api/mutes")
def list_mutes():
    return {"ok": True, "mutes": db.list_mutes(active_only=True)}


@app.post("/api/mutes")
def add_mute(body: dict):
    import time as _t
    scope = str(body.get("scope") or "")
    key = str(body.get("key") or "").strip()
    if scope not in ("object", "sender") or not key:
        raise HTTPException(400, "scope 须为 object / sender，key 不能为空")
    if scope == "sender":
        key = key.lower()
    try:
        days = float(body.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1 / 24, min(days, 90))
    until = _t.time() + days * 86400
    mid = db.add_mute(scope, key, str(body.get("label") or "")[:200], until)
    return {"ok": True, "id": mid, "until": until}


@app.delete("/api/mutes/{mute_id}")
def remove_mute(mute_id: int):
    return {"ok": True, "removed": db.remove_mute(mute_id)}



def _attach_ai_priority(emails) -> None:
    """列表行附带已缓存的 AI 优先级（act/reply/notice/fyi）；未分析的邮件不带该字段。"""
    try:
        if not emails:
            return
        m = db.get_insight_priorities([x.get("id") for x in emails if isinstance(x, dict)])
        try:
            import time as _t
            _now = _t.time()
            snz = {k: v for k, v in db.get_snoozes([x.get("id") for x in emails if isinstance(x, dict)]).items() if v > _now}
        except Exception:
            snz = {}
        for x in emails:
            p = "fyi" if _is_no_action(x) else m.get(x.get("id"))
            if p:
                x["ai_priority"] = p
            if x.get("id") in snz:
                x["snooze_until"] = snz[x["id"]]
        try:
            si = _series_index()["by_id"]
            for x in emails:
                s_ = si.get(x.get("id"))
                if s_:
                    x["series_key"] = s_["key"]; x["series_n"] = s_["n"]; x["series_idx"] = s_["idx"]
        except Exception:
            pass
        try:
            nc = db.note_counts([x.get("id") for x in emails if isinstance(x, dict)])
            for x in emails:
                if x.get("id") in nc:
                    x["note_count"] = nc[x["id"]]
        except Exception:
            pass
    except Exception:
        pass   # 优先级只是增强信息，取不到不能影响列表


# ── 「无需处理」：对某类邮件打标签，以后不标待处理、不提取待办────────────
_NO_TAG = "无需处理"


def _is_no_action(e) -> bool:
    if not isinstance(e, dict):
        return False
    t = e.get("tags")
    if isinstance(t, str):
        try:
            t = json.loads(t or "[]")
        except Exception:
            t = []
    return isinstance(t, list) and _NO_TAG in t


def _save_rules_preserving(rules: list) -> None:
    """只改 prompts.rules，用 ruamel 往返读写，保留 config.yaml 的注释与键序。

    _save_cfg 用的是 yaml.dump：会丢掉全部注释并重排键（例如 smtp.accounts 的注释会被删）。
    「无需处理」会被频繁点，不能每点一次就把配置文件格式洗一遍。ruamel 不可用时退回 _save_cfg。
    """
    try:
        from ruamel.yaml import YAML
        y = YAML(); y.preserve_quotes = True; y.width = 4096
        y.representer.add_representer(type(None), lambda r, d: r.represent_scalar('tag:yaml.org,2002:null', 'null'))   # 保持原文的 null 写法
        p = ROOT / "config.yaml"
        with open(p, "r", encoding="utf-8") as f:
            doc = y.load(f)
        doc.setdefault("prompts", {})["rules"] = rules
        tmp = p.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            y.dump(doc, f)
        # Windows：目标正被其他进程读取时 os.replace 报 WinError 5，短暂重试；仍失败就原地写（同进程写锁下可接受）
        import time as _t
        for _i in range(5):
            try:
                os.replace(tmp, p); break
            except PermissionError:
                _t.sleep(0.2)
        else:
            p.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8"); tmp.unlink(missing_ok=True)
    except ImportError:
        c = _load_cfg(); c.setdefault("prompts", {})["rules"] = rules; _save_cfg(c)


_SKIP_TAG = "不分析"


def _no_action_rule(e: dict, scope: str, skip: bool = False) -> dict:
    sender = (e.get("sender_email") or "").strip().lower()
    conds = [{"field": "from", "op": "is", "value": sender}]
    name = "无需处理 · " + (e.get("sender") or sender)
    if scope == "same_kind":
        pat = ai.subject_pattern(e.get("subject") or "")
        conds.append({"field": "subject", "op": "pattern_is", "value": pat})
        name += " · " + pat[:24]
    import time as _t
    return {"name": name[:60], "enabled": True, "match_mode": "all", "conditions": conds, "ai_match": "",
            "source": "停止提取", "created": _t.strftime("%Y-%m-%d %H:%M"),
            "actions": {"no_action": True, "category": "", "folder": "", "star": False, "mark_read": False,
                        "skip_ai": bool(skip), "summary_prompt": "", "draft_prompt": ""}}


def _no_action_targets(e: dict, scope: str) -> list:
    """同类邮件 id（未删除）。按发件人地址先用 SQL 收窄，再按规则同口径判定。"""
    if scope == "single":
        return [e["id"]]
    sender = (e.get("sender_email") or "").strip()
    if not sender:
        return [e["id"]]
    rule = _no_action_rule(e, scope)
    out = []
    with db._conn() as conn:
        rows = conn.execute("SELECT id, subject, sender, sender_email, recipients, account, folder, has_attachment FROM emails"
                            " WHERE deleted=0 AND lower(sender_email)=lower(?)", (sender,)).fetchall()
    fields_of = ai._email_match_fields
    for r in rows:
        f = fields_of(dict(r))
        if all(ai._eval_condition(c, f) for c in rule["conditions"]):
            out.append(r[0])
    if e["id"] not in out:
        out.append(e["id"])
    return out


def _pending_todos_for(ids) -> list:
    ids = [i for i in ids if i]
    if not ids:
        return []
    out = []
    with db._conn() as conn:
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            out += [dict(r) for r in conn.execute("SELECT id, content, source_email_id FROM todos WHERE status!='done'"
                                                  " AND source_email_id IN (%s)" % ",".join("?" * len(part)), part)]
    return out


@app.get("/api/no-action/preview")
def no_action_preview(email_id: str, scope: str = "same_kind"):
    e = db.get_email_by_id(email_id)
    if not e:
        return {"ok": False, "error": "邮件不存在"}
    scope = scope if scope in ("same_kind", "sender", "single") else "same_kind"
    ids = _no_action_targets(e, scope)
    todos = _pending_todos_for(ids)
    return {"ok": True, "scope": scope, "matched": len(ids), "pending_todos": len(todos),
            "todo_samples": [t["content"] for t in todos[:3]],
            "sender": e.get("sender_email") or "", "pattern": ai.subject_pattern(e.get("subject") or "")}


@app.post("/api/no-action")
def no_action_apply(body: dict):
    """body: {email_id, scope: same_kind|sender|single, complete_todos: bool}"""
    e = db.get_email_by_id(str(body.get("email_id") or ""))
    if not e:
        return {"ok": False, "error": "邮件不存在"}
    scope = body.get("scope") if body.get("scope") in ("same_kind", "sender", "single") else "same_kind"
    skip = body.get("mode") == "skip"     # skip：之后直接归入「其他」，不做 AI 分析；默认 keep：仍显示摘要但不提取待办
    rule_name, rule_created = "", False
    if scope != "single":
        c = _load_cfg()
        pr = c.setdefault("prompts", {})
        rules = pr.get("rules") or []
        rule = _no_action_rule(e, scope, skip)
        same = [r for r in rules if isinstance(r, dict) and (r.get("actions") or {}).get("no_action")
                and r.get("conditions") == rule["conditions"]]
        if same and skip and not (same[0].get("actions") or {}).get("skip_ai"):
            same[0].setdefault("actions", {})["skip_ai"] = True; pr["rules"] = rules; _save_rules_preserving(rules); ai.prompts = pr
        rule_created = not same
        if not same:
            rules.append(rule)        # 追加在末尾；no_action 规则不参与首个命中链，位置不影响其他规则
            pr["rules"] = rules
            _save_rules_preserving(rules)
            ai.prompts = pr
        rule_name = (same[0] if same else rule)["name"]
    ids = _no_action_targets(e, scope)
    tagged = sum(1 for i in ids if db.add_tag(i, _NO_TAG))
    if skip:
        for i in ids:
            db.add_tag(i, _SKIP_TAG)
    done = 0
    if body.get("complete_todos"):
        for t in _pending_todos_for(ids):
            try:
                db.update_todo(t["id"], status="done"); done += 1
            except Exception:
                pass
    return {"ok": True, "scope": scope, "rule": rule_name, "rule_created": rule_created, "matched": len(ids),
            "tagged": tagged, "todos_done": done, "ids": ids}


@app.post("/api/no-action/undo")
def no_action_undo(body: dict):
    """撤销：去掉这批邮件的「无需处理」标签；本次新建的规则一并删除（已完成的待办不回滚）。"""
    n = 0
    for i in (body.get("ids") or [])[:5000]:
        e = db.get_email_by_id(str(i))
        t = (e or {}).get("tags") or []
        if _NO_TAG in t or _SKIP_TAG in t:
            db.set_tags(e["id"], [x for x in t if x not in (_NO_TAG, _SKIP_TAG)]); n += 1
    removed = False
    name = str(body.get("rule") or "")
    if body.get("rule_created") and name:
        c = _load_cfg(); pr = c.setdefault("prompts", {})
        rules = pr.get("rules") or []
        keep = [r for r in rules if not (isinstance(r, dict) and r.get("name") == name and (r.get("actions") or {}).get("no_action"))]
        if len(keep) != len(rules):
            pr["rules"] = keep; _save_rules_preserving(keep); ai.prompts = pr; removed = True
    return {"ok": True, "untagged": n, "rule_removed": removed}


def _unread_inbox_count() -> int:
    """侧边栏"收件箱"徽标用的未读数。

    口径必须与收件箱/未读列表完全一致：未删除 + 未归档 + 排除免打扰话题。
    否则徽标会把用户点不到的邮件也算进去（归档里的未读、被免打扰收起的未读），
    出现"未读已经点完了，计数还有 N"。
    """
    _m = _muted_ids()
    return db.count_filtered(unread_only=True, archived=False,
                             exclude_ids=_m or None)


@app.get("/api/stats")
def get_stats():
    ai_health = _ai_health.snapshot()
    return {
        "total":        db.count(),
        # 全库未读（不含回收站）。刻意不再用 get_unread_count_by_folder() 求和——
        # 那个函数按 folder 分组并丢掉 folder 为空的行，会少算。
        "unread":       db.count_filtered(unread_only=True),
        "unread_inbox": _unread_inbox_count(),
        "pending_ai":   db.count_no_ai(SKIP_ACCOUNTS, SKIP_FOLDERS),
        "sync_running": db.get_sync_status("sync_running") == "1",
        "sync_progress":db.get_sync_status("sync_progress") or "",
        "last_sync":    db.get_sync_status("last_sync_end") or "",
        "ollama_ok":    ai_health["reachable"] is True,
        "ai_health":    ai_health,
        "ai_provider":  ai.provider,
        "drafts":       db.count_drafts(),
    }

# ── 同步 ──────────────────────────────────────────────────────────────

_SYNC_SIGNAL_FILE = ROOT / "data" / ".sync_trigger"


@app.post("/api/sync")
def trigger_sync(full: bool = False):
    """通知 daemon 进程立即同步（通过信号文件）。

    server 不再自己执行同步，避免 COM 线程问题和职责混乱。
    daemon 进程检测到信号文件后执行同步并清除该文件。
    """
    running = db.get_sync_status("sync_running") == "1"
    # 写入信号文件，daemon 会在当前同步结束后的下次轮询中检测到
    _SYNC_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SYNC_SIGNAL_FILE.write_text("full" if full else "incremental", encoding="utf-8")
    if running:
        return {"ok": True, "msg": "同步进行中，已排队等待下次执行"}
    return {"ok": True, "msg": "同步信号已发送，等待 daemon 执行"}

# ── AI 流式接口 ───────────────────────────────────────────────────────

def _sender_history_hint(email: dict, limit: int = 5) -> str:
    """同一发件人近期往来邮件，供摘要的【历史提醒】参考。"""
    try:
        sender = email.get("sender_email", "")
        if not sender:
            return ""
        prior = [e for e in db.get_emails_by_person(sender, limit=12) if e.get("id") != email.get("id")]
        prior = prior[:limit]
        if not prior:
            return ""
        lines = [f"· [{(e.get('time') or '')[:10]}] {(e.get('subject') or '')[:50]}" for e in prior]
        return f"发件人 {email.get('sender','')} 近期同类往来（约 {len(prior)} 封）：\n" + "\n".join(lines)
    except Exception:
        return ""


@app.get("/api/stream/summary/{email_id}")
async def stream_summary(email_id: str):
    email = db.get_email_by_id(email_id)
    if not email:
        return StreamingResponse(iter([_sse({"error": "邮件不存在", "done": True})]),
                                 media_type="text/event-stream")

    # 已有摘要直接返回（秒响应）
    if email.get("summary"):
        todos = email.get("action_items") or []
        if isinstance(todos, str):
            try: todos = json.loads(todos)
            except: todos = []
        async def _cached():
            yield _sse({"text": email["summary"], "cached": True})
            yield _sse({"todos": todos, "done": True})
        return StreamingResponse(_cached(), media_type="text/event-stream", headers=SSE_HEADERS)

    # 同类历史邮件提醒（同一发件人近期往来）
    hist_hint = _sender_history_hint(email)

    # 流式生成
    full_text = []
    async def _gen():
        q, t = _bridge(ai.stream_summary, email.get("subject",""), email.get("body",""), hist_hint, email)
        async for chunk_sse in _drain(q, t):
            data = json.loads(chunk_sse[6:])
            if data.get("text"):
                full_text.append(data["text"])
                yield chunk_sse
            elif data.get("done"):
                summary = "".join(full_text)
                # 流式后端在故障时把 "[API 异常: ...]" 当作正文 yield 出来。
                # 落库就等于把错误文本固化成这封邮件的摘要，而摘要端点
                # 「已有摘要直接返回」会让它永不重生成。宁可这次不缓存。
                if generation_failed(summary) or not summary.strip():
                    log.warning("摘要生成失败，不落库 email_id=%s: %r",
                                email_id, summary[:160])
                    yield _sse({"todos": [], "done": True})
                else:
                    todos = ai.quick_todos(summary)
                    db.update_ai_fields(email_id, "📢内部通知", summary, todos)
                    yield _sse({"todos": todos, "done": True})

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/stream/chat")
async def stream_chat(body: dict):
    msg          = body.get("message", "")
    email_id     = body.get("email_id")
    history      = body.get("history") or []
    cur_email    = db.get_email_by_id(email_id) if email_id else None

    # 关键修改：当用户选中了特定邮件时，只基于该邮件回答，不搜索其他邮件
    # 避免 RAG 检索到无关邮件导致 AI "混淆"
    if cur_email:
        search_hits = []  # 不搜索其他邮件，只用当前邮件上下文
    else:
        search_hits = vs.chat_search(msg, n_results=6) if msg else []

    async def _gen():
        q, t = _bridge(ai.stream_chat, msg, search_hits, cur_email, history)
        async for s in _drain(q, t):
            yield s

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/stream/global-chat")
async def stream_global_chat(body: dict):
    msg         = body.get("message", "")
    history     = body.get("history") or []
    search_hits = vs.chat_search(msg, n_results=12) if msg else []

    async def _gen():
        q, t = _bridge(ai.stream_chat, msg, search_hits, None, history)
        async for s in _drain(q, t):
            yield s

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)

# ── 起草回复 ──────────────────────────────────────────────────────────

@app.post("/api/stream/draft")
async def stream_draft(body: dict):
    email_id = body.get("email_id", "")
    tone     = body.get("tone", "正式")
    email    = db.get_email_by_id(email_id) if email_id else None
    if not email:
        return StreamingResponse(iter([_sse({"error": "邮件不存在", "done": True})]),
                                 media_type="text/event-stream")
    _snd = (email.get("sender") or "").strip()
    if not _snd or "@" in _snd:
        # 邮件头只有地址时用往来邮件里出现过的显示名，草稿才能称呼对方，
        # 而不是把原文对「我」的称呼照抄成抬头
        try:
            _n = db.get_display_name(email.get("sender_email", ""))
        except Exception as _e:
            log.warning("draft: 查发件人显示名失败: %s", _e)
            _n = ""
        if _n:
            email = dict(email, sender=_n)
    async def _gen():
        q, t = _bridge(ai.stream_draft, email.get("subject",""), email.get("body",""), tone, email)
        async for s in _drain(q, t):
            yield s
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)

# ── 待办 ──────────────────────────────────────────────────────────────

# ── 草稿箱 ────────────────────────────────────────────────────────────
# MCP 的 create_draft 打到 POST /api/drafts。这里刻意只做草稿的增删改查，
# 不提供任何「草稿转发送」的服务端捷径——发送必须由人在界面上点 /api/emails/send。

def _draft_out(d: dict) -> dict:
    """出参统一成前端 compose 的字段名（to/cc），并保留 to_addr 兼容。"""
    return {
        "id": d.get("id"),
        "to": d.get("to_addr") or "",
        "cc": d.get("cc_addr") or "",
        "subject": d.get("subject") or "",
        "body": d.get("body") or "",
        "is_html": bool(d.get("is_html")),
        "account": d.get("account") or "",
        "reply_to_id": d.get("reply_to_id") or "",
        "source": d.get("source") or "ui",
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        # 回写到服务端草稿箱的落点。改前没带这两个，GET 恒回 None，
        # 看起来像"回写没成功"，其实库里有值——排查时会被误导。
        "imap_folder": d.get("imap_folder") or "",
        "imap_uid": d.get("imap_uid") or "",
    }


def _draft_in(body: dict) -> dict:
    """入参同时接受 to/cc 与 to_addr/cc_addr，MCP 与前端各按自己的习惯写。"""
    out = {}
    if "to" in body or "to_addr" in body:
        out["to_addr"] = str(body.get("to") or body.get("to_addr") or "")
    if "cc" in body or "cc_addr" in body:
        out["cc_addr"] = str(body.get("cc") or body.get("cc_addr") or "")
    for k in ("subject", "body", "account", "reply_to_id", "source"):
        if k in body:
            out[k] = "" if body.get(k) is None else str(body.get(k))
    if "is_html" in body:
        out["is_html"] = bool(body.get("is_html"))
    return out


def _push_draft_to_imap(draft_id: int) -> bool:
    """把草稿回写到服务端草稿箱，让手机原生客户端也能看到。

    单向（local -> IMAP），best-effort：**失败只记日志，绝不影响草稿本身已写入成功**。
    草稿箱能用比"手机能看到"重要得多。
    同步侧的 DEFAULT_SKIP_FOLDERS 本来就跳过草稿箱，所以这份副本不会被拉回来
    变成 emails 表里的重复条目——这也是"不需要定双向冲突规则"的原因。

    注意：APPEND 到草稿箱不是投递，MCP 面依旧没有任何外发能力。
    """
    # 整个函数体都在 try 内：本函数的契约是"无论如何不影响调用方"，
    # 取配置、取草稿这些同样可能抛（改前它们在 try 外，加上 log 未定义，
    # 直接把 POST /api/drafts 变成 500）。
    try:
        imap_cfg = cfg.get("imap") or {}
        if not imap_cfg.get("enabled"):
            return False
        d = db.get_draft(draft_id)
        if not d:
            return False
        accounts = list((imap_cfg.get("accounts") or {}).keys())
        acct = (d.get("account") or "").strip() or (accounts[0] if accounts else "")
        if not acct:
            return False
        from core import win_credential
        from core.imap_extractor import from_config as _imap_from_config
        from core.imap_extractor import push_draft_to_imap as _push
        pw = win_credential.get_password(acct)
        if not pw:
            log.warning("草稿回写 IMAP 跳过：账户 %s 无可用凭据", acct)
            return False
        ext = _imap_from_config({"imap": imap_cfg}, acct)
        # db.get_draft() 回的是**原始列**（to_addr / cc_addr）；
        # to / cc 是 _draft_out() 映射出来的 API 形状，对原始行恒为空。
        # 改前写成 d.get("to")，导致 APPEND 上去的草稿没有收件人。
        folder, uid = _push(ext, pw,
                            subject=d.get("subject", ""), body=d.get("body", ""),
                            to_addr=d.get("to_addr", "") or "",
                            cc_addr=d.get("cc_addr", "") or "",
                            is_html=bool(d.get("is_html")),
                            drop_uid=(d.get("imap_uid") or ""))
        del pw                                  # 不让密码在栈上多停留
        if not folder:
            return False
        db.update_draft(draft_id, imap_folder=folder, imap_uid=uid)
        log.info("草稿 %s 已回写 IMAP 草稿箱 %s uid=%s", draft_id, folder, uid or "?")
        return True
    except Exception as e:
        try:
            log.warning("草稿回写 IMAP 失败（不影响草稿本身）: %s", repr(e)[:160])
        except Exception:
            pass                    # 连记日志都失败也不能把异常带出去
        return False


@app.get("/api/drafts")
def list_drafts(limit: int = 200):
    return [_draft_out(d) for d in db.get_drafts(limit=min(max(1, limit), 500))]


@app.post("/api/drafts")
async def create_draft(body: dict):
    data = _draft_in(body)
    subject = (data.get("subject") or "").strip()
    text = (data.get("body") or "").strip()
    if not subject and not text:
        raise HTTPException(400, "subject 与 body 不能同时为空")
    did = db.add_draft(subject=data.get("subject", ""), body=data.get("body", ""),
                       to_addr=data.get("to_addr", ""), cc_addr=data.get("cc_addr", ""),
                       is_html=data.get("is_html", False),
                       account=data.get("account") or None,
                       reply_to_id=data.get("reply_to_id") or None,
                       source=data.get("source") or "agent")
    # 前端自动保存每 3s 一次，只写本地草稿箱；不往邮件服务器草稿箱堆副本（那边删不掉、手机上可见）
    pushed = False if body.get("imap") is False else _push_draft_to_imap(did)
    return {"ok": True, "id": did, "sent": False, "imap_pushed": pushed}


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: int):
    d = db.get_draft(draft_id)
    if not d:
        raise HTTPException(404, "草稿不存在")
    return _draft_out(d)


@app.patch("/api/drafts/{draft_id}")
async def patch_draft(draft_id: int, body: dict):
    if not db.get_draft(draft_id):
        raise HTTPException(404, "草稿不存在")
    db.update_draft(draft_id, **_draft_in(body))
    pushed = False
    if body.get("imap") is True and not (db.get_draft(draft_id) or {}).get("imap_folder"):
        pushed = _push_draft_to_imap(draft_id)   # 自动保存过的草稿，用户点「存草稿」时才第一次回写
    return {"ok": True, "id": draft_id, "imap_pushed": pushed}


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: int):
    if not db.delete_draft(draft_id):
        raise HTTPException(404, "草稿不存在")
    return {"ok": True}


@app.get("/api/todos")
def list_todos(status: Optional[str] = None):
    return db.get_todos(status=status)


@app.post("/api/todos")
async def create_todo(body: dict):
    tid = db.add_todo(body.get("content",""),
                      source_email_id=body.get("source_email_id"),
                      priority=body.get("priority","normal"),
                      due_date=body.get("due_date"))
    return {"id": tid}


@app.patch("/api/todos/{todo_id}")
async def update_todo(todo_id: int, body: dict):
    db.update_todo(todo_id, **body)
    return {"ok": True}


@app.delete("/api/todos/{todo_id}")
def delete_todo(todo_id: int):
    db.delete_todo(todo_id)
    return {"ok": True}


_todo_batch = {"running": False, "scanned": 0, "total": 0, "added": 0, "error": "", "done": False}

@app.post("/api/todos/extract-batch")
async def todos_extract_batch(body: dict):
    if _todo_batch.get("running"):
        return {"ok": False, "error": "\u5df2\u6709\u6279\u91cf\u63d0\u53d6\u5728\u8fdb\u884c\u4e2d"}
    days = int(body.get("days", 7) or 7)
    accounts = body.get("accounts") or []
    use_ai = bool(body.get("use_ai", True))
    max_emails = min(int(body.get("max_emails", 200) or 200), 500)

    def _worker():
        _todo_batch.update(running=True, scanned=0, total=0, added=0, error="", done=False)
        try:
            emails = db.get_emails_for_batch(days, accounts, max_emails)
            _todo_batch["total"] = len(emails)
            for e in emails:
                eid = e.get("id")
                added = 0
                if db.has_tag(eid, _NO_TAG):
                    _todo_batch["skipped_no_action"] = _todo_batch.get("skipped_no_action", 0) + 1
                    _todo_batch["scanned"] += 1
                    continue
                try:
                    added += db.import_action_items(eid)
                    if added == 0 and use_ai:
                        items = ai.extract_todos(e.get("subject", "") or "", e.get("body", "") or "")
                        # items 为 None 表示 AI 生成/解析失败（[] 才是"确实没有待办"）。
                        # 不记下来的话，报告只会说"扫了 N 封加了 M 条"，
                        # 用户会以为这批邮件本来就没待办。
                        if items is None:
                            _todo_batch["ai_failed"] = _todo_batch.get("ai_failed", 0) + 1
                            items = []
                        contents = []
                        for it in items:
                            c = (it.get("content") if isinstance(it, dict) else str(it)) or ""
                            if c.strip():
                                contents.append(c.strip())
                        added += db.add_todos_dedup(contents, source_email_id=eid)
                except Exception as _ex:
                    # 原来是 except: pass —— 逐封失败连痕迹都不留。
                    _todo_batch["errors"] = _todo_batch.get("errors", 0) + 1
                    if not _todo_batch.get("last_error"):
                        _todo_batch["last_error"] = "%s: %s" % (type(_ex).__name__,
                                                               str(_ex)[:90])
                _todo_batch["added"] += added
                _todo_batch["scanned"] += 1
        except Exception as ex:
            _todo_batch["error"] = str(ex)[:150]
        finally:
            _todo_batch["running"] = False
            _todo_batch["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "started": True}

@app.get("/api/todos/extract-batch/status")
def todos_extract_batch_status():
    return dict(_todo_batch)

# ── 导出 ──────────────────────────────────────────────────────────────

@app.get("/api/export/todos")
def export_todos():
    return Response(db.export_todos_csv().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=todos.csv"})


@app.get("/api/export/emails")
def export_emails(limit: int = 1000):
    return Response(db.export_emails_csv(limit=limit).encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=emails.csv"})

# ── 人物 ──────────────────────────────────────────────────────────────

_ADDR_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _clean_contact_name(name: str, addr: str) -> str:
    """规范化显示名。

    people 表里的 name 质量差（多数不是真实显示名）：
    等于地址、只是地址片段、被单引号包住、或干脆为空。
    这里统一成"要么是真名，要么空串"——展示端回退为只显示地址，
    不硬凑一个假名字。
    """
    n = (name or "").strip().strip("'\"").strip()
    a = (addr or "").strip()
    if not n:
        return ""
    low_n, low_a = n.lower(), a.lower()
    if low_n == low_a:
        return ""                       # 名字就是地址，等于没有名字
    if "@" in n and low_n in low_a:
        return ""                       # 地址的截断片段
    if low_a.startswith(low_n) and "@" not in n:
        return ""                       # 地址的本地部分，例如 xuligang
    return n


def _contact_rows(q: str, limit: int) -> list:
    """清洗后的联系人列表。只保留真实邮件地址，按往来次数降序。"""
    out = []
    for r in db.get_people(kw=q or "", limit=max(limit * 3, limit + 200)):
        addr = (r.get("email") or "").strip()
        # Exchange X.500 legacy DN 不是地址，补进收件人会发不出去
        if not _ADDR_RE.match(addr):
            continue
        out.append({
            "email": addr,
            "name": _clean_contact_name(r.get("name"), addr),
            "count": int(r.get("email_count") or 0),
            "last_seen": r.get("last_seen"),
        })
        if len(out) >= limit:
            break
    return out


@app.get("/api/people")
def list_people(q: Optional[str] = None, limit: int = 50):
    """清洗后的通讯录。供收件人补全与通讯录页共用。

    limit 上限 3000：通讯录页一次取全（全量约 6KB），前端本地过滤，
    补全就不必逐键打服务端。
    """
    return _contact_rows(q or "", max(1, min(int(limit or 50), 3000)))

# ── 设置 ──────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    c = _load_cfg()
    llm = c.get("llm", {})
    api = llm.get("api", {})
    ol  = c.get("ollama", {})
    return {
        "llm_provider":   llm.get("provider", "ollama"),
        "ollama_url":     ol.get("base_url", "http://127.0.0.1:11434"),
        "ollama_model":   ol.get("model", "qwen2.5:7b"),
        "api_base_url":   api.get("base_url", ""),
        # 不回传明文：前端留空 = 不修改；填了才覆盖
        "api_key":        "",
        "api_key_set":    bool(_resolve_api_key(api)),
        "api_key_source": "env" if os.environ.get("QUILLBOX_API_KEY") else ("config" if api.get("api_key") else ""),
        "api_model":      api.get("model", ""),
        "api_timeout":    api.get("timeout", 120),
        "sync_days":      c.get("email", {}).get("incremental_days", 7),
        "sync_interval":  c.get("email", {}).get("sync_interval", 15),
        "prompt_summary": c.get("prompts", {}).get("summary", ""),
        "prompt_draft":   c.get("prompts", {}).get("draft", ""),
        "prompt_chat":    c.get("prompts", {}).get("chat", ""),
        "prompt_topic_digest": c.get("prompts", {}).get("topic_digest", ""),
        "prompt_topic_nl":     c.get("prompts", {}).get("topic_nl", ""),
        "prompt_todo_extract": c.get("prompts", {}).get("todo_extract", ""),
        "prompt_insight":      c.get("prompts", {}).get("insight", ""),
        "prompt_insight_default": ai.DEFAULT_INSIGHT_PROMPT,
        "prompt_rule_gen":     c.get("prompts", {}).get("rule_gen", ""),
        "prompt_rules":   json.dumps(c.get("prompts", {}).get("rules", []), ensure_ascii=False, indent=2),
        "signature":      c.get("signature", ""),
    }


@app.post("/api/settings")
def save_settings(body: dict):
    global ai
    c = _load_cfg()
    # 更新 llm 节
    c.setdefault("llm", {})
    c.setdefault("ollama", {})
    if "llm_provider"  in body: c["llm"]["provider"]        = body["llm_provider"]
    if "ollama_url"    in body: c["ollama"]["base_url"]      = body["ollama_url"]
    if "ollama_model"  in body: c["ollama"]["model"]         = body["ollama_model"]
    api_sec = c["llm"].setdefault("api", {})
    if "api_base_url"  in body: api_sec["base_url"]          = body["api_base_url"]
    # 留空表示「不修改」（GET 不再回传明文，前端拿到的永远是空串）；明确清除用 api_key_clear=true
    if body.get("api_key_clear"): api_sec.pop("api_key", None)
    elif (body.get("api_key") or "").strip(): api_sec["api_key"] = body["api_key"].strip()
    if "api_model"     in body: api_sec["model"]             = body["api_model"]
    if "api_timeout"   in body: api_sec["timeout"]           = int(body["api_timeout"])
    if "sync_days"     in body: c.setdefault("email", {})["incremental_days"] = int(body["sync_days"])
    if "sync_interval" in body: c.setdefault("email", {})["sync_interval"]    = int(body["sync_interval"])
    pr = c.setdefault("prompts", {})
    if "prompt_summary" in body: pr["summary"] = body["prompt_summary"] or ""
    if "prompt_draft"   in body: pr["draft"]   = body["prompt_draft"] or ""
    if "prompt_chat"    in body: pr["chat"]    = body["prompt_chat"] or ""
    if "prompt_topic_digest" in body: pr["topic_digest"] = body["prompt_topic_digest"] or ""
    if "prompt_topic_nl"     in body: pr["topic_nl"]     = body["prompt_topic_nl"] or ""
    if "prompt_todo_extract" in body: pr["todo_extract"] = body["prompt_todo_extract"] or ""
    if "prompt_insight" in body and (body["prompt_insight"] or "insight" in pr):   # 留空且原来没有 → 不往 config 里加空键
        pr["insight"] = body["prompt_insight"] or ""
    if "prompt_rule_gen"     in body: pr["rule_gen"]     = body["prompt_rule_gen"] or ""
    if "prompt_rules"   in body:
        try:
            _rules = json.loads(body["prompt_rules"] or "[]")
            if isinstance(_rules, list):
                pr["rules"] = _rules
        except Exception:
            pass  # JSON 非法则忽略，不覆盖已有规则
    if "signature"      in body: c["signature"] = body["signature"] or ""
    _save_cfg(c)
    # 热重载 AIEngine（不重启 server）
    ai = AIEngine(
        base_url     = c["ollama"].get("base_url", "http://127.0.0.1:11434"),
        model        = c["ollama"].get("model", "qwen2.5:7b"),
        provider     = c["llm"].get("provider", "ollama"),
        api_base_url = api_sec.get("base_url", ""),
        api_key      = _resolve_api_key(api_sec),
        api_model    = api_sec.get("model", ""),
        api_timeout  = api_sec.get("timeout", 120),
        prompts      = c.get("prompts", {}),
        signature    = c.get("signature", ""),
    )
    return {"ok": True, "provider": ai.provider}


@app.post("/api/settings/test-ai")
def test_ai_connection(body: dict):
    """用表单当前值临时构建 AIEngine 测试连通性，不影响运行中的 ai 实例"""
    provider = body.get("llm_provider", "ollama")
    test_ai = AIEngine(
        base_url     = body.get("ollama_url", "http://127.0.0.1:11434"),
        model        = body.get("ollama_model", "qwen2.5:7b"),
        provider     = provider,
        api_base_url = body.get("api_base_url", ""),
        api_key      = (body.get("api_key") or "").strip() or _resolve_api_key((_load_cfg().get("llm") or {}).get("api") or {}),
        api_model    = body.get("api_model", ""),
        api_timeout  = int(body.get("api_timeout", 120)),
    )
    ok = test_ai.is_running()
    label = "内网 API" if provider == "api" else "Ollama"
    return {"ok": ok, "provider": provider, "label": label}


@app.post("/api/rules/generate")
def rules_generate(body: dict):
    """自然语言 → 规则（功能1）"""
    return ai.generate_rule(body.get("description", ""))


@app.post("/api/rules/evaluate")
def rules_evaluate(body: dict):
    """评估规则（功能2）"""
    return {"evaluation": ai.evaluate_rule(body.get("rule") or {})}


# ── 批量操作 ──────────────────────────────────────────────────────────

@app.post("/api/emails/bulk")
async def bulk_emails(body: dict):
    """批量操作：read/unread/star/unstar/delete/category(需 value)。"""
    ids = body.get("ids") or []
    action = body.get("action", "")
    value = body.get("value")
    if not ids:
        return {"ok": False, "error": "未选择邮件"}
    if action == "category" and value:
        db.bulk_update_category(ids, value)
        return {"ok": True, "count": len(ids)}
    n = 0
    for eid in ids:
        try:
            if action == "read":     db.mark_read(eid, True)
            elif action == "unread": db.mark_read(eid, False)
            elif action == "star":   db.star_email(eid, True)
            elif action == "unstar": db.star_email(eid, False)
            elif action == "delete": db.soft_delete(eid, True)
            else: continue
            n += 1
        except Exception:
            pass
    return {"ok": True, "count": n}


# ── 保存的搜索 / 智能视图 ─────────────────────────────────────────────

def _save_cfg(c: dict):
    # 保留注释与格式，只改变化的键（core/config_store.py）。原 yaml.dump 每次保存都洗掉全部注释。
    from core.config_store import save_config
    save_config(ROOT / "config.yaml", c)

@app.get("/api/reader")
def get_reader():
    """insight.v2 的「关于我」：角色、上级 / 下属、关注的系统、内部域名。"""
    c = _load_cfg()
    return {"ok": True, "reader": c.get("reader") or {}, "enabled": bool((c.get("ai") or {}).get("insight_v2", True))}


@app.post("/api/reader")
def set_reader(body: dict):
    rd = body.get("reader")
    if not isinstance(rd, dict):
        return {"ok": False, "error": "reader 必填"}
    def person(p):
        return {"name": str((p or {}).get("name") or "").strip()[:30], "email": str((p or {}).get("email") or "").strip().lower()[:80]} if isinstance(p, dict) else None
    def people(lst):
        return [x for x in (person(p) for p in (lst if isinstance(lst, list) else [])) if x and (x["name"] or x["email"])][:30]
    def strs(lst, n=30, w=40):
        return [str(x).strip()[:w] for x in (lst if isinstance(lst, list) else []) if str(x).strip()][:n]
    clean = {"name": str(rd.get("name") or "").strip()[:30], "role": str(rd.get("role") or "").strip()[:300],
             "manager": person(rd.get("manager")) or {"name": "", "email": ""},
             "subordinates": people(rd.get("subordinates")), "peers": people(rd.get("peers")),
             "focus": strs(rd.get("focus")), "internal_domains": [d.lower() for d in strs(rd.get("internal_domains"), 20, 60)]}
    c = _load_cfg()
    c["reader"] = clean
    if "enabled" in body:
        c.setdefault("ai", {})["insight_v2"] = bool(body["enabled"])
    _save_cfg(c)
    return {"ok": True, "reader": clean}


@app.get("/api/saved-searches")
def get_saved_searches():
    return _load_cfg().get("saved_searches", [])

@app.post("/api/saved-searches")
def add_saved_search(body: dict):
    name = (body.get("name") or "").strip()
    query = (body.get("query") or "").strip()
    if not name or not query:
        return {"ok": False, "error": "名称和查询都必填"}
    c = _load_cfg()
    lst = [s for s in c.get("saved_searches", []) if s.get("name") != name]
    lst.append({"name": name, "query": query})
    c["saved_searches"] = lst
    _save_cfg(c)
    return {"ok": True, "saved_searches": lst}

@app.delete("/api/saved-searches/{name}")
def del_saved_search(name: str):
    c = _load_cfg()
    c["saved_searches"] = [s for s in c.get("saved_searches", []) if s.get("name") != name]
    _save_cfg(c)
    return {"ok": True, "saved_searches": c["saved_searches"]}


# ── 动态分类（内置 + 规则定义 + 库中已用）─────────────────────────────

@app.get("/api/categories")
def get_categories():
    base = ["🚨重要行动", "📢内部通知", "📰订阅"]
    seen = set(base); out = list(base)
    try:
        for r in (_load_cfg().get("prompts", {}) or {}).get("rules", []) or []:
            cat = ((r.get("actions") or {}).get("category") or "").strip()
            if cat and cat not in seen:
                seen.add(cat); out.append(cat)
    except Exception:
        pass
    try:
        for cat in db.get_distinct_categories():
            if cat and cat not in seen:
                seen.add(cat); out.append(cat)
    except Exception:
        pass
    return out


# ── AI 提取待办（从指定邮件）──────────────────────────────────────────

@app.post("/api/emails/{email_id}/extract-todos")
def extract_todos_endpoint(email_id: str, force: int = 0):
    """从单封邮件提取待办。与 /api/todos/extract-batch 同口径。

    改前这里无条件调 AI 且用 add_todo 直插，于是：
      * 忽略摘要早已算好并落库的 action_items，白花一次 AI 调用
        （库里已有几条，AI 又跑一次换回不同的）；
      * 不去重，重复点「AI待办」会造出重复待办行。
    批量路径一直是"先 import_action_items，没有才调 AI，且去重"，
    这里只是没接上。force=1 可显式绕过缓存重新问 AI（仍去重）。
    """
    e = db.get_email_by_id(email_id)
    if not e:
        return {"ok": False, "error": "邮件不存在"}
    if _is_no_action(e):
        return {"ok": True, "count": 0, "source": "no_action", "considered": 0, "contents": [],
                "note": "这类邮件已标记为无需处理，不提取待办"}

    # ── 免费路径：用摘要生成时落库的 action_items ──
    if not force:
        cached = e.get("action_items") or []
        if isinstance(cached, str):
            try:
                cached = json.loads(cached)
            except Exception:
                cached = []
        cached = [str(x).strip() for x in cached if str(x).strip()]
        if cached:
            n = db.import_action_items(email_id)   # 自带去重
            return {"ok": True, "count": n, "source": "cached",
                    "considered": len(cached), "contents": cached}

    # ── 付费路径：确实没有已提取结果，才问 AI ──
    try:
        items = ai.extract_todos(e.get("subject", ""), e.get("body", ""))
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:80]}
    if items is None:
        # None = AI 生成或解析失败（[] 才是"确实没有待办"）。
        # 必须回 ok=False，否则界面会把失败显示成「未提取到待办」，
        # 用户不知道这是可以重试的。
        return {"ok": False, "error": "AI 提取待办失败，可重试", "retryable": True}

    added, contents = [], []
    for it in (items or []):
        content = (it.get("content") if isinstance(it, dict) else str(it)) or ""
        content = content.strip()
        if not content:
            continue
        contents.append(content)
        due = it.get("due_date") if isinstance(it, dict) else None
        pri = (it.get("priority") if isinstance(it, dict) else None) or "normal"
        try:
            # TODO-QUALITY-20260930：去重、过滤说明文字、优先级/日期归一都在 add_todo_checked 里
            tid = db.add_todo_checked(content, source_email_id=email_id, priority=pri, due_date=due)
            if tid:
                added.append({"id": tid, "content": content})
        except Exception:
            pass
    return {"ok": True, "count": len(added), "source": "ai",
            "considered": len(contents), "contents": contents, "todos": added}


# ── 从 Outlook 导入规则（D）─────────────────────────────────────────

@app.post("/api/rules/import-outlook")
def import_outlook_rules():
    """由 daemon（拥有 Outlook COM）执行：写触发文件 → 轮询结果文件。"""
    import time as _t
    req = ROOT / "data" / ".import_outlook_req"
    res = ROOT / "data" / ".import_outlook_res"
    try:
        if res.exists(): res.unlink()
    except Exception: pass
    try:
        req.write_text(str(_t.time()), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"无法写触发文件：{e}"}
    for _ in range(50):  # 最多 ~25s
        _t.sleep(0.5)
        if res.exists():
            try:
                data = json.loads(res.read_text(encoding="utf-8"))
            except Exception:
                data = {"ok": False, "error": "结果解析失败"}
            try: res.unlink()
            except Exception: pass
            try: ai.prompts = _load_cfg().get("prompts", {})
            except Exception: pass
            return data
    try: req.unlink()
    except Exception: pass
    return {"ok": False, "error": "导入超时（请确认 Outlook 已打开、且后台同步 daemon 在运行）"}


# ── 统计看板（E）─────────────────────────────────────────────────────

@app.get("/api/dashboard")
def dashboard():
    import time as _t, datetime
    with db._conn() as conn:
        def one(q, p=()):
            return conn.execute(q, p).fetchone()[0]
        total    = one("SELECT COUNT(*) FROM emails WHERE deleted=0")
        unread   = one("SELECT COUNT(*) FROM emails WHERE deleted=0 AND unread=1")
        with_att = one("SELECT COUNT(*) FROM emails WHERE deleted=0 AND has_attachment=1")
        starred  = one("SELECT COUNT(*) FROM emails WHERE deleted=0 AND starred=1")
        by_cat = [{"label": (r[0] or "未分类"), "count": r[1]}
                  for r in conn.execute("SELECT category,COUNT(*) FROM emails WHERE deleted=0 GROUP BY category ORDER BY 2 DESC")]
        by_acct = [{"label": r[0], "count": r[1], "unread": r[2]}
                   for r in conn.execute("SELECT account,COUNT(*),SUM(CASE WHEN unread=1 THEN 1 ELSE 0 END) FROM emails WHERE deleted=0 GROUP BY account ORDER BY 2 DESC")]
        top_senders = [{"label": (r[0] or r[1] or "?"), "count": r[2]}
                       for r in conn.execute("SELECT sender,sender_email,COUNT(*) FROM emails WHERE deleted=0 GROUP BY sender_email ORDER BY 3 DESC LIMIT 10")]
        ts_rows = conn.execute("SELECT received_timestamp FROM emails WHERE deleted=0 AND received_timestamp>?",
                               (_t.time() - 14 * 86400,)).fetchall()
    buckets = {}
    for (ts,) in ts_rows:
        try:
            d = datetime.datetime.fromtimestamp(float(ts)).strftime("%m-%d")
            buckets[d] = buckets.get(d, 0) + 1
        except Exception:
            pass
    by_day = []
    for i in range(13, -1, -1):
        d = (datetime.datetime.now() - datetime.timedelta(days=i)).strftime("%m-%d")
        by_day.append({"label": d, "count": buckets.get(d, 0)})
    try:
        todos = db.count_todos_by_status()
    except Exception:
        todos = {}
    return {"total": total, "unread": unread, "with_attachment": with_att, "starred": starred,
            "by_category": by_cat, "by_account": by_acct, "top_senders": top_senders,
            "by_day": by_day, "todos": todos}


# ── AI 话题/项目聚合（F）─────────────────────────────────────────────

import re as _re2
def _norm_subject(s):
    s = (s or "").strip()
    s = _re2.sub(r'^((re|fw|fwd|回复|答复|回覆|转发|转发件|转)\s*[:：]\s*)+', '', s, flags=_re2.I).strip()
    return s

def _clean_snippet(s):
    s = (s or "")
    s = _re2.sub(r'^\s*#{1,6}\s*.*$', '', s, flags=_re2.M)
    s = _re2.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = _re2.sub(r'【[^】]*】', '', s)
    s = _re2.sub(r'[`#>*]', '', s)
    s = _re2.sub(r'\s+', ' ', s)
    return s.strip()[:140]

def _mk_cluster(label, items, custom=False):
    # items: 该会话下的邮件（dict，按时间倒序，最新在前）
    senders, seen = [], set()
    for it in items:
        nm = (it.get("sender") or it.get("sender_email") or "").strip()
        if nm and nm not in seen:
            seen.add(nm); senders.append(nm)
    latest = items[0]
    chrono = list(reversed(items))[:80]  # 旧→新，Gmail 阅读顺序
    members = [{
        "id": it["id"], "subject": it.get("subject") or "(无主题)",
        "sender": it.get("sender") or it.get("sender_email") or "?",
        "sender_email": it.get("sender_email") or "", "time": it.get("time") or "",
        "snippet": _clean_snippet(it.get("summary") or it.get("body_head") or ""),
        "unread": bool(it.get("unread")), "starred": bool(it.get("starred")),
        "has_attachment": bool(it.get("has_attachment")),
        "account": it.get("account") or "",
    } for it in chrono]
    return {
        "topic": label, "count": len(items), "senders": senders[:8],
        "accounts": list({it.get("account") for it in items if it.get("account")}),
        "latest": latest.get("time") or "", "latest_ts": latest.get("ts") or 0,
        "snippet": _clean_snippet(latest.get("summary") or latest.get("body_head") or ""),
        "unread": sum(1 for it in items if it.get("unread")),
        "starred": any(it.get("starred") for it in items),
        "has_attachment": any(it.get("has_attachment") for it in items),
        "ids": [it["id"] for it in items][:60],
        "members": members, "custom": custom,
        "analysis": "", "_analyzing": False, "_expanded": False,
    }

_TOPIC_COLS = ["id", "subject", "sender", "sender_email", "time", "ts", "account",
               "unread", "starred", "has_attachment", "summary", "body_head", "category"]
_TOPIC_SEL = ("SELECT id,subject,sender,sender_email,time,received_timestamp,account,"
              "unread,starred,has_attachment,summary,substr(body,1,200),category FROM emails ")
_TOPIC_SCORE_MIN = 0.30

def _rows_from(where_sql, params):
    with db._conn() as conn:
        raw = conn.execute(_TOPIC_SEL + where_sql, params).fetchall()
    return [dict(zip(_TOPIC_COLS, r)) for r in raw]

def _kw_list(rule):
    kws = rule.get("keywords") or []
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",") if k.strip()]
    return kws

def _my_addrs():
    with db._conn() as conn:
        accs = [r[0] for r in conn.execute("SELECT DISTINCT account FROM emails WHERE account<>''")]
    return [a for a in accs if a and a not in SKIP_ACCOUNTS]

def _filter_where(filt):
    import datetime as _dt
    where, params = "", []
    if not isinstance(filt, dict):
        return where, params
    if filt.get("account"):
        where += " AND account=?"; params.append(filt["account"])
    if filt.get("folder"):
        where += " AND folder=?"; params.append(filt["folder"])
    if filt.get("category"):
        where += " AND category=?"; params.append(filt["category"])
    if filt.get("from"):
        where += " AND (sender LIKE ? OR sender_email LIKE ?)"; params += ["%%%s%%" % filt["from"]] * 2
    if filt.get("has_attachment"):
        where += " AND has_attachment=1"
    if filt.get("unread"):
        where += " AND unread=1"
    if filt.get("starred"):
        where += " AND starred=1"
    def _ts(d):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
            try:
                return _dt.datetime.strptime(str(d).strip(), fmt).timestamp()
            except Exception:
                pass
        return None
    if filt.get("after"):
        t = _ts(filt["after"])
        if t: where += " AND received_timestamp>=?"; params.append(t)
    if filt.get("before"):
        t = _ts(filt["before"])
        if t: where += " AND received_timestamp<?"; params.append(t)
    for flag, typ in (("to_me", "to"), ("cc_me", "cc")):
        if filt.get(flag):
            addrs = _my_addrs()
            if addrs:
                where += " AND (" + " OR ".join(["recipients LIKE ?"] * len(addrs)) + ")"
                params += ['%%"email": "%s", "type": "%s"%%' % (a, typ) for a in addrs]
    return where, params

def _topic_ids(where_sql, params):
    """话题筛选第一段：在 idx_topic_meta 里判断 WHERE、排序、截断，只返回 id。
    where_sql 只能引用该索引里的列（_filter_where 生成的条件都在）；索引缺失时退回普通查询。"""
    import sqlite3 as _sq
    with db._conn() as conn:
        try:
            raw = conn.execute("SELECT id FROM emails INDEXED BY idx_topic_meta " + where_sql, params).fetchall()
        except _sq.OperationalError:
            raw = conn.execute("SELECT id FROM emails " + where_sql, params).fetchall()
    return [r[0] for r in raw]

def _topic_rows(where_sql, params):
    """两段式取行：先 _topic_ids，再只取命中的行（顺序与原来的 ORDER BY 一致）。"""
    ids = _topic_ids(where_sql, params)
    if not ids:
        return []
    return _rows_from("WHERE id IN (%s) ORDER BY received_timestamp DESC" % ",".join("?" * len(ids)), tuple(ids))

def _emails_for_topic(rule, cap=25):
    filt = rule.get("filters") or {}
    fwhere, fparams = _filter_where(filt)
    kws = _kw_list(rule)
    q = (rule.get("query") or "").strip()
    if not (kws or q):
        # 纯结构化过滤（如"抄送我"）：全量、精确、无语义上限
        return _topic_rows("WHERE deleted=0 " + fwhere + " ORDER BY received_timestamp DESC LIMIT 300", fparams)[:200]
    # 文本匹配器：关键词 ∪ 语义（可选 OR 组）
    text_ids = set()
    if q:
        try:
            hits = [h for h in vs.search(q, n_results=cap)
                    if h.get("id") and float(h.get("score", 0) or 0) >= _TOPIC_SCORE_MIN]
            hits.sort(key=lambda h: float(h.get("score", 0) or 0), reverse=True)
            text_ids.update(h["id"] for h in hits[:cap])
        except Exception:
            pass
    if kws:
        clauses = " OR ".join(["lower(subject) LIKE ?"] * len(kws))
        kp = tuple("%%%s%%" % k.lower() for k in kws)
        text_ids.update(_topic_ids("WHERE deleted=0 AND (%s) ORDER BY received_timestamp DESC LIMIT 300" % clauses, kp))
    if not text_ids:
        return []
    idl = list(text_ids)
    qm = ",".join("?" * len(idl))
    rows = _rows_from("WHERE deleted=0 AND id IN (%s)%s ORDER BY received_timestamp DESC" % (qm, fwhere),
                      tuple(idl) + tuple(fparams))
    return rows[:200]

def _cluster_threads(rows, min_size=1, by="subject"):
    groups = {}
    for r in rows:
        if by == "sender":
            key = (r["sender_email"] or "").lower()
        else:
            key = _norm_subject(r["subject"]).lower()
        if not key:
            key = "__id_" + str(r.get("id"))
        groups.setdefault(key, []).append(r)
    out = []
    for key, items in groups.items():
        if len(items) < min_size:
            continue
        items.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        if by == "sender":
            label = items[0].get("sender") or items[0].get("sender_email") or "?"
        else:
            label = _norm_subject(items[0]["subject"]) or items[0]["subject"] or "(无主题)"
        out.append(_mk_cluster(label, items, custom=False))
    out.sort(key=lambda c: c.get("latest_ts") or 0, reverse=True)
    return out

@app.get("/api/topics")
def topics(days: int = 30, min_size: int = 2, limit: int = 80, by: str = "subject",
           category: str = "", topic: str = ""):
    import time as _t
    cfg = _load_cfg()
    # 用户定义的智能话题：语义(向量)+关键词，全时段
    if topic:
        rule = None
        for cr in (cfg.get("topic_rules", []) or []):
            if (cr.get("name") or "") == topic:
                rule = cr; break
        if not rule:
            return []
        return _cluster_threads(_emails_for_topic(rule), min_size=1)[:limit]
    cutoff = _t.time() - days * 86400
    # 分类标签：近 days 天
    if category:
        rows = _rows_from(
            "WHERE deleted=0 AND received_timestamp>? AND category=? ORDER BY received_timestamp DESC",
            (cutoff, category))
        return _cluster_threads(rows, min_size=1)[:limit]
    # 全部：近 days 天，仅真正的多封会话线程（去掉单封噪声）
    rows = _rows_from(
        "WHERE deleted=0 AND received_timestamp>? ORDER BY received_timestamp DESC", (cutoff,))
    return _cluster_threads(rows, min_size=max(2, min_size), by=by)[:limit]

@app.post("/api/topics/preview")
def topic_preview(body: dict):
    rule = {"query": (body.get("query") or ""), "keywords": body.get("keywords") or [],
            "filters": body.get("filters") or {}}
    rows = _emails_for_topic(rule)
    return {"count": len(rows), "samples": [(r["subject"] or "(无主题)")[:50] for r in rows[:8]]}

@app.post("/api/topics/from-nl")
def topic_from_nl(body: dict):
    import json as _json
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "请输入一句话描述"}
    accts = _my_addrs()
    prompt = (
        (ai._custom("topic_nl") or '把用户的一句话转成邮件话题过滤条件 JSON，只输出 JSON，不要解释。') + '\n'
        'filters 可含: from(发件人含), to_me(bool 发给我), cc_me(bool 抄送我), account(我的邮箱之一), '
        'has_attachment(bool), unread(bool), starred(bool), category(分类名), after(YYYY-MM-DD), before(YYYY-MM-DD).\n'
        'keywords: 主题精确关键词数组. query: 仅当是模糊主题时填的自然语言, 否则空串.\n'
        '只在明确提及时设置字段, 未提及的不要写. 我的邮箱: ' + ", ".join(accts) + '.\n'
        '示例: 抄送给我的会议邮件 -> {"filters":{"cc_me":true},"keywords":["会议"],"query":""}\n'
        '用户输入: ' + text
    )
    try:
        raw = ai._generate(prompt, fmt="json", timeout=45) or ""
        if not isinstance(raw, str):
            raw = str(raw)
        i, j = raw.find("{"), raw.rfind("}")
        data = _json.loads(raw[i:j+1]) if (i >= 0 and j > i) else {}
    except Exception as e:
        return {"ok": False, "error": "AI 解析失败：%s" % str(e)[:80]}
    filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    kws = data.get("keywords") or []
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",") if k.strip()]
    return {"ok": True, "filters": filters, "keywords": kws, "query": (data.get("query") or "").strip()}

@app.post("/api/topic-rules/reorder")
def reorder_topic_rules(body: dict):
    names = body.get("names") or []
    c = _load_cfg()
    rules = c.get("topic_rules", []) or []
    by = {r.get("name"): r for r in rules}
    ordered = [by[n] for n in names if n in by]
    rest = [r for r in rules if r.get("name") not in set(names)]
    c["topic_rules"] = ordered + rest
    _save_cfg(c)
    return {"ok": True, "topic_rules": c["topic_rules"]}

@app.post("/api/topic-rules/update")
def update_topic_rule(body: dict):
    name = (body.get("name") or "").strip()
    c = _load_cfg()
    rules = c.get("topic_rules", []) or []
    for r in rules:
        if r.get("name") == name:
            if "pinned" in body: r["pinned"] = bool(body.get("pinned"))
            if "hidden" in body: r["hidden"] = bool(body.get("hidden"))
            if "quick" in body: r["quick"] = bool(body.get("quick"))
            if "mute" in body: r["mute"] = bool(body.get("mute")); _MUTED_CACHE["ts"] = 0.0
            break
    c["topic_rules"] = rules
    _save_cfg(c)
    return {"ok": True, "topic_rules": rules}

@app.get("/api/inbox-tabs")
def get_inbox_tabs():
    return _load_cfg().get("inbox_tabs", [])

@app.post("/api/inbox-tabs")
def add_inbox_tab(body: dict):
    name = (body.get("name") or "").strip()
    kind = (body.get("kind") or "").strip()
    ref = (body.get("ref") or "").strip()
    if not name or kind not in ("category", "topic") or not ref:
        return {"ok": False, "error": "标签名、类型、来源都必填"}
    c = _load_cfg()
    lst = [x for x in (c.get("inbox_tabs", []) or []) if x.get("name") != name]
    lst.append({"name": name, "kind": kind, "ref": ref})
    c["inbox_tabs"] = lst
    _save_cfg(c)
    return {"ok": True, "inbox_tabs": lst}

@app.delete("/api/inbox-tabs/{name}")
def del_inbox_tab(name: str):
    c = _load_cfg()
    c["inbox_tabs"] = [x for x in (c.get("inbox_tabs", []) or []) if x.get("name") != name]
    _save_cfg(c)
    return {"ok": True, "inbox_tabs": c["inbox_tabs"]}

_MUTED_CACHE = {"ts": 0.0, "ids": set()}

def _muted_ids():
    import time as _t
    if _t.time() - _MUTED_CACHE["ts"] < 30:
        return list(_MUTED_CACHE["ids"])
    ids = set()
    try:
        for cr in (_load_cfg().get("topic_rules", []) or []):
            if cr.get("mute"):
                for r in _emails_for_topic(cr):
                    ids.add(r["id"])
    except Exception:
        pass
    _MUTED_CACHE["ts"] = _t.time(); _MUTED_CACHE["ids"] = ids
    return list(ids)

@app.get("/api/topic-emails")
def topic_emails(topic: str = ""):
    cfg = _load_cfg()
    rule = next((c for c in (cfg.get("topic_rules", []) or []) if (c.get("name") or "") == topic), None)
    if not rule:
        return {"emails": [], "total": 0, "has_more": False}
    ids = [r["id"] for r in _emails_for_topic(rule)]
    if not ids:
        return {"emails": [], "total": 0, "has_more": False}
    with db._conn() as conn:
        qm = ",".join("?" * len(ids))
        byid = {}
        for ro in conn.execute("SELECT * FROM emails WHERE id IN (%s)" % qm, tuple(ids)).fetchall():
            d = db._row_to_dict(ro); byid[d["id"]] = d
    emails = [byid[i] for i in ids if i in byid]
    return {"emails": emails, "total": len(emails), "has_more": False}

@app.get("/api/topic-counts")
def topic_counts():
    out = {}
    for cr in (_load_cfg().get("topic_rules", []) or []):
        nm = cr.get("name")
        try:
            rows = _emails_for_topic(cr)
            out[nm] = {"total": len(rows), "unread": sum(1 for r in rows if r.get("unread"))}
        except Exception:
            out[nm] = {"total": 0, "unread": 0}
    return out

@app.get("/api/topic-rules")
def get_topic_rules():
    return _load_cfg().get("topic_rules", [])

@app.post("/api/topic-rules")
def add_topic_rule(body: dict):
    name = (body.get("name") or "").strip()
    kws = body.get("keywords")
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",") if k.strip()]
    kws = kws or []
    query = (body.get("query") or "").strip()
    filters = body.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    filters = {k: v for k, v in filters.items() if v not in (None, "", False, [])}
    if not name or (not kws and not query and not filters):
        return {"ok": False, "error": "名称必填；属性/关键词/描述至少填一个"}
    c = _load_cfg()
    old = next((x for x in c.get("topic_rules", []) if x.get("name") == name), None)
    lst = [x for x in c.get("topic_rules", []) if x.get("name") != name]
    lst.append({"name": name, "keywords": kws, "query": query, "filters": filters,
                "pinned": bool(old.get("pinned")) if old else False,
                "hidden": bool(old.get("hidden")) if old else False})
    c["topic_rules"] = lst
    _save_cfg(c)
    return {"ok": True, "topic_rules": lst}

@app.delete("/api/topic-rules/{name}")
def del_topic_rule(name: str):
    c = _load_cfg()
    c["topic_rules"] = [x for x in c.get("topic_rules", []) if x.get("name") != name]
    _save_cfg(c)
    return {"ok": True, "topic_rules": c["topic_rules"]}

@app.post("/api/topics/analyze")
def topic_analyze(body: dict):
    ids = body.get("ids") or []
    ems = [e for e in (db.get_email_by_id(i) for i in ids[:20]) if e]
    if not ems:
        return {"ok": False, "analysis": "没有邮件可分析"}
    lines = []
    for e in ems:
        digest = (e.get("summary") or e.get("body", "") or "")[:120]
        lines.append(f"[{(e.get('time') or '')[:10]}] {e.get('sender','')}：{e.get('subject','')} — {digest}")
    _digest_default = ("以下是围绕同一主题/项目的多封邮件（按时间）。请综合分析这个事项：\n"
              "1.【整体进展】这件事目前发展到哪一步。\n"
              "2.【关键节点/决策】重要时间点、决定或变更。\n"
              "3.【涉及的人】主要参与方。\n"
              "4.【当前状态与待办/风险】现在卡在哪、下一步该做什么。\n"
              "用简洁中文分点，不要逐封复述。")
    prompt = ((ai._custom("topic_digest") or _digest_default) + "\n\n" + "\n".join(lines))
    try:
        return {"ok": True, "analysis": ai._generate(prompt, timeout=90)}
    except Exception as e:
        return {"ok": False, "analysis": f"分析失败：{str(e)[:80]}"}

# ── 邮件操作（发送 / 转发 / 删除）────────────────────────────────────────

from core.email_sender import queue_send, queue_forward, queue_delete, get_action_status


def _send_transport() -> str:
    """smtp（默认）或 outlook。发信不可撤销，所以保留一行切回老路的开关。"""
    t = str(((cfg.get("email") or {}).get("send_transport") or "smtp")).strip().lower()
    return t if t in ("smtp", "outlook") else "smtp"


def _sendable_accounts() -> list:
    """能用来发信的账户。

    取自取件侧已配置的账户（imap + ews）—— 凭据与取件同一套，
    不额外维护一份发信账户表（那必然会漂）。
    """
    out = []
    for seg in ("imap", "ews"):
        for a in ((_load_cfg().get(seg) or {}).get("accounts") or {}):
            if a not in out:
                out.append(a)
    return out


def _smtp_conf_for(account: str) -> dict:
    """某账户的 SMTP 配置。

    不同服务器参数可能不同（例如一台 465 隐式 TLS、另一台 587 STARTTLS），
    登录名形态也可能不同（邮箱地址 vs 域\\用户名），所以按账户分开配；
    缺省回落到顶层 smtp 段。
    """
    c = _load_cfg()
    top = dict(c.get("smtp") or {})
    per = ((top.get("accounts") or {}).get(account) or {})
    merged = {k: v for k, v in top.items() if k != "accounts"}
    merged.update({k: v for k, v in per.items() if v not in (None, "")})
    merged.setdefault("username", account)
    return merged


def _smtp_account(prefer: str = "") -> tuple:
    """返回 (账户, 发件人)。

    `prefer` 是界面选择的发件人。**必须是已配置账户之一** ——
    不接受任意 From：那等于给伪造发件人开口子，服务器多半也会拒。
    不匹配时回落到第一个账户，而不是照着用户给的地址硬发。
    """
    accounts = _sendable_accounts()
    want = (prefer or "").strip().lower()
    acct = ""
    for a in accounts:
        if a.lower() == want:
            acct = a
            break
    if not acct:
        acct = accounts[0] if accounts else ""
    sender = (_smtp_conf_for(acct).get("sender") or "").strip() or acct
    return acct, sender


def _append_sent_copy(acct: str, raw: bytes) -> str:
    """把已发送副本存到服务端“已发送”。best-effort，绝不影响已发出的事实。"""
    try:
        if not (cfg.get("smtp") or {}).get("append_to_sent", True):
            return ""
        from core import win_credential
        from core.imap_extractor import from_config as _ifc, append_to_sent
        pw = win_credential.get_password(acct)
        if not pw:
            return ""
        ext = _ifc({"imap": cfg.get("imap") or {}}, acct)
        try:
            return append_to_sent(ext, pw, raw)
        finally:
            del pw
    except Exception as e:
        try:
            log.warning("已发送副本存服务端失败（不影响发送）: %s", repr(e)[:140])
        except Exception:
            pass
        return ""


def _reply_context(email_id: str, is_html: bool) -> dict:
    """按库里的 message_id 生成回复上下文（线程头 + 引用块）。

    改前走 Outlook EntryID，对 imap_ 前缀的邮件恒定位不到，
    于是线程头与引用一起丢掉。message_id 与传输后端无关，是干净的判据。
    """
    out = {"in_reply_to": "", "references": "", "quote_html": "", "quote_text": ""}
    if not email_id:
        return out
    em = db.get_email_by_id(email_id)
    if not em:
        return out
    mid = (em.get("message_id") or "").strip()
    if mid:
        if not mid.startswith("<"):
            mid = "<%s>" % mid
        out["in_reply_to"] = mid
        out["references"] = mid
    from core.smtp_sender import build_quote
    qh, qt = build_quote(em, is_html=is_html)
    out["quote_html"], out["quote_text"] = qh, qt
    return out


@app.post("/api/emails/send")
async def send_email(body: dict):
    to      = body.get("to", "").strip()
    subject = body.get("subject", "").strip()
    content = body.get("body", "").strip()
    if not to or not subject:
        return {"ok": False, "error": "收件人和主题不能为空"}

    if _send_transport() == "outlook":
        # 老路：排队 + Outlook COM。行为与改前完全一致，作为回退保留。
        action_id = queue_send(
            to=to, subject=subject, body=content,
            cc=body.get("cc", ""),
            reply_to_entry_id=body.get("reply_to_entry_id", ""),
            is_html=bool(body.get("is_html")),
            attachments=body.get("attachments") or [],
        )
        return {"ok": True, "action_id": action_id, "transport": "outlook",
                "msg": "已加入发送队列，需在工作机桌面 session 中执行"}

    # SMTP：同步发送。不需要交互桌面 session，也就没有"排队等 Outlook"的理由；
    # 换来的是立刻拿到真实错误，而不是几十分钟后被静默标成 expired。
    smtp_cfg = cfg.get("smtp") or {}
    if not smtp_cfg.get("enabled") or not smtp_cfg.get("host"):
        return {"ok": False, "error": "SMTP 未配置（config.yaml 的 smtp 段）"}
    acct, sender = _smtp_account((body or {}).get("from") or "")
    if not acct:
        return {"ok": False, "error": "没有可用的 IMAP 账户，无法确定发件人与凭据"}

    is_html = bool(body.get("is_html"))
    ctx = _reply_context(body.get("reply_to_entry_id", ""), is_html)
    try:
        from core import win_credential
        from core.smtp_sender import build_message, send_via_smtp
        pw = win_credential.get_password(acct)
        if not pw:
            return {"ok": False, "error": "取不到发信凭据（DPAPI）"}
        msg = build_message(
            sender=sender, to=to, subject=subject, body=content,
            cc=body.get("cc", ""), bcc=body.get("bcc", ""), is_html=is_html,
            signature=str(cfg.get("signature") or ""),
            in_reply_to=ctx["in_reply_to"], references=ctx["references"],
            quote_html=ctx["quote_html"], quote_text=ctx["quote_text"],
            attachments=body.get("attachments") or [],
        )
        raw = msg.as_bytes()
        try:
            _sc = _smtp_conf_for(acct)
            res = send_via_smtp(host=_sc["host"], port=_sc.get("port", 465),
                                username=_sc.get("username") or acct,
                                password=pw, msg=msg,
                                bcc=body.get("bcc", ""),
                                # 465 隐式 TLS / 587 STARTTLS，按账户配置走
                                use_ssl=bool(_sc.get("use_ssl", True)),
                                verify_tls=bool(_sc.get("verify_tls", True)))
        finally:
            del pw
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "SMTP 发送失败"}
        sent_folder = _append_sent_copy(acct, raw)
        log.info("SMTP 发送成功 message_id=%s 收件人 %s 个 副本->%s",
                 res.get("message_id"), res.get("recipients"), sent_folder or "(未存)")
        return {"ok": True, "action_id": "", "transport": "smtp",
                "message_id": res.get("message_id"),
                "recipients": res.get("recipients"),
                "sent_folder": sent_folder,
                "msg": "已发送"}
    except Exception as e:
        try:
            log.warning("SMTP 发送异常: %s", repr(e)[:200])
        except Exception:
            pass
        return {"ok": False, "error": ("发送失败: %s" % repr(e)[:120])}


# ── 撰写邮件附件上传（base64 JSON，避免额外依赖 python-multipart）─────────────
_OUTBOX_DIR = (ROOT / "data" / "outbox").resolve()


@app.post("/api/upload")
async def upload_attachment(body: dict):
    """接收 base64 编码的附件，保存到 data/outbox，返回本地路径供发送时附加。"""
    name = Path(str(body.get("name", "file"))).name
    data = body.get("data", "")
    if not data:
        return {"ok": False, "error": "空文件"}
    # 兼容 data URL 前缀（data:...;base64,xxxx）
    if "," in data and data.strip().lower().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception as e:
        return {"ok": False, "error": f"解码失败: {e}"}
    # 限制单附件 25MB
    if len(raw) > 25 * 1024 * 1024:
        return {"ok": False, "error": "附件超过 25MB 限制"}
    _OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip() or "file"
    dest = _OUTBOX_DIR / f"{uuid.uuid4().hex[:8]}_{safe}"
    dest.write_bytes(raw)
    return {"ok": True, "path": str(dest), "name": name, "size": len(raw)}


@app.post("/api/emails/{email_id}/forward")
async def forward_email(email_id: str, body: dict):
    em = db.get_email_by_id(email_id)
    if not em:
        return {"ok": False, "error": "邮件不存在"}
    to = body.get("to", "").strip()
    if not to:
        return {"ok": False, "error": "收件人不能为空"}
    if _send_transport() == "outlook":
        entry_id  = em.get("entry_id") or email_id
        action_id = queue_forward(entry_id=entry_id, to=to, note=body.get("note", ""),
                                  is_html=bool(body.get("is_html")),
                                  attachments=body.get("attachments") or [])
        return {"ok": True, "action_id": action_id, "transport": "outlook"}

    # SMTP 转发：从库里取原文自己拼引用。
    # 这是 IMAP 邮件的转发从"直接 raise 找不到邮件"变成可用的关键——
    # 老路 execute_forward 走 ns.GetItemFromID，只认 Outlook EntryID。
    smtp_cfg = cfg.get("smtp") or {}
    if not smtp_cfg.get("enabled") or not smtp_cfg.get("host"):
        return {"ok": False, "error": "SMTP 未配置"}
    acct, sender = _smtp_account((body or {}).get("from") or "")
    if not acct:
        return {"ok": False, "error": "没有可用的 IMAP 账户"}
    is_html = bool(body.get("is_html"))
    subj = em.get("subject") or ""
    if not subj.lower().startswith(("fwd:", "fw:", "转发:")):
        subj = "Fwd: " + subj
    try:
        from core import win_credential
        from core.smtp_sender import build_message, send_via_smtp, build_quote
        qh, qt = build_quote(em, is_html=is_html)
        pw = win_credential.get_password(acct)
        if not pw:
            return {"ok": False, "error": "取不到发信凭据（DPAPI）"}
        msg = build_message(
            sender=sender, to=to, subject=subj,
            body=body.get("note", "") or "", cc=body.get("cc", ""),
            bcc=body.get("bcc", ""), is_html=is_html,
            signature=str(cfg.get("signature") or ""),
            quote_html=qh, quote_text=qt,
            attachments=body.get("attachments") or [],
        )
        raw = msg.as_bytes()
        try:
            _sc = _smtp_conf_for(acct)
            res = send_via_smtp(host=_sc["host"], port=_sc.get("port", 465),
                                username=_sc.get("username") or acct,
                                password=pw, msg=msg,
                                bcc=body.get("bcc", ""),
                                # 465 隐式 TLS / 587 STARTTLS，按账户配置走
                                use_ssl=bool(_sc.get("use_ssl", True)),
                                verify_tls=bool(_sc.get("verify_tls", True)))
        finally:
            del pw
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "SMTP 转发失败"}
        _append_sent_copy(acct, raw)
        return {"ok": True, "action_id": "", "transport": "smtp",
                "message_id": res.get("message_id"), "msg": "已转发"}
    except Exception as e:
        try:
            log.warning("SMTP 转发异常: %s", repr(e)[:200])
        except Exception:
            pass
        return {"ok": False, "error": ("转发失败: %s" % repr(e)[:120])}


@app.delete("/api/emails/{email_id}")
async def delete_email(email_id: str, permanent: bool = False):
    em = db.get_email_by_id(email_id)
    entry_id = (em or {}).get("entry_id") or email_id
    action_id = queue_delete(entry_id=entry_id, permanent=permanent)
    # 先在本地 DB 标记删除（乐观更新，让 UI 立即消失）
    db.soft_delete(email_id, True)
    return {"ok": True, "action_id": action_id}


@app.get("/api/action/{action_id}")
def get_action(action_id: str):
    return get_action_status(action_id)

# ── 入口 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 监听地址可配：config.yaml 的 server.host / server.port。
    # 缺省只听本机（127.0.0.1）：本服务持有邮箱凭据与全部邮件，不应在未显式配置时暴露到网络。
    # 需要远程访问时显式写 server.host（例如 0.0.0.0），此时远程来源一律经 _AuthGate 令牌门禁；
    # 更推荐保持 127.0.0.1，经 SSH 本地转发或带认证的反向代理访问。
    _srv = (cfg.get("server") or {})
    _host = str(_srv.get("host", "127.0.0.1"))
    _port = int(_srv.get("port", 8765))
    print("[Quillbox] listening on http://%s:%d" % (_host, _port), flush=True)
    uvicorn.run("server:app", host=_host, port=_port, reload=False, log_level="info")
