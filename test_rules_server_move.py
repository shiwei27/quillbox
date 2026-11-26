#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""规则的服务端移动：开关必须真的管用（离线，纳入回归）。

最要紧的一条是**开关关闭时绝不碰服务器**。这是持续自动写入邮箱的闸门，
"我以为它是关的"和"它确实是关的"之间的差别，就是一个写错的规则会不会
在无人看着的时候一直错下去。所以用 mock 替掉 server_move，断言它一次都没被调用。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import io
import os
import re
import sys
import unittest
from unittest import mock

os.chdir(ROOT)
sys.path.insert(0, ROOT)


def read(p):
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


class FakeDB:
    def __init__(self):
        self.moved = []
        self.read_marked = []

    def move_folder(self, eid, fld):
        self.moved.append((eid, fld))

    def mark_read(self, eid, v=True):
        self.read_marked.append(eid)

    def star_email(self, eid, v=True):
        pass

    def bulk_update_category(self, ids, cat):
        pass

    def update_ai_fields(self, *a, **kw):
        pass


class FakeAI:
    def __init__(self, rule):
        self._rule = rule

    def _match_rule(self, e):
        return self._rule


RULE = {"name": "示例告警通知转移", "enabled": True,
        "actions": {"folder": "告警通知", "mark_read": True}}


def mails(n=2):
    return [{"id": "ews_%d" % i, "account": "alice@example.org",
             "folder": "收件箱", "message_id": "mid-%d@x" % i,
             "subject": "告警 %d" % i} for i in range(n)]


class TestSwitchOff(unittest.TestCase):
    """开关关闭（默认）时：本地照改，服务器一次都不碰。"""

    def test_local_move_still_happens(self):
        import daemon
        db = FakeDB()
        daemon._apply_rules(mails(2), FakeAI(RULE), db, {"rules": {}})
        self.assertEqual([f for _, f in db.moved], ["告警通知", "告警通知"])

    def test_server_module_never_called(self):
        import daemon
        with mock.patch.dict(sys.modules):
            fake = mock.MagicMock()
            fake.DEFAULT_MAX = 50
            fake.MoveError = Exception
            sys.modules["core.server_move"] = fake
            daemon._apply_rules(mails(3), FakeAI(RULE), FakeDB(), {"rules": {}})
            fake.move_batch.assert_not_called()

    def test_absent_config_is_treated_as_off(self):
        """p 里根本没有 rules 段时也必须当成关闭 —— 缺配置不能等于开启。"""
        import daemon
        with mock.patch.dict(sys.modules):
            fake = mock.MagicMock()
            fake.DEFAULT_MAX = 50
            fake.MoveError = Exception
            sys.modules["core.server_move"] = fake
            daemon._apply_rules(mails(1), FakeAI(RULE), FakeDB(), {})
            fake.move_batch.assert_not_called()
            daemon._apply_rules(mails(1), FakeAI(RULE), FakeDB(), None)
            fake.move_batch.assert_not_called()

    @unittest.skipUnless(_os.path.exists(_os.path.join(ROOT, "config.yaml")), "no config.yaml (fresh checkout)")
    def test_config_sets_the_switch_explicitly(self):
        """配置里必须**显式**写出这个开关，取值由用户决定。

        不断言它必须是 false：那会把"代码默认值必须是关闭"和"当前配置取值"
        混为一谈，用户打开后就成了假失败。
        "代码默认关闭"由 test_absent_config_is_treated_as_off 覆盖（缺配置即关闭），
        这里只要求它是显式的布尔值：不能靠"忘了写"来决定要不要写邮箱。
        """
        import yaml
        y = yaml.safe_load(read("config.yaml"))
        v = (y.get("rules") or {}).get("apply_on_server")
        self.assertIsInstance(v, bool,
                              "rules.apply_on_server 必须显式写成布尔值，实际 %r" % (v,))
        print("\n      （当前配置：apply_on_server = %r）" % v)


class TestSwitchOn(unittest.TestCase):
    """开关打开时：按 (账户, 目标) 攒批调用，且传 apply=True。"""

    def _run(self, n, rule=RULE):
        import daemon
        fake = mock.MagicMock()
        fake.DEFAULT_MAX = 50
        fake.MoveError = type("MoveError", (Exception,), {})
        fake.move_batch.return_value = {
            "moved": ["x"], "already_there": [], "not_found": [], "failed": []}
        with mock.patch.dict(sys.modules):
            sys.modules["core.server_move"] = fake
            daemon._apply_rules(mails(n), FakeAI(rule), FakeDB(),
                                {"rules": {"apply_on_server": True}})
        return fake

    def test_called_with_apply_true(self):
        fake = self._run(2)
        self.assertTrue(fake.move_batch.called)
        self.assertIs(fake.move_batch.call_args.kwargs.get("apply"), True)

    def test_batched_not_per_email(self):
        """3 封应当一次调用，而不是三次 —— 逐封建连接对服务器不友好。"""
        fake = self._run(3)
        self.assertEqual(fake.move_batch.call_count, 1)

    def test_chunked_by_module_limit(self):
        """超过模块上限要切片，而不是把 120 封一次丢过去让它抛错。"""
        fake = self._run(120)
        self.assertEqual(fake.move_batch.call_count, 3)

    def test_no_folder_action_means_no_server_call(self):
        fake = self._run(2, rule={"name": "只标已读", "enabled": True,
                                  "actions": {"mark_read": True}})
        fake.move_batch.assert_not_called()

    def test_move_error_does_not_raise(self):
        """IMAP 会抛 MoveError（未实现），不能让本轮同步挂掉。"""
        import daemon
        fake = mock.MagicMock()
        fake.DEFAULT_MAX = 50

        class ME(Exception):
            pass
        fake.MoveError = ME
        fake.move_batch.side_effect = ME("IMAP 暂不支持")
        with mock.patch.dict(sys.modules):
            sys.modules["core.server_move"] = fake
            db = FakeDB()
            daemon._apply_rules(mails(2), FakeAI(RULE), db,
                                {"rules": {"apply_on_server": True}})
        # 本地移动仍然完成：服务端失败不该拖掉本地
        self.assertEqual(len(db.moved), 2)


class TestGuardsStillInModule(unittest.TestCase):
    """接线不得绕过 server_move 里的护栏。"""

    def setUp(self):
        self.d = read("daemon.py")
        i = self.d.find("def _apply_rules_on_server")
        self.body = self.d[i:self.d.find("\ndef ", i + 10)]

    def test_goes_through_move_batch(self):
        self.assertIn("move_batch", self.body)

    def test_does_not_call_ews_directly(self):
        """不能在 daemon 里自己建 EWS 连接绕过护栏。"""
        for bad in ("exchangelib", "move_to(", "_ews_account", "item.move"):
            self.assertNotIn(bad, self.body,
                             "daemon 里直接操作 EWS（%s）会绕过 server_move 的护栏" % bad)

    def test_local_move_before_server(self):
        """本地先改、服务端后做：本地是用户立刻能看到的那一层。"""
        i = self.d.find("def _apply_rules")
        seg = self.d[i:i + 3000]
        self.assertLess(seg.find("db.move_folder"),
                        seg.find("pending_server.setdefault"),
                        "应当先改本地库，再把服务端移动攒进待办")


if __name__ == "__main__":
    unittest.main(verbosity=2)
