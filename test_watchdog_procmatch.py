#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""watchdog 的进程判据必须按 .py 文件名精确比对。

这是本项目反复出问题的地方，值得钉死：
  * 旧实现 `return script in out` 让 `_proc_running("server.py")`
    被 **mcp_server.py** 满足。本机常驻 10 个 mcp_server 会话，
    该判据实测恒为 True，于是 server 挂掉后拉起分支永远不成立。
  * 同类错误我在运维脚本里连踩三次：`_restart_server.py` 把自己杀了、
    `_start_daemon.py` 因文件名含 `daemon.py` 把自己误判成 daemon。

用 stub 替掉 subprocess.run，构造各种命令行组合验证判据。
不启进程、不碰真实系统。

用法：python test_watchdog_procmatch.py
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import sys
import types
import unittest

sys.path.insert(0, ROOT)
import watchdog  # noqa: E402


def fake_wmic(lines):
    """把 wmic 输出替换成给定命令行列表。"""
    def _run(*a, **k):
        return types.SimpleNamespace(stdout="CommandLine\n" + "\n".join(lines) + "\n")
    return _run


MCP = r"python  -X utf8 -u mcp_server.py"
SRV = r"C:\Python313\python.exe server.py"
DMN = r"C:\Python313\pythonw.exe daemon.py"
WD = r"C:\Python313\pythonw.exe watchdog.py"
MYSCRIPT = r"python -X utf8 _start_daemon.py"


class ScriptNamesTest(unittest.TestCase):

    def test_basename_lowercased(self):
        self.assertEqual(watchdog._script_names(r"C:\X\Server.PY foo"), {"server.py"})

    def test_multiple(self):
        self.assertEqual(watchdog._script_names("python a.py b.py"), {"a.py", "b.py"})

    def test_mcp_not_confused_with_server(self):
        names = watchdog._script_names(MCP)
        self.assertIn("mcp_server.py", names)
        self.assertNotIn("server.py", names)      # 这就是旧实现搞错的地方

    def test_underscore_prefixed_not_confused(self):
        names = watchdog._script_names(MYSCRIPT)
        self.assertIn("_start_daemon.py", names)
        self.assertNotIn("daemon.py", names)


class ProcRunningTest(unittest.TestCase):

    def setUp(self):
        self._orig = watchdog.subprocess.run

    def tearDown(self):
        watchdog.subprocess.run = self._orig

    def use(self, lines):
        watchdog.subprocess.run = fake_wmic(lines)

    def test_only_mcp_running_means_server_not_running(self):
        """核心回归：10 个 MCP 会话在跑，不代表 API server 在跑。"""
        self.use([MCP] * 10)
        self.assertFalse(watchdog._proc_running("server.py"))

    def test_real_server_detected(self):
        self.use([MCP] * 10 + [SRV])
        self.assertTrue(watchdog._proc_running("server.py"))

    def test_daemon_detected_even_as_pythonw(self):
        self.use([DMN])
        self.assertTrue(watchdog._proc_running("daemon.py"))

    def test_helper_script_not_mistaken_for_daemon(self):
        self.use([MYSCRIPT])
        self.assertFalse(watchdog._proc_running("daemon.py"))

    def test_watchdog_itself_excluded(self):
        self.use([WD])
        self.assertFalse(watchdog._proc_running("watchdog.py"))

    def test_empty_output(self):
        self.use([])
        self.assertFalse(watchdog._proc_running("server.py"))

    def test_probe_failure_reports_not_running(self):
        """探测失败宁可认为没在跑：多拉一次由 grace 兜住，漏拉会一直不可用。"""
        def boom(*a, **k):
            raise OSError("wmic gone")
        watchdog.subprocess.run = boom
        self.assertFalse(watchdog._proc_running("server.py"))

    def test_path_prefixed_target(self):
        self.use([SRV])
        self.assertTrue(watchdog._proc_running(r"server.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
