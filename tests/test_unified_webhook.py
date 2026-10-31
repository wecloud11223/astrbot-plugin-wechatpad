"""多账号回调入口回归：统一 Webhook + 独立监听共享端口。

钉住两个曾把第二个账号实例直接打挂的问题：

  1. 新建实例的配置表单里根本没有 webhook 相关字段 —— AstrBot 用
     ``default_config_tmpl`` 生成新建表单，旧模板只有 wpp_* 键，于是新实例
     「无法填写 webhook」。现在模板必须带 ``unified_webhook_mode`` /
     ``webhook_uuid`` / ``webhook_public_url`` / ``webhook_secret`` 等键与元数据。
  2. 每个实例各自 bind ``0.0.0.0:6199`` → ``[Errno 98] address already in use``
     → ``run()`` 抛错 → 平台被标 ERROR、enable 回退 false。现在默认走 AstrBot
     统一 Webhook（不占端口、按 uuid 路由）；legacy 模式同一端口上的多个实例
     复用同一个 aiohttp server，按各自的回调路径分发。
"""

import asyncio
import hashlib
import hmac
import importlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

mod = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_adapter")
WppPlatformAdapter = mod.WppPlatformAdapter

GATEWAY = "https://wpp.test/api/v1/gateway"


def make_adapter(instance_id="wpp-u1", token="tok-u1", **overrides):
    cfg = {
        "id": instance_id,
        "wpp_gateway_url": GATEWAY,
        "wpp_gateway_token": token,
    }
    cfg.update(overrides)
    return WppPlatformAdapter(cfg, {}, asyncio.Queue())


class FakeRequest:
    """模拟 AstrBot 的 DashboardRequest（只用到这几个属性）。"""

    def __init__(self, method="POST", body=b"", headers=None, path=""):
        self.method = method
        self._body = body
        self.headers = headers or {}
        self.path = path

    async def get_data(self):
        return self._body


SECRET = "unit-test-secret"


def signed_headers(body: str, secret: str, *, delivery="d-1") -> dict:
    ts = int(time.time() * 1000)
    base = f"{ts}.{delivery}.".encode() + body.encode("utf-8")
    return {
        "X-Webhook-Signature": hmac.new(
            secret.encode("utf-8"), base, hashlib.sha256
        ).hexdigest(),
        "X-Webhook-Timestamp-Ms": str(ts),
        "X-Webhook-Delivery": delivery,
        "X-Webhook-Signature-Version": "v2",
    }


class TestConfigTemplate(unittest.TestCase):
    def _template(self):
        """注册表里保存的就是 AstrBot 新建实例时用的那份 default_config_tmpl。"""
        from astrbot.core.platform.register import platform_registry

        pm = next(p for p in platform_registry if p.name == mod.PLATFORM_NAME)
        return pm.default_config_tmpl or {}

    def test_template_exposes_webhook_fields(self):
        """新建实例的表单必须有 webhook 字段，否则用户无处填写。"""
        tmpl = self._template()
        for key in (
            "unified_webhook_mode",
            "webhook_uuid",
            "webhook_public_url",
            "webhook_secret",
            "webhook_host",
            "webhook_port",
            "webhook_path",
            "webhook_token",
        ):
            self.assertIn(key, tmpl, f"default_config_tmpl 缺少 {key}")
        self.assertTrue(tmpl["unified_webhook_mode"], "多账号场景应默认开统一 Webhook")

    def test_metadata_documents_every_webhook_field(self):
        meta = mod.WPP_CONFIG_METADATA
        for key in (
            "unified_webhook_mode",
            "webhook_uuid",
            "webhook_public_url",
            "webhook_secret",
            "webhook_host",
            "webhook_port",
            "webhook_path",
            "webhook_token",
        ):
            self.assertIn(key, meta, f"WPP_CONFIG_METADATA 缺少 {key}")
            self.assertTrue(meta[key].get("hint"), f"{key} 需要 hint")

    def test_legacy_instance_without_key_stays_legacy(self):
        """老实例配置里没有 unified_webhook_mode：不能悄悄换掉它的回调地址。"""
        adapter = make_adapter("wpp-old")
        self.assertFalse(adapter.unified_webhook_mode)
        self.assertFalse(adapter._use_unified_webhook())

    def test_new_instance_defaults_to_unified(self):
        adapter = make_adapter("wpp-new", unified_webhook_mode=True)
        self.assertTrue(adapter._use_unified_webhook())


