"""WPP 回调 v2 签名边界（fail-closed）。

WPP relay 发送::

    X-Webhook-Signature: hex(HMAC-SHA256(secret, f"{TsMs}.{DeliveryID}." + body))
    X-Webhook-Timestamp-Ms / X-Webhook-Delivery
    X-Webhook-Signature-Version: v2

校验必须在读完原始请求体之后做；时间戳默认 ±300s（``WPP_WEBHOOK_MAX_SKEW_SEC``）。
"""

import hashlib
import hmac
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

adapter_mod = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_adapter")
msgs = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_messages")

BODY = '{"Data":{"Content":"hi"}}'
SECRET = "s3cret"
TS = 1_760_000_000_000
DELIVERY = "pad-delivery-1"


def _sig(body: str = BODY, ts: int = TS, delivery: str = DELIVERY, secret: str = SECRET) -> str:
    base = f"{ts}.{delivery}.".encode() + body.encode("utf-8")
    return hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def _headers(**overrides):
    h = {
        "X-Webhook-Signature": _sig(),
        "X-Webhook-Timestamp-Ms": str(TS),
        "X-Webhook-Delivery": DELIVERY,
        "X-Webhook-Signature-Version": "v2",
    }
    h.update(overrides)
    return h


class TestWppV2Signature(unittest.TestCase):
    def setUp(self):
        # 固定时间戳在 ±300s 内，避免用例随时间腐烂。
        self._now = patch.object(adapter_mod.time, "time", return_value=TS / 1000.0)
        self._now.start()
        self.addCleanup(self._now.stop)
        self.verify = adapter_mod.WppPlatformAdapter._verify_wpp_v2_signature

    def test_valid_signature_accepted(self):
        self.assertTrue(self.verify(_headers(), BODY, SECRET))

    def test_tampered_body_rejected(self):
        self.assertFalse(self.verify(_headers(), BODY + " ", SECRET))

    def test_wrong_secret_rejected(self):
        self.assertFalse(self.verify(_headers(), BODY, "wrong"))

    def test_missing_or_wrong_delivery_rejected(self):
        self.assertFalse(self.verify(_headers(**{"X-Webhook-Delivery": ""}), BODY, SECRET))
        self.assertFalse(
            self.verify(_headers(**{"X-Webhook-Delivery": "other"}), BODY, SECRET)
        )

    def test_missing_signature_or_timestamp_rejected(self):
        self.assertFalse(self.verify(_headers(**{"X-Webhook-Signature": ""}), BODY, SECRET))
        self.assertFalse(
            self.verify(_headers(**{"X-Webhook-Timestamp-Ms": ""}), BODY, SECRET)
        )

    def test_stale_timestamp_rejected(self):
        stale = TS - 301_000
        h = _headers(**{
            "X-Webhook-Timestamp-Ms": str(stale),
            "X-Webhook-Signature": _sig(ts=stale),
        })
        self.assertFalse(self.verify(h, BODY, SECRET))

    def test_future_timestamp_rejected(self):
        future = TS + 301_000
        h = _headers(**{
            "X-Webhook-Timestamp-Ms": str(future),
            "X-Webhook-Signature": _sig(ts=future),
        })
        self.assertFalse(self.verify(h, BODY, SECRET))

    def test_unknown_signature_version_rejected(self):
        self.assertFalse(
            self.verify(_headers(**{"X-Webhook-Signature-Version": "v3"}), BODY, SECRET)
        )

    def test_skew_can_be_disabled(self):
        stale = TS - 86_400_000
        h = _headers(**{
            "X-Webhook-Timestamp-Ms": str(stale),
            "X-Webhook-Signature": _sig(ts=stale),
        })
        with patch.dict(os.environ, {"WPP_WEBHOOK_MAX_SKEW_SEC": "0"}):
            self.assertTrue(self.verify(h, BODY, SECRET))


class TestFilehelperDirection(unittest.TestCase):
    def test_recipient_or_conversation_matches(self):
        self.assertTrue(msgs.src_is_filehelper({"recipient_id": "filehelper"}))
        self.assertTrue(msgs.src_is_filehelper({"conversation_id": "filehelper"}))

    def test_sender_only_does_not_match(self):
        """文件助手发来的消息不是本机命令。"""
        self.assertFalse(msgs.src_is_filehelper({"sender_id": "filehelper"}))
        self.assertFalse(
            msgs.src_is_filehelper(
                {"sender_id": "filehelper", "recipient_id": "wxid_bot", "conversation_id": "wxid_bot"}
            )
        )


if __name__ == "__main__":
    unittest.main()
