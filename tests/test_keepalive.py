"""保活/在线状态日志回归测试。

旧版在 vendor 未登录时每 30s 打一条
``保活检查失败: WPP API error code=-1: /Login/AutoHeartBeat``，
重启后会连续刷屏十几分钟。这里锁定 v0.15.1 的行为：

  1. Code=-1（未登录/缓存缺失）不调用 AutoHeartBeat，只告警一次；
  2. 状态未变化时不重复输出 WARNING；
  3. 已登录但长连接未建立时触发心跳，并在 5 分钟内退避；
  4. 个人资料最多每 10 分钟刷新一次，状态循环不反复打 vendor。
"""

import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

mod = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_adapter")
WppPlatformAdapter = mod.WppPlatformAdapter


def make_adapter() -> WppPlatformAdapter:
    return WppPlatformAdapter(
        {"id": "wpp-keepalive", "wpp_gateway_url": "https://wpp.test/api/v1/gateway", "wpp_gateway_token": "tok"}, {}, asyncio.Queue()
    )


class TestKeepaliveLogging(unittest.IsolatedAsyncioTestCase):
    async def test_logged_out_skips_heartbeat_and_warns_once(self):
        adapter = make_adapter()
        acct = adapter.account
        acct._api = SimpleNamespace(
            is_online=AsyncMock(
                return_value=(False, {"Code": -1, "Message": "未登录或缓存缺失"})
            ),
            auto_heartbeat=AsyncMock(),
        )

        with patch.object(mod.logger, "warning") as warn:
            await adapter._check_account_online(acct)
            await adapter._check_account_online(acct)
            await adapter._check_account_online(acct)

        acct._api.auto_heartbeat.assert_not_awaited()
        self.assertEqual(warn.call_count, 1, "未登录状态只应告警一次")

    async def test_api_error_is_rate_limited_by_state(self):
        adapter = make_adapter()
        acct = adapter.account
        acct._api = SimpleNamespace(
            is_online=AsyncMock(
                return_value=(False, {"Code": -3, "Message": "网络错误"})
            ),
            auto_heartbeat=AsyncMock(),
        )

        with patch.object(mod.logger, "warning") as warn:
            await adapter._check_account_online(acct)
            await adapter._check_account_online(acct)

        self.assertEqual(warn.call_count, 1)
        acct._api.auto_heartbeat.assert_not_awaited()

    async def test_heartbeat_is_backed_off_for_five_minutes(self):
        adapter = make_adapter()
        acct = adapter.account
        acct._api = SimpleNamespace(
            is_online=AsyncMock(
                return_value=(False, {"Code": 0, "Message": "未连接"})
            ),
            auto_heartbeat=AsyncMock(return_value={"Message": "自动心跳已启动"}),
        )

        with (
            patch.object(mod.asyncio, "sleep", AsyncMock()),
            patch.object(mod.logger, "info"),
        ):
            await adapter._check_account_online(acct)
            await adapter._check_account_online(acct)

        self.assertEqual(acct._api.auto_heartbeat.await_count, 1)

    async def test_recovery_logs_once_and_clears_backoff(self):
        adapter = make_adapter()
        acct = adapter.account
        acct._api = SimpleNamespace(
            is_online=AsyncMock(
                side_effect=[
                    (False, {"Code": -1, "Message": "未登录"}),
                    (True, {"Code": 0, "Data": {"wxid": "wxid_bot", "online": True}}),
                ]
            ),
            auto_heartbeat=AsyncMock(),
            get_contract_profile=AsyncMock(
                return_value={
                    "Success": True,
                    "Data": {"userInfo": {"NickName": {"string": "小助理"}}},
                }
            ),
        )

        with patch.object(mod.logger, "warning"), patch.object(mod.logger, "info") as info:
            await adapter._check_account_online(acct)
            await adapter._check_account_online(acct)

        self.assertTrue(any("恢复在线" in str(c) for c in info.call_args_list))
        self.assertNotIn(acct.account_id, adapter._keepalive_next_attempt)
        self.assertEqual(acct._account_status["nickname"], "小助理")


class TestProfileRefreshThrottle(unittest.IsolatedAsyncioTestCase):
    async def test_profile_is_fetched_at_most_every_ten_minutes(self):
        adapter = make_adapter()
        acct = adapter.account
        calls = []

        async def profile():
            calls.append(1)
            return {
                "Success": True,
                "Data": {"userInfo": {"NickName": {"string": "小助理"}}},
            }

        acct._api = SimpleNamespace(get_contract_profile=profile)
        online_resp = {
            "Code": 0,
            "Data": {"wxid": "wxid_bot", "online": True},
        }

        await adapter._refresh_account_status(acct, online_resp)
        await adapter._refresh_account_status(acct, online_resp)
        self.assertEqual(len(calls), 1)

        acct._profile_refreshed_at -= 601
        await adapter._refresh_account_status(acct, online_resp)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
