"""WPP Cloud 平台适配器（只适配 WPP 托管）。

AstrBot 平台适配器插件范式 (docs/zh/dev/plugin-platform-adapter.md):
  1. main.py 必须有 Star 类 (否则 star_manager 拒绝加载)
  2. 适配器放独立文件, 用 @register_platform_adapter 装饰
  3. main.py __init__ 里 import 适配器模块触发注册

传输契约（WPP Cloud）:
  - 出站: ``https://<host>/api/v1/gateway`` + ``Authorization: Bearer wxa_...``
  - 入站主通道: WPP 控制台「普通回调」→ 本插件 webhook（v2 HMAC）
  - 入站兜底: ``/Msg/Sync`` 每 60s + 收到回调通知立即拉
  - **不建立 vendor WS，也不向 vendor 注册 /Webhook/*（槽位归 WPP）
  - 消息类型数字: 1=text 3=image 6=file 34=voice 43=video 47=emoji 49=app 51=relay 10002=revoke

账号模型:
  - 一个平台实例 = 一个 WPP 网关令牌（``wxa_...``）= 一个托管微信账号
  - 多账号 = 多个平台实例 (id 唯一, 各自令牌)
  - 同一个令牌只允许一个实例轮询
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import (
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from astrbot.core.platform.message_session import MessageSession

from .wpp_account import WppAccount
from .wpp_constants import (
    LEGACY_PLATFORM_NAMES,
    PLATFORM_DISPLAY_NAME,
    PLATFORM_LOGO_PATH,
    PLATFORM_NAME,
    RUNTIME_ID_SLUG,
    WEBHOOK_UUID_SEED_SUFFIX,
)
from .wpp_event import WppMessageEvent
from .wpp_heartflow import (
    HEARTFLOW_WHITELIST_MODES,
    resolve_heartflow_groups,
    sync_heartflow_whitelist as sync_heartflow_config,
)
from .wpp_messages import extract_all_msg_src, safe_str, split_users
from .wpp_send import send_chain
from .wpp_storage import plugin_data_dir, safe_account_id
from .wpp_sync import REASON_PUSH, max_skew_seconds


def _as_csv(value: Any) -> set[str]:
    """配置值 (list 或逗号串) → wxid/群 ID 集合。"""
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip() for v in value if str(v).strip()}
    return split_users(str(value or ""))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _reload_safe_globals() -> dict[str, Any]:
    """返回跨插件重载保留的全局注册表。

    AstrBot 热重载插件时会把本模块从 sys.modules 里摘掉再重新 import，模块级
    变量全部归零，但**旧适配器实例还在跑**（端口还占着、令牌还在轮询）。把
    共享监听器与令牌锁挂在常驻的 ``astrbot`` 包对象上，重载后的新模块才能
    接住旧实例的状态：否则会再绑一次 6199（errno 98）或重复轮询同一令牌。
    """
    try:
        import astrbot  # noqa: PLC0415 - 运行时导入, 避免测试环境把插件当独立包
    except Exception:  # noqa: BLE001 - 没有 astrbot 时退化为模块级字典
        return {}
    store = getattr(astrbot, "_wpp_cloud_globals", None)
    if not isinstance(store, dict):
        store = {}
        setattr(astrbot, "_wpp_cloud_globals", store)
    return store


def _derive_webhook_uuid(instance_id: Any) -> str:
    """按平台实例 id 派生稳定的统一 Webhook 标识。

    AstrBot 只给 ``WEBHOOK_SUPPORTED_PLATFORMS`` 里的类型自动生成 ``webhook_uuid``
    (见 ``astrbot/core/utils/webhook_utils.py``)，WPP Cloud 是第三方插件类型，不在
    其中，所以由插件自己派生：同一个实例 id 每次启动得到同一个 uuid，重启后
    ``/api/platform/webhook/<uuid>`` 不会变（无需落盘，也不会因为丢失文件而失效）。
    """
    seed = f"{str(instance_id or '').strip() or RUNTIME_ID_SLUG}:{WEBHOOK_UUID_SEED_SUFFIX}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _astrbot_callback_base() -> str:
    """AstrBot 自己的公网基址 (``callback_api_base``)，没配置时返回空串。

    统一 Webhook 模式的回调地址形如 ``<base>/api/platform/webhook/<uuid>``，
    base 就是 AstrBot 面板对外可访问的域名/地址。
    """
    try:
        from astrbot.core import astrbot_config

        return str(astrbot_config.get("callback_api_base", "") or "").strip().rstrip("/")
    except Exception:  # noqa: BLE001 - 配置不可用时降级为仅输出路径
        return ""


class _SharedWebhookServer:
    """同一 ``host:port`` 上的多个平台实例共用一个 aiohttp server。

    每个实例的回调路径不同（各自带独立 URL token），所以按路径分发即可。这样
    即使多个账号实例都把 ``webhook_port`` 填成 6199，也不会再出现
    ``[Errno 98] address already in use`` 把新实例启动直接打挂。
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = int(port)
        self._owners: dict[str, Any] = {}
        self._runner = None

    def is_idle(self) -> bool:
        return not self._owners

    async def ensure_started(self) -> None:
        """首次调用时真正 bind；已启动则直接复用。"""
        if self._runner is not None:
            return
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/{tail:.*}", self._dispatch_get)
        app.router.add_post("/{tail:.*}", self._dispatch_post)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner

    def register(self, path: str, adapter: Any) -> None:
        self._owners[str(path).rstrip("/")] = adapter

    def unregister(self, path: str) -> None:
        self._owners.pop(str(path).rstrip("/"), None)

    def _owner(self, path: str) -> Any:
        return self._owners.get(str(path).rstrip("/"))

    async def _dispatch_post(self, request: Any) -> Any:
        from aiohttp import web

        owner = self._owner(request.path)
        if owner is None:
            logger.warning("[WPP Cloud] 回调 path 未匹配到任何账号实例 (token 不落日志)")
            return web.Response(status=404)
        return await owner._handle_webhook(request)

    async def _dispatch_get(self, request: Any) -> Any:
        from aiohttp import web

        if self._owner(request.path) is None:
            return web.Response(status=404)
        return web.Response(text="ok")

    async def release(self) -> None:
        """最后一个实例退出时才真正关闭 server。"""
        if not self.is_idle():
            return
        # 先摘登记再 await：cleanup 会让出事件循环，期间若新实例拿到旧 hub 就会
        # 注册进一个正在关闭的 server，回调直接 404。
        for key, value in list(_SHARED_WEBHOOK_SERVERS.items()):
            if value is self:
                _SHARED_WEBHOOK_SERVERS.pop(key, None)
        runner, self._runner = self._runner, None
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()


#: ``(host, port) -> 共享 server``；只在独立监听模式（unified_webhook_mode=false）下使用。
#: 启动预检（在线检查 + 状态刷新）的硬超时：超时即判启动失败，不要挂住平台。
PREFLIGHT_TIMEOUT_S = 30.0

_SHARED_WEBHOOK_SERVERS: dict[tuple[str, int], _SharedWebhookServer] = (
    _reload_safe_globals().setdefault("shared_webhook_servers", {})
)


def normalize_gateway_url(value: Any) -> str:
    """把用户填的网关地址归一化为 ``.../api/v1/gateway``。

    接受 ``https://host``、``https://host/api``、``https://host/api/v1`` 和
    完整网关地址；空值返回空串（由调用方报错）。
    """
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/api/v1/gateway"):
        return url
    if url.endswith("/api/v1"):
        return url + "/gateway"
    if url.endswith("/api"):
        return url + "/v1/gateway"
    return url + "/api/v1/gateway"


