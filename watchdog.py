"""watchdog.py — Quillbox 守护进程

职责：
  1. 确保 FastAPI server（端口 8765）始终在运行；挂了自动拉起。
  2. 确保同步 daemon 始终在运行（通过心跳文件判断）；挂了自动拉起。
  3. 通过用户启动项在登录后自动运行（见 install_startup.py），实现重启后自动恢复。

判活方式（避免重复启动）：
  - server：能否连上 127.0.0.1:8765。
  - daemon：data/.daemon_heartbeat 文件的修改时间是否在阈值内。
子进程以 DETACHED 方式启动，即使 watchdog 退出也不会中断服务；
watchdog 再次启动时通过“判活”重新接管，不会重复拉起。

单实例：绑定本地锁端口，重复启动的 watchdog 会直接退出。
"""

import os
import re
import sys
import time
import socket
import subprocess
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent

# 子进程必须用 python.exe（控制台版）启动：pythonw.exe 下 std 流为 None，
# 会导致 torch/transformers 加载嵌入模型时挂起。watchdog 自身可用 pythonw 运行。
def _console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)

PY = _console_python()

SERVER_PORT       = 8765
LOCK_PORT         = 8799            # watchdog 单实例锁端口
HEARTBEAT_FILE    = ROOT / "data" / ".daemon_heartbeat"
DAEMON_STALE_SEC  = 20 * 60         # 心跳超过 20 分钟视为 daemon 已死
CHECK_INTERVAL    = 20              # 每 20s 巡检一次
CREATE_NO_WINDOW  = 0x08000000      # 隐藏控制台但保留 std 句柄，避免 torch/tqdm 在无窗口下挂起
# 卡死判定阈值：进程还在、但功能已死，持续超过下列时长即强制重启。
# 刻意给宽，避免把“慢”误判成“卡死”：server 冷启动（加载嵌入模型与向量库）可能要几分钟。
SERVER_STUCK_SEC  = 900             # 进程在但始终不监听 8765
DAEMON_STUCK_SEC  = 300             # 心跳已过期（DAEMON_STALE_SEC）之后再持续这么久

# 拉起后给一点冷却时间，避免刚启动还没监听端口就被判定为“未起来”而重复拉起
_server_grace_until = 0.0
_daemon_grace_until = 0.0
# 自己拉起的子进程句柄：用 poll() 判活，避免依赖 wmic（它超时会被误判为"进程没在跑"）
_server_proc = None
_daemon_proc = None


def _child_alive(proc) -> bool:
    """我们自己 spawn 的子进程是否还活着。零外部依赖，不会超时。"""
    try:
        return bool(proc) and proc.poll() is None
    except Exception:
        return False


