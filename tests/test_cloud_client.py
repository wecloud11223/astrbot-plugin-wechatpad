"""WPP Cloud 客户端契约：网关 URL / Bearer / 控制面边界。

不触网：只检查 ``_url`` / ``_headers`` 与本地 block 列表。WPP 网关本身也会
拦截同样的控制面；本地先挡是为了给出可行动的错误，而不是 403。
"""

import importlib
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

client_mod = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_client")


def _client() -> "client_mod.WppClient":
    return client_mod.WppClient(
        "https://wpp.test/api/v1/gateway", "wxa_test", cloud_mode=True
    )


class TestCloudTransport(unittest.TestCase):
    def test_url_has_no_api_prefix_and_no_authcode_query(self):
        c = _client()
        self.assertEqual(
            c._url("/Msg/SendTxt"),
            "https://wpp.test/api/v1/gateway/Msg/SendTxt",
        )
        self.assertNotIn("authcode", c._url("/Msg/GetSelfInfo"))

    def test_headers_use_bearer_only(self):
        h = _client()._headers()
        self.assertEqual(h["Authorization"], "Bearer wxa_test")
        self.assertNotIn("X-Access-Token", h)
        self.assertNotIn("TokenKey", h)

    def test_sync_message_body_uses_cursor(self):
        c = _client()
        captured = {}

        async def fake_post(endpoint, body, **kwargs):
            captured["endpoint"] = endpoint
            captured["body"] = body
            return {"Code": 0, "Data": {}}

        c._post = fake_post  # type: ignore[assignment]
        import asyncio

        asyncio.run(c.sync_message("K1"))
        self.assertEqual(captured["endpoint"], "/Msg/Sync")
        self.assertEqual(captured["body"], {"Scene": 0, "Synckey": "K1"})


class TestCloudPolicy(unittest.TestCase):
    def test_control_plane_blocked(self):
        c = _client()
        for ep in (
            "/Admin/GetAllUsers",
            "/Webhook/Set",
            "/Webhook/Business/Set",
            "/Runtime/Info",
            "/Customized/WXCTDUniftyAuthBatch",
            "/Login/GetQR",
            "/Login/Logout",
            "/Tools/SetProxy",
            "/Tools/GetBoundHardDevices",
            "/Msg/StartAutoSync",
            "/Msg/StopAutoSync",
        ):
            with self.subTest(ep=ep):
                with self.assertRaises(client_mod.WppApiError) as ctx:
                    c._url(ep)
                self.assertEqual(ctx.exception.code, "cloud_blocked")

    def test_business_and_login_status_allowed(self):
        c = _client()
        for ep in (
            "/Login/GetLoginStatus",
            "/Login/LongLinkStatus",
            "/Login/HeartBeatLong",
            "/Login/AutoHeartBeat",
            "/Msg/Sync",
            "/Msg/GetSelfInfo",
            "/User/GetOnlineInfo",
            "/User/GetContractProfile",
            "/Tools/DownloadImg",
        ):
            with self.subTest(ep=ep):
                self.assertTrue(
                    c._url(ep).startswith("https://wpp.test/api/v1/gateway/"), ep
                )

    def test_vendor_mode_is_only_for_contrast(self):
        c = client_mod.WppClient(
            "https://vendor.test", "acct-1", cloud_mode=False
        )
        self.assertEqual(
            c._url("/Msg/SendTxt"),
            "https://vendor.test/api/Msg/SendTxt?authcode=acct-1",
        )
        h = c._headers()
        self.assertEqual(h["X-Access-Token"], "acct-1")
        self.assertNotIn("Authorization", h)


if __name__ == "__main__":
    unittest.main()