def _parse_account(platform_config: dict) -> dict | None:
    """解析当前平台实例的唯一账号。

    一个平台实例 = 一个 WPP 网关令牌（``wxa_...``）= 一个微信账号。
    多账号请添加多个平台实例 (id 唯一, 各自令牌)。未配置令牌/地址时返回 None。
    """
    if str(platform_config.get("wpp_auth_token", "") or "").strip() or str(
        platform_config.get("wpp_ws_url", "") or ""
    ).strip():
        logger.warning(
            "[WPP Cloud] 忽略旧的 vendor 配置项 wpp_auth_token / wpp_ws_url；"
            "本插件只使用 wpp_gateway_url + wpp_gateway_token。"
        )

    token = str(platform_config.get("wpp_gateway_token", "") or "").strip()
    base_url = normalize_gateway_url(platform_config.get("wpp_gateway_url", ""))
    if not token:
        return None
    if not base_url:
        logger.error(
            "[WPP Cloud] 未配置 wpp_gateway_url：请填写 WPP 网关地址，"
            "例如 https://wpp.example.com/api/v1/gateway"
        )
        return None
    return {
        "id": str(platform_config.get("id", PLATFORM_NAME) or PLATFORM_NAME),
        "authcode": token,
        "allow_users": _as_csv(platform_config.get("wpp_allow_users", "")),
        "group_reply": str(platform_config.get("wpp_group_reply", "atbot") or "atbot"),
        "blacklist_groups": set(),
        "block_users": _as_csv(platform_config.get("wpp_block_users", "")),
        "require_at_mention": False,
        "private_allow_all": _as_bool(
            platform_config.get("wpp_private_allow_all"), False
        ),
        "proactive_groups": _as_csv(platform_config.get("wpp_proactive_groups", "")),
        "base_url": base_url,
        "ws_url": "",
        "cloud_mode": True,
    }


def authcode_fingerprint(authcode: str) -> str:
    """authcode 锁标识 (不落盘/不打印明文)。"""
    return hashlib.sha256(str(authcode).encode("utf-8")).hexdigest()[:12]


# Dashboard WebUI 表单配置元数据 (config_metadata)
WPP_CONFIG_METADATA = {
    "wpp_gateway_url": {
        "description": "WPP 网关地址",
        "type": "string",
        "hint": "填你的 WPP 部署网关，例如 https://<平台域名>/api/v1/gateway；也可只填 https://<平台域名>（自动补全路径）。",
    },
    "wpp_gateway_token": {
        "description": "WPP 网关令牌 (wxa_...)",
        "type": "string",
        "secret": True,
        "hint": "在 WPP 控制台「凭证/账号」详情里签发。一个平台实例只能填一把令牌，对应一个托管微信账号；重新签发会让旧令牌立即失效。",
    },
    "wpp_allow_users": {
        "description": "白名单 wxid / 群ID (逗号分隔)",
        "type": "string",
        "hint": "wxid 控制私聊, @chatroom 群ID 同时作为群白名单和主动回复候选（与白名单共用）。作为启动初始值；日常修改请在 WPP 控制台进行。",
    },
    "wpp_private_allow_all": {
        "description": "空白名单时放行所有私聊",
        "type": "bool",
        "hint": "默认 false：私聊只放行白名单里的 wxid（群 ID 不影响私聊）。只有调试/内部账号才建议开启；开启后仍受 wpp_block_users 约束。作为启动初始值；日常修改请在 WPP 控制台进行。",
    },
    "wpp_block_users": {
        "description": "私聊黑名单 wxid (逗号分隔)",
        "type": "string",
        "hint": "全部放行模式下用它排除特定 wxid (私聊/群聊都生效)。公众号/微信团队/微信支付/服务通知等系统消息已内置静默, 无需填写。作为启动初始值；日常修改请在 WPP 控制台进行",
    },
    "wpp_group_reply": {
        "description": "群消息回复模式",
        "type": "string",
        "hint": "atbot=只放行@ / none=忽略群 / all=放行所有群消息(观察模式, 不强制回复)。作为启动初始值；日常修改请在 WPP 控制台进行",
    },
    "wpp_proactive_groups": {
        "description": "额外主动回复候选群 (逗号分隔群ID)",
        "type": "string",
        "hint": "群白名单 (wpp_allow_users 中的 @chatroom) 已自动作为主动候选, 通常无需填写; 此项只用于未配置群白名单时显式补充。未@消息只放行不唤醒, 由 Heartflow/active_reply 决定是否接话。作为启动初始值；日常修改请在 WPP 控制台进行。",
    },
    "wpp_sync_heartflow_whitelist": {
        "description": "群白名单自动同步到 Heartflow",
        "type": "bool",
        "hint": "默认 true。把各账号的群白名单 + 主动候选群同步到 Heartflow 的 chat_whitelist (只替换本平台的 UMO 条目, 保留其他平台条目), 写盘并实时更新已加载实例。启动/策略变更/热更新即时同步, 并每 5 分钟自愈一次。未安装 Heartflow 时自动跳过。",
    },
    "wpp_heartflow_whitelist_mode": {
        "description": "Heartflow 白名单来源",
        "type": "string",
        "hint": "auto=参考 WPP 群白名单 (默认); custom=用下方 wpp_heartflow_groups 单独设置。custom 中的群必须在群白名单 (allow_users 的 @chatroom) 中, 否则会被拒绝并告警。",
    },
    "wpp_heartflow_groups": {
        "description": "单独设置的 Heartflow 群白名单 (custom 模式)",
        "type": "string",
        "hint": "逗号分隔群 ID, 仅 wpp_heartflow_whitelist_mode=custom 时生效。群白名单必须包含这些群, 适配器才会放行未@消息。",
    },
    "wpp_lazy_media": {
        "description": "未@群消息媒体懒加载",
        "type": "bool",
        "hint": "默认 true。主动候选群/观察模式下, 图片/文件/视频只在真正调用 LLM 前下载。若启用了内置 group_icl_enable / group_message_history_enable (需要在事件创建时拿到真实媒体), 请设为 false 改为即时下载。",
    },
    "wpp_send_interval_ms": {
        "description": "同一会话发送最小间隔 (毫秒)",
        "type": "int",
        "hint": "默认 300。同一会话的多分片/多事件回复会串行并保持该间隔, 降低微信风控概率; 0=不节流。",
    },
    "wpp_outbound_repeat_guard": {
        "description": "出站复读保护",
        "type": "bool",
        "hint": "默认 true。同一会话相同文本在窗口内超过次数上限后跳过, 防止 LLM 复读刷屏。",
    },
    "wpp_outbound_repeat_window": {
        "description": "复读保护窗口 (秒)",
        "type": "int",
        "hint": "默认 300 (5 分钟)。",
    },
    "wpp_outbound_repeat_limit": {
        "description": "复读保护次数上限",
        "type": "int",
        "hint": "默认 2。窗口内第 3 条相同文本开始跳过 (前两条正常发送, 避免误杀正常重复回答)。",
    },
    "wpp_sync_interval": {
        "description": "/Msg/Sync 兜底轮询间隔 (秒)",
        "type": "int",
        "hint": "默认 60。WPP 回调通知到达时会立即拉取；此间隔只用于回调丢失/网络抖动时的兜底。",
    },
    "unified_webhook_mode": {
        "description": "使用 AstrBot 统一 Webhook 入口",
        "type": "bool",
        "hint": "默认开启（新建实例默认开，多账号场景必须开）：回调直接走 AstrBot 自己的端口 "
        "https://<你的域名>/api/platform/webhook/<webhook_uuid>，多个账号实例共用一个端口，"
        "不会出现 6199 被上一个实例占用导致新实例启动失败 (errno 98)。"
        "只有在这个开关关闭时，本插件才会自己监听下面的 webhook_host/webhook_port。",
    },
    "webhook_uuid": {
        "description": "统一 Webhook 标识",
        "type": "string",
        "hint": "留空即按实例 id 自动派生（推荐：重启后地址不变）。它就是回调地址 "
        "/api/platform/webhook/<webhook_uuid> 的最后一段，填错会导致该实例收不到回调。",
    },
    "webhook_host": {
        "description": "webhook 监听地址（仅独立监听模式）",
        "type": "string",
        "hint": "默认 0.0.0.0。unified_webhook_mode=true 时该项不生效；"
        "多账号各自独立监听时必须给每个实例填不同端口。",
    },
    "webhook_port": {
        "description": "webhook 监听端口（仅独立监听模式）",
        "type": "int",
        "hint": "默认 6199。unified_webhook_mode=true 时该项不生效；"
        "多账号请勿共用同一端口（同一端口上的多个实例会共用一个 server、按路径分发，"
        "但仍建议各自独立端口以免混淆）。",
    },
    "webhook_public_url": {
        "description": "回调地址公网前缀",
        "type": "string",
        "hint": "WPP 控制台回调使用的公网 URL（含协议/域名，不含路径），例如 https://astrbot.example.com。"
        "统一 Webhook 模式下留空则用 AstrBot 的 callback_api_base；"
        "独立监听模式下留空会退化成 http://<host>:<port>（不要用公网 IP:端口）。"
        "完整回调地址（含 token）会写入 data 目录并在启动日志提示。",
    },
    "webhook_path": {
        "description": "webhook 路径前缀（仅独立监听模式）",
        "type": "string",
        "hint": "默认 /wpp-cloud/webhook；实际路径会自动追加 URL token。"
        "unified_webhook_mode=true 时路径由 AstrBot 的 /api/platform/webhook/<uuid> 决定。",
    },
    "webhook_token": {
        "description": "webhook URL token（仅独立监听模式）",
        "type": "string",
        "secret": True,
        "hint": "留空时自动生成并持久化。属于 URL 的一部分，请勿外泄。",
    },
    "webhook_secret": {
        "description": "WPP 回调 HMAC 密钥",
        "type": "string",
        "secret": True,
        "hint": "与 WPP 控制台该账号回调的 secret 保持一致。WPP 发送 X-Webhook-Signature v2（HMAC-SHA256(secret, \"{TsMs}.{DeliveryID}.\" + body)）。留空时仅靠 URL token 鉴权（建议始终设置）。",
    },
}


