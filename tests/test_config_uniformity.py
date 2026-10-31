"""配置口径统一回归：新建表单与已有实例共用同一份键表。

用户要求（两件事，钉在这里）：

  1. 「添加机器人」列表里显示的名称是「个人微信(Pad协议)」。**注意它只是显示名**
     （``adapter_display_name``），平台技术名 ``PLATFORM_NAME`` 是配置里的 ``type``，
     改了会让已有实例找不到适配器，所以必须保持不变。
  2. 不管加多少台机器人，配置项集合 / 默认值 / 类型都要一致：新建实例用
     ``DEFAULT_CONFIG_TMPL`` 生成表单，老实例在启动时由
     ``normalize_platform_config`` 按同一份模板补齐缺键、纠正类型。
"""

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

mod = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_adapter")

GATEWAY = "https://wpp.test/api/v1/gateway"


def make_adapter(instance_id="wpp-u1", token="tok-u1", **overrides):
    cfg = {
        "id": instance_id,
        "wpp_gateway_url": GATEWAY,
        "wpp_gateway_token": token,
    }
    cfg.update(overrides)
    return mod.WppPlatformAdapter(cfg, {}, None)


def registry_template():
    from astrbot.core.platform.register import platform_registry

    pm = next(p for p in platform_registry if p.name == mod.PLATFORM_NAME)
    return pm


class TemplateConsistencyTest(unittest.TestCase):
    def test_template_keys_match_metadata_keys(self):
        """每个配置键都要有表单元数据，否则前端渲染不出来/无法填写。"""
        self.assertEqual(
            sorted(mod.DEFAULT_CONFIG_TMPL), sorted(mod.WPP_CONFIG_METADATA)
        )

    def test_registry_template_has_every_default_key(self):
        """注册到内核的模板必须覆盖全部键（新建机器人靠它生成表单）。"""
        tmpl = registry_template().default_config_tmpl or {}
        for key, default in mod.DEFAULT_CONFIG_TMPL.items():
            self.assertIn(key, tmpl, f"注册模板缺少配置键 {key}")
            self.assertEqual(tmpl[key], default, f"注册模板 {key} 默认值与常量不一致")
        for managed in ("type", "enable", "id"):
            self.assertIn(managed, tmpl)

    def test_template_constant_is_not_polluted_by_registration(self):
        """装饰器就地往模板里塞 type/enable/id；常量不能被污染，否则补不齐键。"""
        for managed in mod._TMPL_MANAGED_KEYS:
            self.assertNotIn(managed, mod.DEFAULT_CONFIG_TMPL)

    def test_display_name_is_personal_wechat_pad(self):
        self.assertEqual(mod.PLATFORM_DISPLAY_NAME, "个人微信(Pad协议)")
        self.assertEqual(registry_template().adapter_display_name, "个人微信(Pad协议)")

    def test_platform_type_name_matches_create_dialog_label(self):
        """创建机器人下拉渲染的是 config_template 的键（= adapter_name = type）。

        AstrBot 前端 ``AddNewPlatform`` 用 ``Object.keys(platformTemplates)``
        做选项、``item-title="name"``；对字符串 item，Vuetify 取不到属性时
        回退成 item 本身。所以「消息平台类别」显示的就是技术名，技术名必须
        等于用户要求看到的「个人微信(Pad协议)」。
        """
        self.assertEqual(mod.PLATFORM_NAME, "个人微信(Pad协议)")
        self.assertEqual(mod.PLATFORM_DISPLAY_NAME, mod.PLATFORM_NAME)
        self.assertEqual(registry_template().name, mod.PLATFORM_NAME)

    def test_legacy_platform_names_are_registered_as_aliases(self):
        """老实例的 type 还是历史技术名，升级后必须仍能加载（且不出现在创建下拉）。"""
        from astrbot.core.platform.register import (
            platform_cls_map,
            platform_registry,
        )

        for legacy in mod.LEGACY_PLATFORM_NAMES:
            self.assertIs(platform_cls_map.get(legacy), mod.WppPlatformAdapter)
            # 别名的模板必须为空：get_astrbot_config() 会跳过它，
            # 创建机器人下拉里才不会出现重复条目。
            entries = [p for p in platform_registry if p.name == legacy]
            self.assertEqual(entries, [])