class TestWebhookUuid(unittest.TestCase):
    def test_uuid_is_derived_and_stable(self):
        a = make_adapter("wpp-uuid", unified_webhook_mode=True)
        b = make_adapter("wpp-uuid", unified_webhook_mode=True, webhook_uuid="")
        self.assertEqual(a.webhook_uuid, b.webhook_uuid, "重启后地址必须不变")
        self.assertEqual(len(a.webhook_uuid), 16)
        int(a.webhook_uuid, 16)  # 纯 hex

    def test_uuid_differs_per_instance(self):
        a = make_adapter("wpp-uuid-a", unified_webhook_mode=True)
        b = make_adapter("wpp-uuid-b", unified_webhook_mode=True)
        self.assertNotEqual(a.webhook_uuid, b.webhook_uuid)

    def test_explicit_uuid_wins(self):
        adapter = make_adapter("wpp-uuid-c", unified_webhook_mode=True, webhook_uuid="deadbeefcafe0001")
        self.assertEqual(adapter.webhook_uuid, "deadbeefcafe0001")

    def test_uuid_written_back_to_platform_config(self):
        """AstrBot 的 find_platform_by_uuid()/unified_webhook() 读的是 platform.config。"""
        cfg = {
            "id": "wpp-writeback",
            "wpp_gateway_url": GATEWAY,
            "wpp_gateway_token": "tok-wb",
            "unified_webhook_mode": True,
        }
        adapter = WppPlatformAdapter(cfg, {}, asyncio.Queue())
        self.assertEqual(cfg["webhook_uuid"], adapter.webhook_uuid)
        self.assertTrue(adapter.unified_webhook(), "AstrBot 侧必须认为这是统一 Webhook 平台")
        self.assertTrue(adapter.get_stats()["unified_webhook"])

    def test_callback_path_uses_uuid(self):
        adapter = make_adapter("wpp-path", unified_webhook_mode=True)
        self.assertEqual(
            adapter.unified_webhook_path(),
            f"/api/platform/webhook/{adapter.webhook_uuid}",
        )