#: 平台实例的**完整**默认配置：新建机器人用它生成表单，插件启动时也用它给
#: 已有实例补齐缺省项 —— 两者共用同一份键表，所以「不管加多少台机器人，
#: 配置项集合与默认值都完全一致」。
DEFAULT_CONFIG_TMPL: dict[str, Any] = {
    "wpp_gateway_url": "",
    "wpp_gateway_token": "",
    "wpp_allow_users": "",
    "wpp_private_allow_all": False,
    "wpp_block_users": "",
    "wpp_group_reply": "atbot",
    "wpp_proactive_groups": "",
    "wpp_sync_heartflow_whitelist": True,
    "wpp_heartflow_whitelist_mode": "auto",
    "wpp_heartflow_groups": "",
    "wpp_lazy_media": True,
    "wpp_send_interval_ms": 300,
    "wpp_outbound_repeat_guard": True,
    "wpp_outbound_repeat_window": 300,
    "wpp_outbound_repeat_limit": 2,
    "wpp_sync_interval": 60,
    # 多账号关键：默认走 AstrBot 统一 Webhook（共用 AstrBot 端口，按 uuid 分发），
    # 每个实例不再各占一个端口，新实例启用时不会被内核报 errno 98。
    "unified_webhook_mode": True,
    "webhook_uuid": "",
    "webhook_public_url": "",
    "webhook_secret": "",
    # 以下四项只在 unified_webhook_mode=false（插件自己监听）时生效。
    "webhook_host": "0.0.0.0",
    "webhook_port": 6199,
    "webhook_path": "/wpp-cloud/webhook",
    "webhook_token": "",
}

#: 由 AstrBot 内核注入/管理的键，不参与补齐。
_TMPL_MANAGED_KEYS = frozenset({"type", "enable", "id"})

#: 老实例缺键时的保守默认值。统一 Webhook 是后加的能力：缺键的老实例必须
#: 继续独立监听，否则它已经登记在 WPP 控制台的回调地址会立刻 404。
_LEGACY_MISSING_DEFAULTS: dict[str, Any] = {"unified_webhook_mode": False}

#: 数值键的合法区间 (min, max)，None = 该侧不限制。
_CONFIG_BOUNDS: dict[str, tuple[int | None, int | None]] = {
    "wpp_sync_interval": (5, None),
    "wpp_send_interval_ms": (0, None),
    "wpp_outbound_repeat_window": (0, None),
    "wpp_outbound_repeat_limit": (1, None),
    "webhook_port": (1, 65535),
}


#: 枚举型配置键 -> 允许取值。拼错的值（"atbo"、"autoo"）会一直带着跑，直到
#: 某天群消息完全不回复；这里统一拦在配置入口。
_CONFIG_ENUMS: dict[str, frozenset[str]] = {
    "wpp_group_reply": frozenset({"atbot", "none", "all"}),
    # 与 wpp_heartflow.resolve_heartflow_groups 的别名表保持一致：
    # auto/reference/whitelist 等价，custom/manual/separate 等价。
    "wpp_heartflow_whitelist_mode": frozenset(
        {"auto", "reference", "whitelist", "custom", "manual", "separate"}
    ),
}


def _coerce_config_value(key: str, value: Any, default: Any) -> Any:
    """把配置值纠正成模板默认值的类型，整数键再夹到 ``_CONFIG_BOUNDS`` 区间。"""
    if isinstance(default, bool):
        return _as_bool(value, default)
    if isinstance(default, int):
        num = _as_int(value, default)
        lo, hi = _CONFIG_BOUNDS.get(key, (None, None))
        if lo is not None:
            num = max(lo, num)
        if hi is not None:
            num = min(hi, num)
        return num
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        text = str(value)
    allowed = _CONFIG_ENUMS.get(key)
    if allowed is not None and text and text.lower() not in allowed:
        logger.warning(
            "[WPP Cloud] 配置 %s=%r 不是合法取值 %s，已回退为 %r",
            key,
            text,
            "/".join(sorted(allowed)),
            default,
        )
        text = ""
    elif allowed is not None:
        text = text.lower()
    # 非空默认值（webhook_host / wpp_group_reply 等）被清空时回填，避免
    # 监听 "" 端口、未知枚举之类的启动期错误。
    return text or default


def normalize_platform_config(platform_config: dict) -> dict[str, tuple[Any, Any]]:
    """按模板补齐/纠正单个平台实例的配置，返回 ``{键: (旧值, 新值)}``。

    老实例的配置常常缺键或类型不对（``webhook_port`` 写成字符串、
    ``wpp_private_allow_all`` 写成 ``"false"`` 字符串等），本函数让任意实例在
    启动后拥有一致的键表与值类型，也就是「无论添加多少机器人，配置规则统一」。
    只改内存里的 config dict，落盘由调用方决定（见 ``_persist_platform_config``）。
    """
    changes: dict[str, tuple[Any, Any]] = {}

    # 技术名迁移：老版本的 type 是历史名。别名映射能让它照常加载，但
    # 配置里留着旧 type 会让面板继续显示旧名字，所以顺手改成当前名。
    legacy_type = str(platform_config.get("type") or "")
    if legacy_type in LEGACY_PLATFORM_NAMES and legacy_type != PLATFORM_NAME:
        platform_config["type"] = PLATFORM_NAME
        changes["type"] = (legacy_type, PLATFORM_NAME)

    for key, default in DEFAULT_CONFIG_TMPL.items():
        if key in _TMPL_MANAGED_KEYS:
            continue
        if key not in platform_config:
            filled = _LEGACY_MISSING_DEFAULTS.get(key, default)
            platform_config[key] = filled
            changes[key] = (None, filled)
            continue
        old = platform_config[key]
        new = _coerce_config_value(key, old, default)
        if new != old:
            platform_config[key] = new
            changes[key] = (old, new)

    # 统一 Webhook 的 uuid 必须落到配置里：AstrBot 的 find_platform_by_uuid() /
    # unified_webhook() 以及前端的「查看 Webhook」按钮都直接读 platform.config。
    if _as_bool(platform_config.get("unified_webhook_mode"), False):
        uuid = str(platform_config.get("webhook_uuid", "") or "").strip()
        if not uuid:
            uuid = _derive_webhook_uuid(platform_config.get("id"))
            platform_config["webhook_uuid"] = uuid
            changes["webhook_uuid"] = ("", uuid)
    return changes