class NormalizeConfigTest(unittest.TestCase):
    def test_fills_all_missing_keys_from_template(self):
        cfg = {"id": "legacy", "wpp_gateway_token": "wxa_x"}
        changes = mod.normalize_platform_config(cfg)
        self.assertEqual(
            sorted(set(cfg) - mod._TMPL_MANAGED_KEYS), sorted(mod.DEFAULT_CONFIG_TMPL)
        )
        self.assertIn("wpp_lazy_media", changes)
        self.assertEqual(cfg["wpp_lazy_media"], True)

    def test_missing_unified_webhook_stays_legacy(self):
        """缺键的老实例不能被无声切到统一模式（它登记的回调会立刻 404）。"""
        cfg = {"id": "legacy"}
        mod.normalize_platform_config(cfg)
        self.assertIs(cfg["unified_webhook_mode"], False)
        self.assertIs(mod._LEGACY_MISSING_DEFAULTS["unified_webhook_mode"], False)

    def test_coerces_types_and_clamps_bounds(self):
        cfg = {
            "id": "x",
            "wpp_private_allow_all": "false",
            "wpp_block_users": 123,
            "wpp_sync_interval": "1",
            "wpp_send_interval_ms": "-5",
            "wpp_outbound_repeat_limit": "0",
            "webhook_port": "99999",
            "webhook_host": "   ",
            "wpp_group_reply": "  atbot  ",
        }
        mod.normalize_platform_config(cfg)
        self.assertIs(cfg["wpp_private_allow_all"], False)
        self.assertEqual(cfg["wpp_block_users"], "123")
        self.assertEqual(cfg["wpp_sync_interval"], 5)  # 下限
        self.assertEqual(cfg["wpp_send_interval_ms"], 0)
        self.assertEqual(cfg["wpp_outbound_repeat_limit"], 1)
        self.assertEqual(cfg["webhook_port"], 65535)
        self.assertEqual(cfg["webhook_host"], "0.0.0.0")  # 空串回填默认值
        self.assertEqual(cfg["wpp_group_reply"], "atbot")

    def test_migrates_legacy_type_to_current_name(self):
        """老实例的 type 是历史技术名：启动后自动迁移成当前名，面板才显示新名字。"""
        for legacy in mod.LEGACY_PLATFORM_NAMES:
            cfg = {"id": "old-bot", "type": legacy}
            changes = mod.normalize_platform_config(cfg)
            self.assertEqual(cfg["type"], mod.PLATFORM_NAME)
            self.assertEqual(changes["type"], (legacy, mod.PLATFORM_NAME))

    def test_current_type_is_left_alone(self):
        cfg = {"id": "new-bot", "type": mod.PLATFORM_NAME}
        changes = mod.normalize_platform_config(cfg)
        self.assertNotIn("type", changes)
        self.assertEqual(cfg["type"], mod.PLATFORM_NAME)

    def test_normalize_is_idempotent(self):
        cfg = {"id": "x", "unified_webhook_mode": True}
        mod.normalize_platform_config(cfg)
        self.assertEqual(mod.normalize_platform_config(cfg), {})

    def test_unified_instance_gets_derived_uuid(self):
        cfg = {"id": "李燕丽（个人微信托管）", "unified_webhook_mode": True}
        mod.normalize_platform_config(cfg)
        uuid = cfg["webhook_uuid"]
        self.assertEqual(uuid, mod._derive_webhook_uuid(cfg["id"]))
        self.assertEqual(len(uuid), 16)
        # 重启/重建不换地址：同一实例 id 派生出同一 uuid
        again = {"id": cfg["id"], "unified_webhook_mode": True}
        mod.normalize_platform_config(again)
        self.assertEqual(again["webhook_uuid"], uuid)

    def test_legacy_instance_gets_no_uuid(self):
        cfg = {"id": "legacy", "unified_webhook_mode": False}
        mod.normalize_platform_config(cfg)
        self.assertEqual(cfg["webhook_uuid"], "")

    def test_explicit_uuid_is_kept(self):
        cfg = {"id": "x", "unified_webhook_mode": True, "webhook_uuid": "abc123"}
        self.assertNotIn("webhook_uuid", mod.normalize_platform_config(cfg))
        self.assertEqual(cfg["webhook_uuid"], "abc123")


class AdapterInitNormalizationTest(unittest.TestCase):
    def test_init_normalizes_and_persists(self):
        cfg = {"id": "old-bot", "wpp_gateway_url": GATEWAY, "wpp_gateway_token": "wxa_x"}
        with patch.object(mod, "_persist_platform_config", return_value=True) as persist:
            mod.WppPlatformAdapter(cfg, {}, None)
        self.assertEqual(
            sorted(set(cfg) - mod._TMPL_MANAGED_KEYS), sorted(mod.DEFAULT_CONFIG_TMPL)
        )
        persist.assert_called_once_with(cfg)
        # 统一模式默认关 → 老实例继续独立监听，uuid 留空
        self.assertIs(cfg["unified_webhook_mode"], False)
        self.assertEqual(cfg["webhook_uuid"], "")

    def test_init_does_not_write_when_nothing_changed(self):
        cfg = {"id": "new-bot", "wpp_gateway_url": GATEWAY, "wpp_gateway_token": "wxa_x"}
        cfg.update({k: v for k, v in mod.DEFAULT_CONFIG_TMPL.items()})
        cfg["unified_webhook_mode"] = False
        with patch.object(mod, "_persist_platform_config", return_value=True) as persist:
            mod.WppPlatformAdapter(cfg, {}, None)
        persist.assert_not_called()

    def test_two_instances_end_with_identical_key_sets(self):
        """「加多少台机器人配置规则都一样」：两台实例最终键集合完全相同。"""
        a_cfg = {"id": "bot-a", "wpp_gateway_token": "wxa_a", "wpp_lazy_media": False}
        b_cfg = {"id": "bot-b", "wpp_gateway_token": "wxa_b"}
        with patch.object(mod, "_persist_platform_config", return_value=True):
            mod.WppPlatformAdapter(a_cfg, {}, None)
            mod.WppPlatformAdapter(b_cfg, {}, None)
        self.assertEqual(sorted(a_cfg), sorted(b_cfg))
        types = {k: type(v) for k, v in a_cfg.items()}
        self.assertEqual(types, {k: type(v) for k, v in b_cfg.items()})


if __name__ == "__main__":
    unittest.main()