class TestUnifiedCallback(unittest.TestCase):
    def test_get_returns_ok(self):
        adapter = make_adapter("wpp-get", unified_webhook_mode=True)
        body, status = asyncio.run(adapter.webhook_callback(FakeRequest(method="GET")))
        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")

    def test_sync_notification_triggers_msg_sync(self):
        adapter = make_adapter("wpp-sync", unified_webhook_mode=True, webhook_secret="s3cret")
        calls = []
        adapter.account.request_sync = lambda reason: calls.append(reason)
        payload = '{"Type":"sync_message"}'
        body, status = asyncio.run(
            adapter.webhook_callback(
                FakeRequest(body=payload.encode(), headers=signed_headers(payload, "s3cret"))
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")
        self.assertEqual(calls, [mod.REASON_PUSH])

    def test_bad_signature_is_rejected(self):
        adapter = make_adapter("wpp-bad-sig", unified_webhook_mode=True, webhook_secret="s3cret")
        payload = '{"Type":"sync_message"}'
        headers = signed_headers(payload, "s3cret")
        headers["X-Webhook-Signature"] = "deadbeef"
        _, status = asyncio.run(
            adapter.webhook_callback(FakeRequest(body=payload.encode(), headers=headers))
        )
        self.assertEqual(status, 403)

    def test_missing_secret_fails_closed(self):
        """统一模式不配 secret = 谁都能伪造消息，必须 403 而不是放行。

        uuid 只是 sha256(实例 id) 派生出来的路由键，不是凭证；AstrBot 又把
        /api/platform/webhook* 排除在 dashboard 鉴权外。
        """
        adapter = make_adapter("wpp-nosecret", unified_webhook_mode=True)
        _, status = asyncio.run(
            adapter.webhook_callback(FakeRequest(body=b'{"Type":"sync_message"}', headers={}))
        )
        self.assertEqual(status, 403)

    def test_malformed_json_is_acked_with_secret(self):
        """坏请求重试也不会成功，回 200 避免 WPP 无意义重投。"""
        body = "not-json"
        adapter = make_adapter("wpp-badjson", unified_webhook_mode=True, webhook_secret=SECRET)
        _, status = asyncio.run(
            adapter.webhook_callback(
                FakeRequest(body=body.encode(), headers=signed_headers(body, SECRET))
            )
        )
        self.assertEqual(status, 200)

    def test_callback_url_prefers_public_url(self):
        adapter = make_adapter(
            "wpp-url",
            unified_webhook_mode=True,
            webhook_public_url="https://astrbot.example.com/",
        )
        self.assertEqual(
            adapter._callback_url(),
            f"https://astrbot.example.com/api/platform/webhook/{adapter.webhook_uuid}",
        )

    def test_callback_url_falls_back_to_path(self):
        adapter = make_adapter("wpp-url2", unified_webhook_mode=True)
        self.assertEqual(adapter._callback_url(), adapter.unified_webhook_path())

    def test_log_unified_webhook_formats_without_error(self):
        """日志格式串写错会抛 TypeError 并把整个 run() 打挂。

        astrbot 的 logger 是 stdlib logging（``%`` 占位），用 ``{}`` + 位置参数会在
        真正 emit 时抛 ``TypeError: not all arguments converted during string
        formatting``。这里用一个「不做异常兜底」的 handler 复现那条路径。
        """
        import logging

        class _StrictHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                # 故意不 try/except：格式化失败必须让测试红。
                self.messages.append(record.getMessage())

        handler = _StrictHandler()
        test_logger = logging.getLogger(f"wpp-test-{id(self)}")
        test_logger.handlers = [handler]
        test_logger.propagate = False
        test_logger.setLevel(logging.DEBUG)

        adapter = make_adapter(
            "wpp-log", unified_webhook_mode=True, webhook_public_url="https://a.example.com"
        )
        with (
            patch.object(mod, "logger", test_logger),
            patch.object(adapter, "_write_callback_url_file", lambda url: "/tmp/x.callback-url"),
        ):
            adapter._log_unified_webhook()

        joined = "\n".join(handler.messages)
        self.assertIn("/api/platform/webhook/", joined, "日志里应打印统一 webhook 地址")


class TestSharedLegacyServer(unittest.IsolatedAsyncioTestCase):
    """legacy 模式：同一 host:port 上多个实例只能 bind 一次。"""

    def _pair(self):
        a = make_adapter("wpp-share-a", token="tok-a", webhook_host="127.0.0.1", webhook_port=0)
        b = make_adapter("wpp-share-b", token="tok-b", webhook_host="127.0.0.1", webhook_port=0)
        return a, b

    async def asyncTearDown(self):
        for hub in list(mod._SHARED_WEBHOOK_SERVERS.values()):
            hub._owners.clear()
            await hub.release()

    async def test_two_instances_share_one_server(self):
        a, b = self._pair()
        await a._start_webhook_server()
        await b._start_webhook_server()
        self.assertIs(a._hub, b._hub, "第二个实例必须复用已启动的 server，否则 errno 98")
        self.assertEqual(len(mod._SHARED_WEBHOOK_SERVERS), 1)
        self.assertEqual(set(a._hub._owners), {a.webhook_path, b.webhook_path})

    async def test_release_keeps_server_alive_for_remaining_owner(self):
        a, b = self._pair()
        await a._start_webhook_server()
        await b._start_webhook_server()
        hub = a._hub
        hub.unregister(a.webhook_path)
        await hub.release()
        self.assertTrue(mod._SHARED_WEBHOOK_SERVERS, "还有实例在用，不能关掉 server")
        self.assertIsNotNone(hub._runner)

        hub.unregister(b.webhook_path)
        await hub.release()
        self.assertFalse(mod._SHARED_WEBHOOK_SERVERS, "最后一个实例退出后才关闭 server")
        self.assertIsNone(hub._runner)

    async def test_dispatch_by_path(self):
        a, b = self._pair()
        await a._start_webhook_server()
        await b._start_webhook_server()

        seen = []

        class DispatchRequest:
            def __init__(self, path, body):
                self.path = path
                self._body = body
                self.method = "POST"
                self.headers = {}

            async def text(self):
                return self._body.decode()

            async def get_data(self):
                return self._body

        b.account.request_sync = lambda reason: seen.append(b.instance_id)
        resp = await a._hub._dispatch_post(DispatchRequest(b.webhook_path, b'{"Type":"sync"}'))
        self.assertEqual(resp.status, 200)
        self.assertEqual(seen, [b.instance_id], "必须路由到路径对应的那个账号实例")

        resp = await a._hub._dispatch_post(DispatchRequest("/wpp-cloud/webhook/unknown", b"{}"))
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