def _persist_platform_config(platform_config: dict) -> bool:
    """把补齐后的实例配置写回 AstrBot 配置文件；失败只告警，不影响启动。"""
    try:
        from astrbot.core import astrbot_config
    except Exception as exc:  # noqa: BLE001 - 老版本/受限环境里没有内核配置
        logger.warning("[WPP Cloud] 无法导入 AstrBot 配置，补齐结果仅作用于内存: %s", exc)
        return False
    try:
        platforms = astrbot_config.get("platform")
        if not isinstance(platforms, list) or not any(
            cfg is platform_config for cfg in platforms
        ):
            # 内核没把同一份 dict 交给适配器（目前不会发生）：不写盘，避免写坏配置。
            logger.warning("[WPP Cloud] 配置补齐未落盘：适配器拿到的不是内核配置对象")
            return False
        astrbot_config.save_config()
        return True
    except Exception as exc:  # noqa: BLE001 - 写盘失败不能阻断平台启动
        logger.warning("[WPP Cloud] 配置补齐写盘失败: %s", exc)
        return False


@register_platform_adapter(
    PLATFORM_NAME,
    "个人微信(Pad协议) · WPP Cloud 托管（出站走网关，入站走回调 + /Msg/Sync）",
    adapter_display_name=PLATFORM_DISPLAY_NAME,
    logo_path=PLATFORM_LOGO_PATH,
    support_streaming_message=False,
    # 传拷贝：register_platform_adapter 会就地往这个 dict 里塞 type/enable/id，
    # 直接传常量会被污染，启动时就无法再用它给老实例补齐配置。
    default_config_tmpl=dict(DEFAULT_CONFIG_TMPL),
    config_metadata=WPP_CONFIG_METADATA,
)
class WppPlatformAdapter(Platform):
    """一个平台实例 = 一个 authcode = 一个微信账号。

    平台级职责: 账号解析/生命周期/meta/create_event/send_by_session/webhook 兜底。
    账号级职责 (WppAccount): WS/队列/消息处理/白名单/filehelper/去重。
    多账号 = 多个平台实例 (id 唯一, 各自 authcode)。
    """

    #: authcode 指纹 → 持有它的实例 id；同一 authcode 只允许一个实例轮询。
    #: 令牌 -> 实例 id; 必须跨插件重载保留 (见 _reload_safe_globals)。
    _ACTIVE_AUTHCODES: dict[str, str] = _reload_safe_globals().setdefault(
        "active_authcodes", {}
    )

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self.settings = platform_settings

        # 实例标识 (平台级, AstrBot 会话路由用)
        self.instance_id = str(platform_config.get("id", PLATFORM_NAME) or PLATFORM_NAME)

        # 统一配置口径：老实例缺的键按模板补齐、类型纠偏（只记键名，不打值，
        # 避免 secret/token 落日志）。补齐成功时顺手写盘，让面板表单也一致。
        normalized = normalize_platform_config(platform_config)
        if normalized:
            logger.info(
                "[WPP Cloud] 实例「%s」配置已按模板统一：%s",
                self.instance_id,
                ", ".join(sorted(normalized)),
            )
            _persist_platform_config(platform_config)

        self.base_url = normalize_gateway_url(platform_config.get("wpp_gateway_url", ""))
        #: WPP Cloud 没有 vendor WS；该属性保留为空，便于共用发送/日志代码。
        self.ws_url = ""
        #: 回调接收器是本插件唯一的入站入口，始终启用。
        self.webhook_enabled = True
        self.cloud_mode = True
        self.sync_interval_s = max(
            5, _as_int(platform_config.get("wpp_sync_interval"), 60)
        )
        # 群白名单自动同步到 Heartflow 的 chat_whitelist (可选集成, 默认开)
        self.heartflow_sync_enabled = _as_bool(
            platform_config.get("wpp_sync_heartflow_whitelist"), True
        )
        hf_mode = (
            str(platform_config.get("wpp_heartflow_whitelist_mode", "auto") or "auto")
            .strip()
            .lower()
        )
        if hf_mode not in HEARTFLOW_WHITELIST_MODES:
            logger.warning(f"[WPP] 未知 Heartflow 白名单模式 {hf_mode!r}, 回退 auto")
            hf_mode = "auto"
        self.heartflow_whitelist_mode = hf_mode
        self.heartflow_groups = _as_csv(platform_config.get("wpp_heartflow_groups", ""))
        self._heartflow_rejected_warned: set[str] = set()
        self.lazy_media = _as_bool(platform_config.get("wpp_lazy_media"), True)
        self.send_interval_ms = max(0, _as_int(platform_config.get("wpp_send_interval_ms"), 300))
        self.outbound_repeat_guard = _as_bool(
            platform_config.get("wpp_outbound_repeat_guard"), True
        )
        self.outbound_repeat_window = max(
            0, _as_int(platform_config.get("wpp_outbound_repeat_window"), 300)
        )
        self.outbound_repeat_limit = max(
            1, _as_int(platform_config.get("wpp_outbound_repeat_limit"), 2)
        )
        self._heartflow_all_warned = False

        # 保活状态 (account_id -> (state, detail)): 只在状态变化时输出日志。
        # vendor 未登录/维护期间 Code=-1 是预期状态，旧版每 30s 打一条
        # WARNING，重启后会刷屏十几分钟。
        self._keepalive_state: dict[str, tuple[str, str]] = {}
        # 不可用心跳的退避 (account_id -> monotonic 截止时间)，防止每 30s
        # 对同一个未登录账号重复调用 AutoHeartBeat。
        self._keepalive_next_attempt: dict[str, float] = {}

        # 缓存平台元数据 (AstrBot 每条消息都会取, 不要每次重建)
        self.metadata = PlatformMetadata(
            name=PLATFORM_NAME,
            description=f"{PLATFORM_DISPLAY_NAME}适配器",
            id=self.instance_id,
            adapter_display_name=PLATFORM_DISPLAY_NAME,
            logo_path=PLATFORM_LOGO_PATH,
            # WPP 不支持流式消息 (vendor 无消息编辑能力)
            support_streaming_message=False,
            support_proactive_message=True,
            module_path=__name__,
            config_metadata=WPP_CONFIG_METADATA,
        )

        # 一个实例只解析/创建一个账号：authcode 与微信账号一一对应。
        acfg = _parse_account(platform_config)
        self.account: WppAccount | None = None
        if acfg:
            self.account = WppAccount(
                platform=self,
                account_id=acfg["id"],
                authcode=acfg["authcode"],
                base_url=acfg["base_url"],
                ws_url=acfg["ws_url"],
                allow_users=acfg.get("allow_users") or set(),
                group_reply=acfg.get("group_reply", "atbot"),
                blacklist_groups=acfg.get("blacklist_groups") or set(),
                block_users=acfg.get("block_users") or set(),
                require_at_mention=acfg.get("require_at_mention", False),
                private_allow_all=acfg.get("private_allow_all", False),
                proactive_groups=acfg.get("proactive_groups") or set(),
                lazy_media=self.lazy_media,
                send_interval_ms=self.send_interval_ms,
                repeat_guard_window=(
                    float(self.outbound_repeat_window) if self.outbound_repeat_guard else 0.0
                ),
                repeat_guard_limit=self.outbound_repeat_limit,
                cloud_mode=True,
                sync_interval_s=float(self.sync_interval_s),
            )
            logger.info(
                f"[WPP Cloud] 账号已注册: id={self.account.account_id} "
                f"gateway={self.account.base_url} "
                f"inbound=callback+/Msg/Sync"
            )

        # WPP 回调接收器：两种模式
        #   ① 统一 Webhook (默认, 多账号推荐)：AstrBot 自己的端口 + /api/platform/webhook/<uuid>
        #   ② 独立监听 (legacy)：本插件自己起 aiohttp server，读下面的 host/port/path/token
        # 老实例可能完全没有 unified_webhook_mode 键 —— 保持独立监听，避免它已经登记在
        # WPP 控制台里的回调地址突然失效；新建实例由 default_config_tmpl 显式给 true。
        self.unified_webhook_mode = _as_bool(
            platform_config.get("unified_webhook_mode"), False
        )
        self.webhook_uuid = str(platform_config.get("webhook_uuid", "") or "").strip()
        if not self.webhook_uuid:
            self.webhook_uuid = _derive_webhook_uuid(self.instance_id)
        if self.unified_webhook_mode:
            # AstrBot 的 find_platform_by_uuid()/unified_webhook() 读的是 platform.config，
            # 只有写回去插件页才会出现「查看 Webhook」按钮、回调才能被路由到本实例。
            platform_config["unified_webhook_mode"] = True
            platform_config["webhook_uuid"] = self.webhook_uuid

        # 前导斜杠必须补: 配置写 "wpp-cloud/webhook" 时, 拼出来的路径永远不等于
        # aiohttp 的 request.path ("/wpp-cloud/webhook/<token>"), legacy 回调会
        # 一直 404 而只有一条 WARNING。
        self.webhook_path = "/" + str(
            platform_config.get("webhook_path", "/wpp-cloud/webhook")
        ).strip().strip("/")
        self.webhook_token = str(platform_config.get("webhook_token", "") or "").strip()
        if not self.webhook_token:
            self.webhook_token = _webhook_token_for(self.instance_id)
        self.webhook_path = f"{self.webhook_path}/{self.webhook_token}"
        self.webhook_secret = str(platform_config.get("webhook_secret", "") or "").strip()
        #: 「统一模式缺 secret」的告警只打一次, 避免每个伪造请求都刷一条 ERROR。
        self._warned_no_secret = False
        self.webhook_host = str(platform_config.get("webhook_host", "0.0.0.0"))
        # 0 = 由内核分配临时端口 (测试/单机多实例用), 不能夹成 1。
        self.webhook_port = max(
            0, min(_as_int(platform_config.get("webhook_port"), 6199), 65535)
        )
        self.webhook_public_url = str(platform_config.get("webhook_public_url", "") or "").rstrip(
            "/"
        )

        self._runner = None
        self._server = None
        self._hub = None
        self._status_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None
        # 串行化 run() 的启动阶段与 terminate()，避免 terminate 在启动
        # 中途执行后，启动代码又把 WS/worker 拉起来（资源泄漏 + 锁已释放）。
        self._lifecycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------ Platform
    def meta(self) -> PlatformMetadata:
        return self.metadata

    @property
    def accounts(self) -> list[WppAccount]:
        """控制台/命令按 accounts 列表展示；单实例恒为 0 或 1 个账号。"""
        return [self.account] if self.account is not None else []

    def create_event(self, message) -> WppMessageEvent:
        """AstrBot 标准事件工厂 (适配器基类约定)。"""
        acct = self.account
        return WppMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            api=acct._api if acct else None,
            account_id=acct.account_id if acct else "",
        )

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["accounts"] = [self.account.stats] if self.account else []
        # 前端据此显示「查看 Webhook」按钮 & 回调地址；AstrBot 面板本身只认
        # unified_webhook 字段，这里再补一个更直观的 webhook_url。
        stats["unified_webhook"] = self._use_unified_webhook()
        stats["webhook_url"] = self.webhook_hint()
        if self.account:
            stats["meta"]["description"] = f"{PLATFORM_DISPLAY_NAME} · {self.account.account_id}"
        return stats

    # ------------------------------------------------------------------ 生命周期
    async def _preflight(self, acct: Any) -> None:
        """启动预检: 确认在线 → 刷新身份 → 拉起账号 worker/同步/维护循环。

        独立成协程是为了能用 ``asyncio.wait_for`` 加硬超时；它**不能**在
        ``_lifecycle_lock`` 里跑（见 run() 的注释）。
        """
        await self._ensure_logged_in(acct)
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            return
        await self._refresh_account_status(acct)
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            return
        acct.start()

    async def run(self) -> None:
        """启动账号 worker + WPP 回调接收器 + /Msg/Sync 兜底。"""
        acct = self.account
        if acct is None:
            raise ValueError(
                "WPP Cloud: 未配置 wpp_gateway_token（WPP 网关令牌 wxa_...）"
                "或 wpp_gateway_url。一个平台实例只能绑定一个托管微信账号；"
                "多账号请添加多个平台实例。"
            )

        # 一个网关令牌同时只能有一个轮询者：两个实例会重复拉 /Msg/Sync。
        fp = authcode_fingerprint(acct.authcode)
        owner = self._ACTIVE_AUTHCODES.get(fp)
        if owner and owner != self.instance_id:
            raise RuntimeError(
                f"同一 WPP 网关令牌已被平台实例 {owner} 使用；"
                "一个令牌对应一个微信账号，只能由一个实例轮询"
            )
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        if self._shutdown_event.is_set():
            # terminate() 已经执行过（例如平台启用失败后被管理器清理），
            # 不再拉起任何资源。
            return
        self._ACTIVE_AUTHCODES[fp] = self.instance_id
        try:
            # ① 网络预检放在锁外：这里有两个 HTTP 往返 + 固定 sleep(2)，而
            # terminate() 要拿同一把锁；放进锁里会让「停用/删除机器人」卡到预检
            # 结束（实测可到 90s），插件热重载期间还会占着平台管理器的主锁。
            await asyncio.wait_for(self._preflight(acct), PREFLIGHT_TIMEOUT_S)
            if self._shutdown_event.is_set():
                return

            async with self._lifecycle_lock:
                if self._shutdown_event.is_set():
                    return

                # ② WPP 回调入口（统一 Webhook 模式下由 AstrBot 自己的端口代收）
                if self._use_unified_webhook():
                    self._log_unified_webhook()
                else:
                    await self._start_webhook_server()
                if self._shutdown_event.is_set():
                    return

                # ③ 后台状态循环 (检查在线 + 心跳)
                self._status_task = asyncio.create_task(self._account_status_loop())

                # ④ 启动时同步 Heartflow 白名单 (此时各插件均已加载)
                self.sync_heartflow_whitelist("startup")

            # ⑤ 持有平台主任务直到 terminate(); 后台任务异常不会让 run 提前退出。
            await self._shutdown_event.wait()
        except Exception as e:
            # 启动失败必须把已拉起的 WS/worker 停掉再抛给平台管理器；
            # 否则平台被标 ERROR 但连接仍在，重启还会撞上 authcode 锁。
            logger.error(f"[WPP:{acct.account_id}] 平台启动失败，清理资源: {e}")
            raise
        finally:
            # 正常 terminate 和异常退出都走同一套幂等清理。
            # 令牌锁必须**先**释放：AstrBot 在 terminate() 返回后会立刻 cancel 本任务，
            # 若把 pop 放在 await 之后，取消会让锁永远残留，同名/同令牌实例重建被拒。
            if self._ACTIVE_AUTHCODES.get(fp) == self.instance_id:
                self._ACTIVE_AUTHCODES.pop(fp, None)
            cleanup = self._stop_resources()
            try:
                async with self._lifecycle_lock:
                    await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # 清理任务已被 shield 保护, 取消不会打断它; 继续向上抛给管理器。
                raise

    def sync_heartflow_whitelist(self, reason: str = "") -> None:
        """把本实例所有账号的群白名单/主动候选群同步给 Heartflow。

        Heartflow 的 chat_whitelist 是全局配置: 只替换本平台实例的 UMO 条目,
        其他平台条目保持不变。同步失败只记日志, 不影响消息收发。
        """
        if not getattr(self, "heartflow_sync_enabled", True):
            return
        try:
            groups, rejected = resolve_heartflow_groups(
                self.accounts,
                getattr(self, "heartflow_groups", set()),
                getattr(self, "heartflow_whitelist_mode", "auto"),
            )
            if rejected and rejected != self._heartflow_rejected_warned:
                self._heartflow_rejected_warned = set(rejected)
                logger.warning(
                    "[WPP] Heartflow 白名单要求群白名单必须包含它们; 以下群不在 "
                    f"allow_users 群白名单, 已跳过同步: {', '.join(sorted(rejected))}。"
                    "请先 /group add 或从 Heartflow 单独白名单移除。"
                )
            if not self._heartflow_all_warned and any(
                getattr(a, "group_reply", "") == "all" for a in self.accounts
            ):
                self._heartflow_all_warned = True
                logger.warning(
                    "[WPP] group_reply=all 会放行所有群, 但 Heartflow 白名单只能覆盖"
                    "显式列出的 allow_users 群 / proactive_groups; 群白名单中的"
                    "@chatroom 会自动作为主动候选并同步心流, 无需重复配置。"
                )
            sync_heartflow_config(self.instance_id, groups, reason)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 同步 Heartflow 白名单失败: {e}")

    # -------------------------------------------------------------- webhook 两种模式
    def _use_unified_webhook(self) -> bool:
        """是否由 AstrBot 统一 Webhook 代收本实例的回调。"""
        return bool(self.unified_webhook_mode and self.webhook_uuid)

    def unified_webhook_path(self) -> str:
        """本实例在 AstrBot 上的统一回调路径（不含域名）。"""
        return f"/api/platform/webhook/{self.webhook_uuid}"

    def _unified_callback_url(self) -> str:
        """统一 Webhook 模式下 WPP 控制台应填写的完整回调地址。"""
        base = self.webhook_public_url or _astrbot_callback_base()
        if not base:
            return self.unified_webhook_path()
        return base.rstrip("/") + self.unified_webhook_path()

    def _callback_url(self) -> str:
        """WPP 控制台应填写的完整回调地址（两种模式各自解析）。"""
        if self._use_unified_webhook():
            return self._unified_callback_url()
        if not self.webhook_path:
            return ""
        if self.webhook_public_url:
            return self.webhook_public_url + self.webhook_path
        if self.webhook_host in ("0.0.0.0", "::", ""):
            return ""
        return f"http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"

    def webhook_hint(self) -> str:
        """给日志/前端看的、可以对外展示的回调地址（不含 URL token）。"""
        if self._use_unified_webhook():
            return f"{self._unified_callback_url()}"
        safe_path = self.webhook_path.rsplit("/", 1)[0] + "/<token>"
        if self.webhook_public_url:
            return self.webhook_public_url + safe_path
        return f"http://{self.webhook_host}:{self.webhook_port}{safe_path}"

    def _log_unified_webhook(self) -> None:
        """统一 Webhook 模式下不监听端口，只打印/落盘回调地址。"""
        acct = self.account
        label = (
            f"{PLATFORM_DISPLAY_NAME} · {acct.account_id}"
            if acct is not None
            else PLATFORM_DISPLAY_NAME
        )
        with contextlib.suppress(Exception):
            from astrbot.core.utils.webhook_utils import log_webhook_info

            log_webhook_info(label, self.webhook_uuid)

        callback_url = self._unified_callback_url()
        target = self._write_callback_url_file(callback_url)
        # astrbot 的 logger 是 stdlib logging：只能用 %s 占位，用 {} 会抛
        # TypeError 并把整个平台启动打挂。
        logger.info(
            "[WPP Cloud] 统一 Webhook 模式：不监听 %s:%s（由 AstrBot 端口代收，多账号共用）。"
            "请在 WPP 控制台把该账号的普通回调填为 %s（完整地址已写入 %s，勿外泄）",
            self.webhook_host,
            self.webhook_port,
            callback_url,
            target or "数据目录失败",
        )
        if self.sync_interval_s:
            logger.info(
                f"[WPP Cloud] 入站兜底: 每 {self.sync_interval_s}s 拉一次 /Msg/Sync；"
                "回调通知到达时立即拉取"
            )

    async def webhook_callback(self, request: Any) -> Any:
        """AstrBot 统一 Webhook 入口: ``/api/platform/webhook/<webhook_uuid>``。

        路径由 AstrBot 按 uuid 路由，这里不再校验 URL token；v2 HMAC 校验照旧。
        返回值必须是 AstrBot 能转换的形态（str/bytes/dict 或 ``(body, status)``），
        不能返回 aiohttp 的 ``web.Response``。
        """
        method = str(getattr(request, "method", "") or "").upper()
        if method == "GET":
            # WPP 控制台保存回调时可能先做一次可访问性探测。
            return "ok", 200
        try:
            if hasattr(request, "get_data"):
                data = await request.get_data()
                raw = (
                    data.decode("utf-8", "replace")
                    if isinstance(data, (bytes, bytearray))
                    else str(data or "")
                )
            else:
                raw = await request.text()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP Cloud] 读取回调请求体失败: {e}")
            return "", 400
        logger.debug(
            "[WPP Cloud] 收到统一 webhook 回调: method=%s path=%s len=%s",
            method,
            getattr(request, "path", ""),
            len(raw),
        )
        return await self._process_webhook(request.headers, raw)

    async def _start_webhook_server(self) -> None:
        """启动 WPP 回调接收器（仅 unified_webhook_mode=false 时使用）。

        WPP Cloud 模式下回调槽位归 WPP 控制台，本插件**不向 vendor 注册**
        ``/Webhook/*``，也不调用 ``/Msg/StartAutoSync``；只负责接收 WPP 转发的
        普通回调（v2 HMAC 校验）并触发 ``/Msg/Sync``。

        多账号共用同一 ``host:port`` 时不在各自实例里再绑定端口（那会 errno 98），
        而是复用模块级的共享 server，按各自的回调路径分发。
        """
        if not self.webhook_secret:
            logger.warning(
                "[WPP Cloud] 未配置 webhook_secret：回调只能靠 URL token 保护。"
                "建议把 WPP 控制台该账号回调的 secret 填到本插件，启用 v2 HMAC 校验。"
            )
        key = (self.webhook_host, int(self.webhook_port))
        hub = _SHARED_WEBHOOK_SERVERS.get(key)
        if hub is None:
            hub = _SharedWebhookServer(self.webhook_host, int(self.webhook_port))
            _SHARED_WEBHOOK_SERVERS[key] = hub
        try:
            await hub.ensure_started()
        except OSError as e:
            if hub.is_idle():
                _SHARED_WEBHOOK_SERVERS.pop(key, None)
            raise RuntimeError(
                f"WPP Cloud: 无法监听 {self.webhook_host}:{self.webhook_port} ({e})。"
                "该端口已被其他程序占用：可改用统一 Webhook 模式"
                "（unified_webhook_mode=true，多账号共用一个入口，推荐），"
                "或给本实例换一个 webhook_port。"
            ) from e
        hub.register(self.webhook_path, self)
        self._hub = hub
        self._runner = None
        self._server = None
        logger.info(
            "[WPP Cloud] 回调接收器已启动: %s（独立监听模式；与其他实例共用同一端口时按路径分发）",
            self.webhook_hint(),
        )

        callback_url = self._callback_url()
        if callback_url:
            target = self._write_callback_url_file(callback_url)
            logger.info(
                "[WPP Cloud] 请在 WPP 控制台把该账号的普通回调填为 %s"
                "（含 token 的完整地址已写入 %s，勿外泄）",
                self.webhook_hint(),
                target or "数据目录失败",
            )
        if self.sync_interval_s:
            logger.info(
                f"[WPP Cloud] 入站兜底: 每 {self.sync_interval_s}s 拉一次 /Msg/Sync；"
                "回调通知到达时立即拉取"
            )

    def _write_callback_url_file(self, url: str) -> str:
        """把完整回调地址写到数据目录（0600），方便管理员复制；不写进源码目录。"""
        import os

        try:
            out_dir = plugin_data_dir() / "webhooks"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{safe_account_id(self.instance_id)}.callback-url"
            path.write_text(url + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
            return str(path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP Cloud] 写入回调地址文件失败: {e}")
            return ""

    async def _account_status_loop(self) -> None:
        """后台循环: 每 30s 检查在线; 已登录但长连接断开时触发自动心跳。

        只在状态变化时输出日志: vendor 未登录/维护期间 ``Code=-1`` 是预期
        状态, 登录前持续告警没有意义 (旧版每 30s 一条 WARNING)。
        """
        while True:
            try:
                await asyncio.sleep(30)
                for acct in self.accounts:
                    try:
                        await self._check_account_online(acct)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self._set_keepalive_state(acct, "check_failed", str(e))
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] 保活循环异常: {e}")

    async def _check_account_online(self, acct: WppAccount) -> None:
        """检查单账号在线状态，必要时触发自动心跳。"""
        online, resp = await acct._api.is_online()
        if online:
            self._set_keepalive_state(acct, "online")
            await self._refresh_account_status(acct, resp)
            return

        code = resp.get("Code")
        message = safe_str(resp.get("Message"))
        if code == -1:
            # vendor 明确回复「未登录或缓存缺失」: 需要人工扫码,
            # AutoHeartBeat 不会成功, 也不应该每 30s 重试。
            self._set_keepalive_state(acct, "logged_out", message)
            return
        if code != 0:
            # -2/-3 或其它 vendor 异常: 瞬时网络/服务问题, 后台继续探测。
            self._set_keepalive_state(acct, f"api_error:{code}", message)
            return

        # Code=0 但 online=false: 账号已登录、长连接未建立。按退避触发
        # 自动心跳, 避免 vendor 故障时每 30s 打一次 AutoHeartBeat。
        now = time.monotonic()
        if now < self._keepalive_next_attempt.get(acct.account_id, 0.0):
            return
        self._keepalive_next_attempt[acct.account_id] = now + 300.0
        try:
            hb = await acct._api.auto_heartbeat()
        except Exception as e:  # noqa: BLE001 - 包含 AutoHeartBeat 返回 Code=-1
            self._set_keepalive_state(acct, "heartbeat_failed", str(e))
            return
        logger.info(
            f"[WPP:{acct.account_id}] 长连接未就绪, 已触发自动心跳: {hb.get('Message')}"
        )
        await asyncio.sleep(2)
        online, resp = await acct._api.is_online()
        if online:
            self._set_keepalive_state(acct, "online")
            await self._refresh_account_status(acct, resp)
        else:
            self._set_keepalive_state(
                acct, "reconnecting", safe_str(resp.get("Message"))
            )

    def _set_keepalive_state(
        self, acct: WppAccount, state: str, detail: str = ""
    ) -> None:
        """记录保活状态；只有状态发生变化时才输出日志。"""
        previous = self._keepalive_state.get(acct.account_id)
        if previous is not None and previous[0] == state:
            return
        self._keepalive_state[acct.account_id] = (state, detail)
        if state == "online":
            self._keepalive_next_attempt.pop(acct.account_id, None)
            if previous is not None:
                logger.info(f"[WPP:{acct.account_id}] 账号已恢复在线")
            return
        if state == "logged_out":
            logger.warning(
                f"[WPP:{acct.account_id}] 账号未登录 (vendor 缓存缺失)，自动心跳无法恢复；"
                f"请到 WPP 管理面板扫码登录: {acct.base_url}。登录前不再重复告警。"
            )
        elif state == "heartbeat_failed":
            logger.warning(
                f"[WPP:{acct.account_id}] 自动心跳失败，5 分钟内不再重试: "
                f"{detail or '-'}"
            )
        elif state == "reconnecting":
            logger.info(
                f"[WPP:{acct.account_id}] 长连接尚未恢复，后台将继续重试: "
                f"{detail or 'online=false'}"
            )
        elif state.startswith("api_error:"):
            logger.warning(
                f"[WPP:{acct.account_id}] 在线状态查询异常 "
                f"(Code={state.split(':', 1)[1]})，将持续重试: {detail or '-'}"
            )
        elif state == "check_failed":
            logger.warning(f"[WPP:{acct.account_id}] 保活检查失败: {detail}")

    async def _refresh_account_status(
        self, acct: WppAccount, resp: dict | None = None
    ) -> None:
        """刷新账号状态缓存；个人资料最多每 10 分钟拉取一次。

        状态循环每 30s 运行一次，旧实现每次都请求 GetContractProfile，
        对 vendor 造成无意义负载。昵称未知时仍会立即拉取。
        """
        try:
            if resp is None:
                online, resp = await acct._api.is_online()
            else:
                online = (
                    resp.get("Code") == 0
                    and (resp.get("Data") or {}).get("online") is True
                )
            data = resp.get("Data") or {}
            wxid = safe_str(data.get("wxid"))
            if wxid:
                acct._self_wxid = wxid
            if online:
                nickname = acct._self_nickname
                now = time.monotonic()
                if (
                    not nickname
                    or now - getattr(acct, "_profile_refreshed_at", 0.0) >= 600.0
                ):
                    prof = None
                    try:
                        prof = await acct._api.get_contract_profile()
                    except Exception:  # noqa: BLE001
                        pass
                    if prof and prof.get("Success") is not False:
                        ui = (prof.get("Data") or {}).get("userInfo") or {}
                        nv = ui.get("NickName")
                        resolved = (
                            str(nv.get("string", ""))
                            if isinstance(nv, dict)
                            else (str(nv) if nv else "")
                        )
                        if resolved:
                            nickname = resolved
                            acct._self_nickname = resolved
                        acct._profile_refreshed_at = now
                acct._account_status = {
                    "online": True,
                    "wxid": wxid,
                    "nickname": nickname,
                    "message": f"在线 · {nickname} ({wxid})",
                }
            else:
                acct._account_status = {
                    "online": False,
                    "wxid": wxid,
                    "nickname": "",
                    "message": "未登录",
                }
        except Exception as e:  # noqa: BLE001
            acct._account_status = {
                "online": None,
                "wxid": "",
                "nickname": "",
                "message": f"查询失败: {e}",
            }

    async def _ensure_logged_in(self, acct: WppAccount) -> None:
        """启动时确认账号在线；未登录只告警一次，不阻塞平台启动。"""
        try:
            online, resp = await acct._api.is_online()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{acct.account_id}] 在线状态查询失败: {e}")
            return
        if online:
            data = resp.get("Data") or {}
            logger.info(f"[WPP:{acct.account_id}] 账号已在线: wxid={data.get('wxid')}")
            self._set_keepalive_state(acct, "online")
            return

        code = resp.get("Code")
        message = safe_str(resp.get("Message"))
        if code == -1:
            # 需要人工扫码: 不在启动阶段调用必然失败的 AutoHeartBeat。
            self._set_keepalive_state(acct, "logged_out", message)
            return
        if code != 0:
            self._set_keepalive_state(acct, f"api_error:{code}", message)
            return

        # Code=0 但 online=false: 已登录、长连接未建立，尝试拉起一次。
        try:
            hb = await acct._api.auto_heartbeat()
        except Exception as e:  # noqa: BLE001
            self._set_keepalive_state(acct, "heartbeat_failed", str(e))
            return
        logger.info(
            f"[WPP:{acct.account_id}] 已触发自动心跳: {hb.get('Message')}"
        )
        self._keepalive_next_attempt[acct.account_id] = time.monotonic() + 300.0
        await asyncio.sleep(2)
        online, resp = await acct._api.is_online()
        if online:
            data = resp.get("Data") or {}
            logger.info(
                f"[WPP:{acct.account_id}] 心跳后账号已在线: wxid={data.get('wxid')}"
            )
            self._set_keepalive_state(acct, "online")
        else:
            self._set_keepalive_state(
                acct, "reconnecting", safe_str(resp.get("Message"))
            )

    async def terminate(self) -> None:
        """清理: 账号 WS/worker + 状态循环 + webhook server + authcode 锁。"""
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        self._shutdown_event.set()
        # 与 run() 的启动阶段互斥：terminate 在启动中途到达时，等启动完成
        # 再统一清理，避免“清理完又被启动代码拉起”。
        async with self._lifecycle_lock:
            await self._stop_resources()
            if self.account is not None:
                fp = authcode_fingerprint(self.account.authcode)
                if self._ACTIVE_AUTHCODES.get(fp) == self.instance_id:
                    self._ACTIVE_AUTHCODES.pop(fp, None)

    async def _stop_resources(self) -> None:
        """幂等清理：run() 的 finally 与 terminate() 都会调用。"""
        task, self._status_task = self._status_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.account is not None:
            try:
                await self.account.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] 停止账号资源失败: {e}")
        hub, self._hub = self._hub, None
        if hub is not None:
            hub.unregister(self.webhook_path)
            try:
                await hub.release()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] 清理 webhook server 失败: {e}")
        runner, self._runner = self._runner, None
        self._server = None
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] 清理 webhook server 失败: {e}")

    # ------------------------------------------------------------------ webhook 兜底
    async def _handle_webhook_health(self, request: Any) -> Any:
        from aiohttp import web

        return web.Response(text="ok")

    async def _handle_webhook(self, request: Any) -> Any:
        """接收 WPP 控制台转发的普通回调（独立监听模式）。"""
        from aiohttp import web

        req_path = request.path
        if req_path.rstrip("/") != self.webhook_path:
            # 路径里含 URL token, 不写日志 (等价于回调凭证)。
            logger.warning("[WPP Cloud] 回调 path mismatch (token 不落日志)")
            return web.Response(status=404)

        try:
            raw = await request.text()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP Cloud] 读取回调请求体失败: {e}")
            return web.Response(status=400)

        # path 里带 URL token 且已匹配 → 这条投递算已认证（legacy 的凭证就是 URL）。
        body, status = await self._process_webhook(
            request.headers, raw, path_token_ok=True
        )
        return web.Response(status=status, text=body)

    async def _process_webhook(
        self, headers: Any, raw: str, *, path_token_ok: bool = False
    ) -> tuple[str, int]:
        """两种模式共用的回调处理：v2 校验 → 解析 → 触发同步/处理消息。

        Args:
            headers: 回调请求头（v2 签名校验用）。
            raw: 回调原始请求体。
            path_token_ok: legacy 模式下请求 path 已命中带 URL token 的回调路径，
                等价于"知道凭证"，据此把投递标记为已认证。

        Returns:
            ``(body, status)``，直接给 aiohttp 包装或交给 AstrBot 统一响应。
        """
        authenticated = bool(path_token_ok)
        if self.webhook_secret:
            if not self._verify_wpp_v2_signature(headers, raw, self.webhook_secret):
                logger.warning("[WPP Cloud] 回调 v2 签名校验失败")
                return "", 403
            authenticated = True
        elif self._use_unified_webhook():
            # 统一模式的 URL 是 /api/platform/webhook/<uuid>，uuid 只是路由键
            # （由实例 id 派生、可猜），AstrBot 又把该前缀排除在 dashboard 鉴权之外
            # ⇒ 没有 secret 时任何人都能 POST 一条伪造的微信消息进 LLM 流程。
            # 因此 fail-closed：未配置 webhook_secret 就拒绝所有入站回调。
            if not self._warned_no_secret:
                self._warned_no_secret = True
                logger.error(
                    "[WPP Cloud] 统一 Webhook 模式未配置 webhook_secret，已拒绝入站回调"
                    "（否则任何拿到 uuid 的人都能伪造消息）。请在插件配置里填入该账号"
                    "在 WPP 控制台的「回调密钥」后重启。"
                )
            return "", 403
        else:
            logger.debug("[WPP Cloud] 未配置 secret，回调仅校验 URL token")

        try:
            payload = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP Cloud] 回调不是合法 JSON: {e}")
            # 返回 200：这种请求重试也不会成功，避免 WPP 无意义重投。
            return "", 200
        if not isinstance(payload, dict):
            return "", 200

        acct = self.account
        if acct is None:
            return "", 200

        srcs = extract_all_msg_src(payload)
        if not srcs:
            # WPP 普通回调常见的形态是「sync_message 通知，不带正文」
            # ⇒ 立即触发一次 /Msg/Sync；首次运行只建立游标。
            logger.info(f"[WPP:{acct.account_id}] 收到 WPP 回调通知（无正文），触发 /Msg/Sync")
            acct.request_sync(REASON_PUSH)
            return "ok", 200

        # 立刻 ack：回调里 inline 跑完整流水线（群资料 10s + 昵称 8s + 媒体 120s）
        # 会让 WPP 判定超时而重投，同一批消息被并发下载/重复提交。交给账号队列,
        # 去重和顺序由 worker 保证（_enqueue 会把认证状态一并带过去）。
        for src in srcs:
            try:
                acct._enqueue(src, verified=authenticated)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP Cloud] 回调消息入队失败: {e}")
        return "ok", 200

    @staticmethod
    def _verify_wpp_v2_signature(headers: Any, raw_body: str, secret: str) -> bool:
        """校验 WPP 回调 v2 签名。

        算法（WPP relay 与 pad 一致）::

            hex(HMAC-SHA256(secret, f"{TimestampMs}.{DeliveryID}." + raw_body))

        ``X-Webhook-Signature-Version`` 必须为空或 ``v2``；时间戳偏差默认
        ±300s（``WPP_WEBHOOK_MAX_SKEW_SEC`` 可调，0=关闭），防重放。
        """
        got = str(headers.get("X-Webhook-Signature") or "").strip()
        ts_raw = str(headers.get("X-Webhook-Timestamp-Ms") or "").strip()
        delivery = str(headers.get("X-Webhook-Delivery") or "").strip()
        version = str(headers.get("X-Webhook-Signature-Version") or "").strip().lower()
        if not got or not ts_raw or not delivery:
            return False
        if version and version != "v2":
            return False
        try:
            ts_ms = int(ts_raw)
        except ValueError:
            return False
        skew = max_skew_seconds()
        if skew and abs(int(time.time() * 1000) - ts_ms) > skew * 1000:
            return False
        base = f"{ts_raw}.{delivery}.".encode() + raw_body.encode("utf-8")
        expect = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(got.lower(), expect)

    # ------------------------------------------------------------------ 发送
    def _find_account_for_send(self, session: MessageSession) -> WppAccount | None:
        """单实例单账号：所有会话都由当前账号发送。"""
        return self.account

    async def send_by_session(self, session: MessageSession, message_chain: MessageChain) -> None:
        """AstrBot 回复/主动推送统一走这里 → 对应账号的 WPP 发送。"""
        stats: dict | None = None
        try:
            acct = self._find_account_for_send(session)
            if acct is None:
                logger.warning(
                    f"[WPP] 当前平台实例未配置 authcode，无法发送到 {session.session_id}"
                )
                return
            logger.info(f"[WPP:{acct.account_id}] send_by_session: to={session.session_id}")
            stats = await send_chain(
                acct._api,
                session.session_id,
                list(message_chain.chain),
                account_id=acct.account_id,
            )
        finally:
            # 基类负责 metric 上报
            await super().send_by_session(session, message_chain)
        if stats and stats.get("failed") and not stats.get("sent"):
            detail = "; ".join(str(e) for e in stats.get("errors", [])[:3])
            raise RuntimeError(f"WPP send_by_session 全部失败: {detail or 'unknown error'}")
        if stats and stats.get("failed"):
            logger.warning(
                f"[WPP] send_by_session 部分失败 (sent={stats.get('sent')}, "
                f"failed={stats.get('failed')}): {stats.get('errors')}"
            )


