# astrbot_plugin_wpp_cloud

AstrBot 的 **WPP Cloud 托管个人微信** 平台适配器。它是原
[`astrbot_plugin_wpp`](https://github.com/jiexiaoyin/astrbot-plugin-wpp) 的
**WPP-only 分支**：只适配 WPP 托管网关，不再直连 vendor。

| 方向 | 本插件（WPP Cloud） | 原 vendor 插件 |
| --- | --- | --- |
| 出站 | `https://<host>/api/v1/gateway` + `Authorization: Bearer wxa_...` | `https://<host>/api/...?authcode=...` |
| 凭据 | WPP 网关令牌（每托管账号一把） | vendor authcode + TokenKey |
| 入站主通道 | WPP 控制台「普通回调」→ 本插件 webhook | vendor `/Webhook/Set` |
| 入站兜底 | `/Msg/Sync` 每 60s + 收到通知立即拉 | vendor WS + `/Msg/Sync` |
| vendor WS | **不建立** | `wss://.../ws/sync` |
| vendor 回调注册 | **不注册**（槽位归 WPP） | `/Webhook/*`、`/Msg/StartAutoSync` |

平台技术名是 **`个人微信(Pad协议)`**（= 配置里的 `type` = Dashboard「创建机器人 →
选择消息平台类别」里显示的那一行，AstrBot 前端就是拿 `config_template` 的键
当选项文案）。它和旧插件 `astrbot_plugin_wpp` 的技术名不同，两个插件可以同时
安装；老版本的实例 `type` 是 `WPP Cloud（个人微信托管）`，升级后会自动迁移
（插件同时保留该名字的加载别名，迁移前也不会启动失败）。

## 1. WPP 控制台准备

1. 账号已导入/托管在 WPP（Authcode 属于共享生产客户端）。
2. 在账号详情里**签发网关令牌**：`wxa_...`（只完整展示一次；重新签发会使旧令牌失效）。
3. 启动本插件后，从数据目录读取回调地址：

   ```bash
   cat /AstrBot/data/plugin_data/astrbot_plugin_wpp_cloud/webhooks/<实例id>.callback-url
   ```

   把该地址填到 WPP 控制台该账号的**普通回调**。若账号仍显示「导入 / 只读」，
   管理员先在账号详情点「转为云托管」再保存（不轮换 authcode、不掉线）；
   已转托管账号直接保存即可，无需 `?takeover=1`。business 回调不受影响。

4. 回调的 customer secret 填到本插件的 `webhook_secret`。WPP 会发送 v2 签名：

   ```
   X-Webhook-Signature = hex(HMAC-SHA256(secret, f"{TimestampMs}.{DeliveryID}." + raw_body))
   X-Webhook-Timestamp-Ms / X-Webhook-Delivery / X-Webhook-Signature-Version: v2
   ```

   未填 secret 时只靠 URL 里的随机 token 鉴权（可用但不推荐）。

## 2. 安装与配置

```bash
# 宿主机
docker cp astrbot_plugin_wpp_cloud astrbot:/AstrBot/data/plugins/
docker restart astrbot
```

Dashboard → 平台适配器 → 添加 **个人微信(Pad协议)**，一个实例填一个账号：

| 字段 | 说明 |
| --- | --- |
| `wpp_gateway_url` | `https://<平台域名>/api/v1/gateway`；也可只填 `https://<平台域名>` |
| `wpp_gateway_token` | WPP 网关令牌 `wxa_...` |
| `unified_webhook_mode` | **默认开启**。回调走 AstrBot 自己的 `/api/platform/webhook/<webhook_uuid>`，多个账号实例共用 AstrBot 端口，不需要各自监听端口 |
| `webhook_uuid` | 统一 Webhook 标识，留空按实例 id 自动派生（`sha256("<实例id>:wpp-cloud")[:16]`，重启后地址不变），它就是回调地址最后一段 |
| `webhook_public_url` | 公网前缀，例如 `https://astrbot.example.com`；统一模式下留空则用 AstrBot 的 `callback_api_base` |
| `webhook_secret` | 与 WPP 控制台该账号回调的 secret 一致（v2 HMAC） |
| `webhook_host` / `webhook_port` / `webhook_path` / `webhook_token` | **仅 `unified_webhook_mode=false` 时生效**（插件自己监听；须把该域名的 `/wpp-cloud/` 反代到该端口） |
| `wpp_sync_interval` | 兜底轮询秒数，默认 60 |
| 其余 `wpp_*` | 白名单/群策略/节流/Heartflow 同步，与原插件一致 |

多账号 = 多个平台实例（id 唯一、各自令牌）。同一个令牌只能由一个实例轮询。

### 多账号（≥2 个托管微信）

新建实例在 Dashboard 里只需填 `wpp_gateway_url` / `wpp_gateway_token`，回调地址自动生成：

| 模式 | 回调地址 | 端口占用 |
| --- | --- | --- |
| 统一 Webhook（默认） | `https://<域名>/api/platform/webhook/<webhook_uuid>` | 不额外占用，所有实例共用 AstrBot 端口 |
| 独立监听（`unified_webhook_mode=false`） | `https://<域名>/wpp-cloud/webhook/<token>` | 需各自独立端口；同一 `host:port` 上的多个实例会复用同一个 server、按路径分发（旧版会 `errno 98` 启动失败） |

每个实例的完整回调地址写在
`plugin_data/astrbot_plugin_wpp_cloud/webhooks/<实例id>.callback-url`，也可从启动日志
「请在 WPP 控制台把该账号的普通回调填为 …」直接复制，或点机器人页的「查看 Webhook」。
**每个 WPP 账号的回调要填同一条实例的地址**，不要串号。

老实例（配置里没有 `unified_webhook_mode` 键）保持独立监听、行为不变；确认要迁移时，
把开关打开并重启，再到 WPP 控制台把该账号回调改成新地址即可。

`webhook_uuid` 留空时由实例 id 确定性派生（`sha256("<实例id>:wpp-cloud")[:16]`），重启地址不变；
也可以自己填一个固定值。改动 `webhook_uuid` 等于换回调地址，记得同步 WPP 控制台。

⚠️ **统一 Webhook 模式必须填 `webhook_secret`**（WPP 控制台该账号的「回调密钥」）。
uuid 只是路由键、不是凭证，AstrBot 又把 `/api/platform/webhook*` 排除在 Dashboard 鉴权之外：
没配 secret 时插件会**拒绝所有入站回调**（403 + 一条 ERROR 日志），避免任何人伪造微信消息。
独立监听模式以 URL 里的 token 为凭证，可以不配 secret，但配了更安全（会额外校验 v2 签名）。

配置项的运行时优先级：`allow_users` / `block_users` / `proactive_groups` / `blacklist_groups` /
`require_at_mention` / `private_allow_all` / `group_reply` 这几个「策略键」以
`plugin_data/astrbot_plugin_wpp_cloud/accounts/<账号>/config.json`（聊天命令 `/user`、`/group`
写的文件）为准，平台配置里的值只是首次运行的种子值；被覆盖时启动日志会打一条 INFO。
其余配置键（网关地址/令牌、webhook_*、媒体与同步参数）始终以平台配置为准。

## 3. 入站语义

- **回调通知**：WPP 普通回调常只推 `sync_message` 通知（无正文）。收到后立即触发
  一次 `/Msg/Sync`，不阻塞回调响应。
- **首次不回放**：本机第一次同步只保存 `KeyBuf` 游标，不派发厂商返回的历史消息
  —— 否则重启后会回复旧消息。
- **先派发、后前进游标**：进程中途退出时下一轮会重复拉到同一批，由 `msg_id`
  去重兜底（at-least-once）。
- **失败只告警**：`/Msg/Sync` 失败不会停掉平台；下一轮定时器继续重试。

## 4. 网关边界

WPP 网关只开放业务面，本插件在本地先挡一次并给出可行动的错误：

- 屏蔽家族：`Admin` / `Webhook` / `Runtime` / `Customized`；
- Login 只放行：`GetLoginStatus`、`LongLinkStatus`、`HeartBeatLong`、`AutoHeartBeat`；
- 屏蔽操作：`/Tools/SetProxy`、`/Tools/GetBoundHardDevices`、
  `/Msg/StartAutoSync`、`/Msg/StopAutoSync`。

## 5. 排障

| 现象 | 先看 |
| --- | --- |
| 平台未启用 | 是否填了 `wpp_gateway_url` + `wpp_gateway_token`；启动日志 `账号已注册` |
| 出站 401 | 令牌是否被重新签发、账号是否 active、网关地址是否以 `/api/v1/gateway` 结尾 |
| 入站全丢 | WPP 控制台回调地址是否等于 `callback-url` 文件内容；插件端口是否公网可达 |
| 回调 403 | `webhook_secret` 是否与 WPP 控制台一致；服务器时钟是否偏差 >300s |
| 只收到通知不回复 | 日志 `收到 WPP 回调通知（无正文），触发 /Msg/Sync`；再查 `/Msg/Sync` 是否报错 |
| 重启后回复旧消息 | 检查 `accounts/<id>/sync.json` 是否存在；删除它会让下一次同步重新建立基线 |

## 6. 测试

```bash
docker cp . astrbot:/tmp/wpp-cloud-test/astrbot_plugin_wpp_cloud
docker exec -w /tmp/wpp-cloud-test astrbot python3 -m pytest \
  astrbot_plugin_wpp_cloud/tests -q
```

`tests/test_cloud_client.py` 钉住网关 URL/Bearer/控制面边界；
`tests/test_sync_fallback.py` 钉住首次不回放、游标顺序与失败处理；
`tests/test_webhook_guard.py` 钉住 v2 HMAC 与时间戳防重放。

## 仓库约定与自动同步

- 协作/agent 规则见 [AGENTS.md](AGENTS.md)：`git commit` 后由 hook 自动推送，另有
  5 分钟兜底的 systemd timer；手动同步 `bash scripts/git-autosync.sh`。
- 提交前敏感信息扫描：`python3 scripts/check_public_tree.py`（pre-commit 也会跑）。
- 部署步骤见 [DEPLOY.md](DEPLOY.md)。

