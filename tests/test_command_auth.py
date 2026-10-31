"""filehelper 管理命令的准入闸门 + 并发去重回归。

背景（评审 P0）：
  - 回调 URL 公网可达（统一 Webhook 只按 uuid 路由、legacy 只校验 URL token），
    旧实现"没有 direction 就当自己发的" → 任何能访问回调的人都能执行
    /user add、/group add、/welcome off 改写白名单。
  - 去重是"先检查后记录"，而记录发生在媒体下载（最长 120s）之后，且
    _process_src 由 worker / /Msg/Sync / webhook 三处并发调用 → 同一条消息
    可被提交两次（两次 AI 回复、命令执行两次）。
"""

import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

account_mod = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_account")
smoke = importlib.import_module("test_mainpath_smoke")


def _filehelper_src(msg_id, content="/help", **extra):
    src = {
        "sender_id": "filehelper",
        "recipient_id": "filehelper",
        "msg_id": msg_id,
        "content": content,
    }
    src.update(extra)
    return src


class TestFilehelperCommandGate(unittest.TestCase):
    def setUp(self):
        self._store_patch = patch.object(account_mod, "AccountStore", smoke.StubStore)
        self._client_patch = patch.object(account_mod, "WppClient", smoke.FakeClient)
        self._store_patch.start()
        self._client_patch.start()
        self.addCleanup(self._store_patch.stop)
        self.addCleanup(self._client_patch.stop)

    def new_acc(self):
        acc = account_mod.WppAccount(
            smoke.Plat(),
            "stub",
            "authcode",
            "http://127.0.0.1:1",
            "",
            allow_users="q123456789",
            group_reply="atbot",
            proactive_groups="",
            require_at_mention=False,
            private_allow_all=False,
        )
        acc._self_wxid = "wxid_bot"
        acc._self_nickname = "小助手"
        return acc

    def run_src(self, acc, src, *, authenticated=False):
        asyncio.run(acc._process_src(src, authenticated=authenticated))
        return acc

    def test_unauthenticated_command_is_rejected(self):
        acc = self.run_src(
            self.new_acc(), _filehelper_src("c1", direction="outgoing")
        )
        self.assertEqual(acc._api.texts, [])
        self.assertEqual(acc.platform.events, [])
        # 拒绝后仍记为已处理, 重推时不必反复尝试
        self.assertIn("c1", acc._seen_msg_ids)

    def test_inbound_direction_is_rejected(self):
        acc = self.run_src(
            self.new_acc(),
            _filehelper_src("c2", direction="incoming"),
            authenticated=True,
        )
        self.assertEqual(acc._api.texts, [])

    def test_unknown_direction_is_rejected(self):
        acc = self.run_src(
            self.new_acc(),
            _filehelper_src("c3", direction="sideways"),
            authenticated=True,
        )
        self.assertEqual(acc._api.texts, [])

    def test_authenticated_outgoing_runs_command(self):
        acc = self.run_src(
            self.new_acc(),
            _filehelper_src("c4", direction="outgoing"),
            authenticated=True,
        )
        self.assertTrue(acc._api.texts, "命令应当执行")
        self.assertIn("WPP 帮助", acc._api.texts[-1][1])

    def test_authenticated_self_sender_runs_command(self):
        acc = self.run_src(
            self.new_acc(),
            {
                "sender_id": "wxid_bot",
                "recipient_id": "filehelper",
                "msg_id": "c5",
                "content": "/help",
            },
            authenticated=True,
        )
        self.assertTrue(acc._api.texts)
        self.assertIn("WPP 帮助", acc._api.texts[-1][1])

    def test_authenticated_without_origin_proof_is_rejected(self):
        """已认证但既无 direction、发送者也不是自己 → 仍拒绝（伪造投递）。"""
        acc = self.run_src(
            self.new_acc(), _filehelper_src("c6"), authenticated=True
        )
        self.assertEqual(acc._api.texts, [])

    def test_synced_item_counts_as_authenticated(self):
        acc = self.new_acc()
        asyncio.run(
            acc._process_synced_item(_filehelper_src("c7", direction="outgoing"))
        )
        self.assertTrue(acc._api.texts, "/Msg/Sync 是认证通道")


class TestConcurrentDedup(unittest.TestCase):
    def setUp(self):
        self._store_patch = patch.object(account_mod, "AccountStore", smoke.StubStore)
        self._client_patch = patch.object(account_mod, "WppClient", smoke.FakeClient)
        self._store_patch.start()
        self._client_patch.start()
        self.addCleanup(self._store_patch.stop)
        self.addCleanup(self._client_patch.stop)

    def new_acc(self):
        acc = account_mod.WppAccount(
            smoke.Plat(),
            "stub",
            "authcode",
            "http://127.0.0.1:1",
            "",
            allow_users="wxid_private",
            group_reply="atbot",
            proactive_groups="",
            require_at_mention=False,
            private_allow_all=True,
        )
        acc._self_wxid = "wxid_bot"
        acc._self_nickname = "小助手"
        return acc

    @staticmethod
    def _private_src(msg_id):
        return {
            "sender_id": "wxid_private",
            "recipient_id": "wxid_bot",
            "msg_id": msg_id,
            "content": "你好",
            "type": 1,
        }

    def test_same_message_concurrently_committed_once(self):
        acc = self.new_acc()

        async def _twice():
            await asyncio.gather(
                acc._process_src(self._private_src("m1")),
                acc._process_src(dict(self._private_src("m1"))),
            )

        asyncio.run(_twice())
        self.assertEqual(len(acc.platform.events), 1)

    def test_second_delivery_after_completion_is_skipped(self):
        acc = self.new_acc()
        asyncio.run(acc._process_src(self._private_src("m2")))
        asyncio.run(acc._process_src(self._private_src("m2")))
        self.assertEqual(len(acc.platform.events), 1)

    def test_inflight_marks_are_released(self):
        acc = self.new_acc()

        async def _boom():
            with patch.object(acc, "_process_src_inner", side_effect=RuntimeError("x")):
                with self.assertRaises(RuntimeError):
                    await acc._process_src(self._private_src("m3"))

        asyncio.run(_boom())
        self.assertEqual(acc._inflight_keys, set())
        # 失败的消息不应被记为已处理 → 重推可以重试
        self.assertNotIn("m3", acc._seen_msg_ids)


if __name__ == "__main__":
    unittest.main()
