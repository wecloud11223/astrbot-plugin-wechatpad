# astrbot-plugin-wechatpad

**个人微信（Pad 协议）· 云端托管 · AstrBot 平台插件**

把个人微信号接入 AstrBot，不需要自己维护协议端、模拟器或常驻设备：账号托管在
[**云端社交聚合平台**](https://cloud.juhe.chat)，扫码即用、7×24 在线；插件只对接
平台网关，用一把 `wxa_` 令牌完成收发。

- 平台：AstrBot `>= 4.16.0, < 5`
- 版本：`1.3.0`
- 许可：MIT

<!-- 一张架构图胜过三段文字：见下文「架构」 -->

---

## 为什么走云端托管

| | 自建协议端 | **本插件 + 云端托管平台** |
| --- | --- | --- |
| 接入方式 | 部署协议服务、维护设备/模拟器、跟随协议版本升级 | 控制台**扫码托管**，插件只填网关地址 + 令牌 |
| 稳定性 | 掉线、风控、会话失效都要自己兜 | 平台侧会话保持、心跳与重连，账号 7×24 托管 |
| 多账号 | 多进程、多端口、多套配置 | 一个账号一个平台实例；共用 AstrBot 端口（统一 Webhook） |
| 可观测性 | 只有自己的日志 | 平台控制台 + 插件健康统计 + 回调/同步日志 |
| 风控边界 | 全量 API 暴露 | 网关只开放业务面，管理/回调注册类接口在本地直接拦截 |

一句话：**你负责业务，协议、会话和稳定性交给平台。**

---

## 核心能力

### 消息通道

- 出站：文本、图片、语音（SILK）、视频、文件、链接卡片、位置、@全体；
  长回复自动引用分条，引用回复可配置前缀。
- 入站：文本、图片、语音、视频、文件、小程序/链接卡片、位置、引用消息、
  群成员变动等事件统一解析成 AstrBot 消息段。
- 媒体：图片/语音等内联媒体默认 **20MB** 上限、文件发送 **100MB** 上限；
  编码走线程池，不阻塞事件循环。

### 双通道入站

| 通道 | 作用 |
| --- | --- |
| WPP 普通回调（webhook） | 主通道；支持 v2 HMAC 验签（±300s 时间戳窗口）、缺 secret 时 fail-closed |
| `/Msg/Sync` 兜底 | 默认每 60s 拉一次；收到回调通知立刻补拉，回调线程不等待 |

- **首次同步不回放**：只落 `KeyBuf` 游标，重启后不会逐条回复历史消息。
- **先派发、后前进游标**：进程中途退出最多重复处理，由 `msg_id` 去重兜底
  （at-least-once，不会静默丢）。
- **失败只告警**：同步失败不影响平台运行，下一轮继续。

### 多账号

一个平台实例对应一个托管账号（实例 id 唯一、令牌唯一）。多个实例可以：

- **统一 Webhook（默认开启）**：全部走 AstrBot 自己的
  `/api/platform/webhook/<webhook_uuid>`，**共用一个端口**，无需为每个账号开反代；
  `webhook_uuid` 缺省时由实例 id 确定性派生（`sha256("<实例id>:wpp-cloud")[:16]`），
  重启地址不变。
- **独立监听**（`unified_webhook_mode=false`）：插件自己监听，同一 host:port 的多个
  实例复用同一个 server、按路径分发（不再出现 `errno 98`）。

### 群运营与策略

- 白名单 / 黑名单 / 群静默 / 私聊放行策略 / 群回复开关 / 必须 @ 才回复。
- 进群欢迎语（模板变量、@ 新人、可选润色）。
- 复读保护：同内容在窗口内重复发送会被抑制，且**只在确认发送成功后记账**。
- Heartflow 同步、语音转写等能力与平台配置保持一致。

### 运维体验

- Dashboard 平台页可直接查看连接状态与账号信息。
- 平台配置**自动归一化**：补缺失键、纠正类型、夹取区间、枚举校验，只记录键名；
  只有值真的变了才落盘。
- 老实例的旧平台名会被自动迁移，升级不用手工改配置。

---

## 架构

```
 手机微信（真实账号）
      │  扫码登录，会话由平台维持
      ▼
┌─────────────────────────────┐
│ 云端社交聚合平台 cloud.juhe.chat │
│  扫码托管 · 会话保持 · 心跳重连   │
└──────────────┬──────────────┘
               │  出站：POST /api/v1/gateway + Authorization: Bearer wxa_…
               │  入站：普通回调（v2 HMAC）/ 通知触发 /Msg/Sync
               ▼
┌─────────────────────────────┐        ┌──────────────────┐
│ astrbot-plugin-wechatpad     │◄──────►│      AstrBot      │
│  平台适配器 · 回调 · 兜底同步   │        │  LLM / 插件 / 指令 │
└─────────────────────────────┘        └──────────────────┘
```

---

## 快速开始

### 1. 在平台上托管账号

账号导入 [云端社交聚合平台](https://cloud.juhe.chat) 后，在账号详情里**签发网关令牌**
（`wxa_…`，只完整展示一次，重新签发会让旧令牌失效）。

### 2. 安装插件

```bash
docker cp astrbot_plugin_wpp_cloud astrbot:/AstrBot/data/plugins/
docker restart astrbot
```

（仓库名与本插件对外名称是 `astrbot-plugin-wechatpad`；内部包名
`astrbot_plugin_wpp_cloud` 为保证既有部署与插件数据目录兼容而保留。）

### 3. 在 Dashboard 添加平台

平台适配器 → 添加 **个人微信(Pad协议)**，一个实例填一个账号：

| 字段 | 说明 |
| --- | --- |
| `wpp_gateway_url` | `https://<平台域名>/api/v1/gateway`，也可只填 `https://<平台域名>` |
| `wpp_gateway_token` | 平台签发的网关令牌 `wxa_…` |
| `unified_webhook_mode` | 默认开启，多账号共用 AstrBot 端口 |
| `webhook_uuid` | 统一 Webhook 标识，留空按实例 id 派生 |
| `webhook_public_url` | 公网前缀；留空则用 AstrBot 的 `callback_api_base` |
| `webhook_secret` | 与平台控制台该账号回调的 secret 一致（v2 HMAC） |
| `webhook_host` / `webhook_port` / `webhook_path` / `webhook_token` | 仅独立监听模式生效 |
| `wpp_sync_interval` | 兜底轮询秒数，默认 60 |

### 4. 回填回调地址

实例的回调地址写在
`plugin_data/astrbot_plugin_wpp_cloud/webhooks/<实例id>.callback-url`，启动日志也会
打印。把它填到平台控制台该账号的**普通回调**，secret 与 `webhook_secret` 保持一致。

> 统一 Webhook 模式**必须**填 `webhook_secret`：uuid 只是路由键、不是凭证，而
> `/api/platform/webhook*` 不在 Dashboard 鉴权范围内。没配 secret 时插件会拒绝所有
> 入站回调（403 + ERROR 日志），避免消息伪造。

### 5. 验证

给这个微信号发一条私聊消息，日志应出现「收到 WPP 消息通知」→ 派发 → 回复。
平台页能看到账号状态；`/help` 可确认插件已加载。

---

## 安全边界

平台网关只开放业务面：控制面、回调注册、运行时管理等非业务接口由客户端本地预挡，
越界调用会直接返回可读错误，不会打到平台。具体策略随平台版本调整，不在文档中展开。

## 可靠性细节

| 场景 | 行为 |
| --- | --- |
| 网关返回 408 / 429 / 503 | 写操作也**重试一次**，优先遵循 `Retry-After`（夹到 0.2–10s） |
| 网关返回 500 / 502 / 504 | 写操作不重试（避免重复发送），读操作照常重试 |
| 同一目标并发发送 | 按 ToWxid 串行 + 间隔节流，降低风控概率 |
| 同一条消息重复投递 | `msg_id` 去重（at-least-once） |
| 媒体过大 | 超限直接给出可读错误，不把内存打爆 |

## 排障

| 现象 | 先看 |
| --- | --- |
| 平台未启用 | `wpp_gateway_url` + `wpp_gateway_token` 是否都填了；启动日志「账号已注册」 |
| 出站 401 | 令牌是否被重新签发、账号是否 active、地址是否以 `/api/v1/gateway` 结尾 |
| 入站全丢 | 平台回调地址是否等于 `callback-url` 文件内容；插件端口是否公网可达 |
| 回调 403 | `webhook_secret` 是否一致；服务器时钟偏差是否 > 300s |
| 只收到通知不回复 | 日志「收到 WPP 回调通知（无正文），触发 /Msg/Sync」；再看 `/Msg/Sync` 报错 |
| 重启后回复旧消息 | 检查 `accounts/<id>/sync.json` 是否存在 |

更多排障与部署细节见 [DEPLOY.md](DEPLOY.md)，变更历史见 [CHANGELOG.md](CHANGELOG.md)。

## 测试

测试依赖 AstrBot 运行时，在官方镜像里跑（目录名必须等于包名）：

```bash
docker run --rm \
  -v "$PWD:/AstrBot/data/plugins/astrbot_plugin_wpp_cloud" \
  -w /AstrBot/data/plugins/astrbot_plugin_wpp_cloud \
  soulter/astrbot:v4.29.1 \
  python -m pytest tests -q
```

`tests/test_cloud_client.py` 钉住网关地址/Bearer/控制面边界；
`tests/test_sync_fallback.py` 钉住首次不回放、游标顺序与失败处理；
`tests/test_webhook_guard.py` 钉住 v2 HMAC 与时间戳防重放；
`tests/test_unified_webhook.py` 钉住统一 Webhook 地址派生与多实例共存。

## 链接

- 云端社交聚合平台：<https://cloud.juhe.chat>
- 问题反馈：<https://github.com/wecloud11223/astrbot-plugin-wechatpad/issues>
- 协作与自动同步约定：[AGENTS.md](AGENTS.md)（commit 后 hook 自动推送 + 定时兜底；
  提交前跑 `python3 scripts/check_public_tree.py`）