def _webhook_token_for(instance_id: str) -> str:
    """按实例 id 加载/创建 webhook token (存插件数据目录, 不写插件源码目录)。"""
    import os

    token_dir = plugin_data_dir() / "webhooks"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / f"{safe_account_id(instance_id)}.token"
    try:
        if token_file.exists():
            tok = token_file.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except Exception:  # noqa: BLE001
        pass
    tok = secrets.token_hex(16)
    try:
        token_file.write_text(tok, encoding="utf-8")
        os.chmod(token_file, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return tok


def _register_legacy_platform_aliases() -> None:
    """把历史技术名映射到同一个适配器类，避免老实例升级后加载失败。

    老版本把平台技术名注册成 ``WPP Cloud（个人微信托管）``，实例配置里的
    ``type`` 就是它。技术名改成 ``个人微信(Pad协议)`` 之后，
    ``astrbot/core/platform/manager.py:231`` 只按 ``type`` 查
    ``platform_cls_map``，查不到就会抛「Platform adapter not found」。
    这里只补 ``platform_cls_map``（不带 ``default_config_tmpl``），因此
    ``get_astrbot_config()`` 里 ``if not platform.default_config_tmpl: continue``
    会把别名跳过，创建机器人下拉里不会多出重复项。
    """
    from astrbot.core.platform.register import platform_cls_map

    for legacy in LEGACY_PLATFORM_NAMES:
        if legacy and legacy != PLATFORM_NAME:
            # setdefault: 别的插件若已占用同名注册项, 不覆盖它。
            platform_cls_map.setdefault(legacy, WppPlatformAdapter)


_register_legacy_platform_aliases()