def _log(msg: str):
    line = f"{datetime.now().isoformat(timespec='seconds')} [watchdog] {msg}"
    print(line, flush=True)
    try:
        with open(ROOT / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _port_alive(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _script_names(cmdline: str) -> set:
    """取命令行里出现的每个 .py 的文件名（去目录、转小写）。"""
    return {os.path.basename(m).lower()
            for m in re.findall(r"[^\s\"\']+\.py", cmdline, re.IGNORECASE)}


def _proc_running(script: str) -> bool:
    """检查是否已有运行中的 python 进程在跑指定脚本（避免重复拉起）。

    **必须按文件名精确比对，不能用子串**：
    原实现是 `return script in out`，out 是整段 wmic 输出，于是
    `_proc_running("server.py")` 会被 **mcp_server.py** 满足。
    机器上可能常驻多个 `mcp_server.py`（每个 MCP 宿主会话一个），
    该判据就恒为 True，导致 server 的拉起分支
    `... and not _proc_running("server.py")` 永远不成立——
    API server 挂掉后不会被自动拉起。

    另外把 name 过滤从 'python.exe' 放宽到 python%：
    daemon 是用 pythonw.exe 起的，原来的过滤根本看不到它，
    只是恰好因为 daemon 分支还有心跳判据兜着才没出事。
    """
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name like 'python%'", "get", "CommandLine"],
            capture_output=True, text=True, timeout=8,
            # 不加这个标志，wmic 每次都会新建一个控制台窗口。
            # capture_output 只管重定向 stdio，管不了窗口创建。
            # 巡检 20s 一轮、每轮查 server 与 daemon 两次，桌面上就是一直在闪。
            creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:
        # 探测失败时宁可认为"没在跑"，让上层去拉起：
        # 多拉一次由 _spawn 后的 grace 兜住，漏拉则会一直不可用。
        return False
    target = os.path.basename(script).lower()
    self_name = os.path.basename(__file__).lower()
    for line in out.splitlines():
        names = _script_names(line)
        if self_name in names:
            continue                    # 别把 watchdog 自己算进去
        if target in names:
            return True
    return False


def _daemon_alive() -> bool:
    try:
        if not HEARTBEAT_FILE.exists():
            return False
        return (time.time() - HEARTBEAT_FILE.stat().st_mtime) < DAEMON_STALE_SEC
    except Exception:
        return False


def _spawn(args, logfile, errfile=None):
    """logfile 为 None 时 stdout 丢弃到 DEVNULL（进程自身已写日志文件）。

    errfile 非空时把 **stderr 单独落盘**：进程自己写的日志走 stdout/日志文件，
    但**未捕获异常的 traceback 只走 stderr**。原实现把两者一起丢进 DEVNULL，
    结果 daemon 崩溃时死因彻底不可观测（
    daemon 在一轮同步中途消失，daemon.log 里只有一条异常信息为空的 warning）。
    tqdm 刷屏走 stdout，仍然丢弃，所以"避免刷屏"这个原始顾虑不受影响。
    """
    if logfile is None:
        out = subprocess.DEVNULL
    else:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        out = open(logfile, "a", encoding="utf-8")
    if errfile is None:
        err = subprocess.STDOUT if logfile is not None else subprocess.DEVNULL
    else:
        errfile.parent.mkdir(parents=True, exist_ok=True)
        err = open(errfile, "a", encoding="utf-8")
    return subprocess.Popen(
        [PY] + args, cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=out, stderr=err,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        creationflags=CREATE_NO_WINDOW,
    )


# 各类异常状态的起始时间戳：key -> 首次观察到的 now
_stuck_since: dict = {}


def _stuck_seconds(key: str, condition: bool, now: float) -> float:
    """condition 为真则累计其持续时长；为假立即清零。返回已持续秒数。

    独立成纯函数是为了可单测——卡死判定不该只能靠线上复现来验证。
    """
    if not condition:
        _stuck_since.pop(key, None)
        return 0.0
    started = _stuck_since.setdefault(key, now)
    return max(0.0, now - started)


def _kill_script(proc, script: str) -> list:
    """结束指定脚本的进程。优先用自己的子进程句柄，其次按命令行找 PID。

    watchdog 自身重启过时手上没有句柄，必须有兜底路径，
    否则卡死的服务照样没人能收。
    """
    killed = []
    try:
        if proc is not None and proc.poll() is None:
            proc.kill()
            killed.append(proc.pid)
    except Exception:
        pass
    try:
        out = subprocess.run(
            # 过滤放宽到 python%：daemon 是 pythonw.exe 起的，
            # 原来的 name='python.exe' 根本看不到它。
            ["wmic", "process", "where", "name like 'python%'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW).stdout or ""
        target = os.path.basename(script).lower()
        self_name = os.path.basename(__file__).lower()
        for line in out.splitlines():
            line = line.strip()
            # 改前是 `if script not in line: continue` —— 子串匹配。
            # 杀 server.py 时会命中 ...\mcp_server.py，接着 taskkill 掉
            # 本机全部 MCP 会话进程，Agent 的邮箱工具面整片掉线。
            names = _script_names(line)
            if self_name in names or target not in names:
                continue
            m = re.search(r",(\d+)\s*$", line)
            if not m:
                continue
            pid = int(m.group(1))
            if pid in killed or pid == os.getpid():
                continue
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
            killed.append(pid)
    except Exception:
        pass
    return killed


def _acquire_single_instance():
    """绑定本地锁端口实现单实例；返回 socket（需在进程生命周期内持有）或 None。"""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
        return lock
    except OSError:
        return None


def main():
    lock = _acquire_single_instance()
    if lock is None:
        _log("已有 watchdog 在运行，退出。")
        return

    _log(f"watchdog 启动：巡检间隔 {CHECK_INTERVAL}s，Python={PY}")
    global _server_grace_until, _daemon_grace_until, _server_proc, _daemon_proc

    while True:
        now = time.time()
        try:
            server_up = _port_alive(SERVER_PORT)

            # ── 卡死检测（进程在、但功能已死）────────────────────────
            # 下面两个拉起分支都要求 `not _proc_running(...)`，因此进程活着
            # 但端口不通 / 心跳过期时它们恒不触发，服务会一直躺着。
            # 这里先做判定：命中就强制结束，清掉冷却，让下一轮正常拉起。
            srv_present = (_child_alive(_server_proc)
                           or _proc_running("server.py"))
            srv_stuck = _stuck_seconds(
                "server", (not server_up) and srv_present, now)
            if srv_stuck > SERVER_STUCK_SEC:
                _log("server 进程在但 %ds 未监听 %d，判定卡死，强制重启"
                     % (int(srv_stuck), SERVER_PORT))
                killed = _kill_script(_server_proc, "server.py")
                _log("  已结束 pid=%s" % (killed or "(未找到)"))
                _server_proc = None
                _stuck_seconds("server", False, now)
                _server_grace_until = 0.0
                time.sleep(CHECK_INTERVAL)
                continue

            dmn_present = (_child_alive(_daemon_proc)
                           or _proc_running("daemon.py"))
            dmn_stuck = _stuck_seconds(
                "daemon", (not _daemon_alive()) and dmn_present, now)
            if dmn_stuck > DAEMON_STUCK_SEC:
                _log("daemon 进程在但心跳过期已 %ds，判定卡死，强制重启"
                     % int(dmn_stuck))
                killed = _kill_script(_daemon_proc, "daemon.py")
                _log("  已结束 pid=%s" % (killed or "(未找到)"))
                _daemon_proc = None
                _stuck_seconds("daemon", False, now)
                _daemon_grace_until = 0.0
                time.sleep(CHECK_INTERVAL)
                continue

            # ── server ──
            if (not server_up and now >= _server_grace_until
                    and not _child_alive(_server_proc) and not _proc_running("server.py")):
                _log("server 未运行，拉起 server.py")
                _server_proc = _spawn(["server.py"], ROOT / "svr.log")
                # 冷启动可能要几分钟（嵌入模型 + 向量库），冷却给到 300s
                _server_grace_until = now + 300

            # ── daemon ──（仅在 server 就绪后再拉起，避免两进程同时加载嵌入模型互相拖垮）
            elif (server_up and now >= _daemon_grace_until and not _daemon_alive()
                    and not _child_alive(_daemon_proc) and not _proc_running("daemon.py")):
                _log("daemon 未运行（心跳缺失/过期），拉起 daemon.py")
                # daemon 自身写 daemon.log，这里丢弃 stdout 避免双写/tqdm 刷屏
                # stdout 仍丢弃（daemon 自己写 daemon.log，且 tqdm 会刷屏），
                # 但 stderr 落盘 —— 崩溃 traceback 只走 stderr，丢了就查不出死因。
                _daemon_proc = _spawn(["daemon.py"], None,
                                      errfile=ROOT / "daemon_crash.log")
                _daemon_grace_until = now + 180
        except Exception as e:
            _log(f"巡检异常: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
