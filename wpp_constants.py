"""平台标识常量（WPP Cloud 专用）。

本插件只适配 **WPP Cloud 托管**：出站走 WPP 网关
（``https://<host>/api/v1/gateway`` + ``Authorization: Bearer wxa_...``），
入站走 WPP 控制台配置的普通回调 + ``/Msg/Sync`` 拉取；不建立 vendor WS，
也不向 vendor 注册 ``/Webhook/*``。

平台技术名（= 配置里的 ``type`` = 适配器注册名）**就是** Dashboard
「创建机器人 → 选择消息平台类别」里显示的那一行：AstrBot 的前端把
``platform.config_template`` 的**键**直接当选项文案渲染（Vuetify 对字符串
item 取 ``name`` 属性会回退成 item 本身），而该键就是 ``adapter_name``，
``adapter_display_name`` 只影响 ``/api/v1/bot-types`` 这份数据，改不动那个下拉。
所以想让下拉显示「个人微信(Pad协议)」，技术名本身就得是它。
"""

PLUGIN_NAME = "astrbot_plugin_wpp_cloud"

#: 平台适配器技术名 = 配置里的 ``type`` = 创建机器人下拉里显示的名字。
#: 改名属于**破坏性变更**：已有实例的 ``type`` 必须一起迁移（否则
#: ``astrbot/core/platform/manager.py:231`` 会抛「Platform adapter not found」）。
PLATFORM_NAME = "个人微信(Pad协议)"

#: 历史技术名。老版本安装的实例 ``type`` 还是这些值，注册期把它们作为
#: 别名映射到同一个类（不带配置模板，因此不会出现在创建下拉里），
#: 保证升级后老实例仍能加载；运行时目录前缀映射也兼容它们。
LEGACY_PLATFORM_NAMES: tuple[str, ...] = ("WPP Cloud（个人微信托管）", "WPP Cloud")

#: Dashboard「添加机器人」列表里显示的名字；与技术名保持一致。
PLATFORM_DISPLAY_NAME = PLATFORM_NAME

#: 运行时目录 / webhook token 使用的稳定 ASCII 前缀（不是平台技术名）。
RUNTIME_ID_SLUG = "wpp-cloud"

#: 派生统一 Webhook uuid 时使用的固定后缀。**不要改**：改了等于给所有
#: 自动派生 uuid 的实例换回调地址。
WEBHOOK_UUID_SEED_SUFFIX = "wpp-cloud"

#: 平台适配器图标（机器人添加/列表）。插件扩展列表仍使用根目录 logo.png。
PLATFORM_LOGO_PATH = "logo-platform.png"
