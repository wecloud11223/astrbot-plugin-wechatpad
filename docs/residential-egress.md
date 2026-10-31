# AID 住宅出口使用方案

> 把**扫码登录**的网络出口固定到自己的公网主机：登录流量从该主机出去，托管账号的
> 后续调用仍走平台网关。用于降低「平台共享出口 IP 与你常驻地点不一致」带来的登录风控。
>
> 操作入口：[云端社交聚合平台](https://cloud.juhe.chat) → **住宅出口**（`/egress`）。
> 出口只影响登录网络，**不影响**消息回调、MCP 与网关调用路径；插件侧无需额外配置。

## 组成

| 组件 | 位置 | 作用 |
| --- | --- | --- |
| 出口记录 | 平台控制台 | 保存 `aid` / `agent_id` / `agent_token` 与来源 CIDR |
| `cloud-agent` | 你的公网主机 | 通过 WSS 长连接到平台，并在本机开一个受限 SOCKS5 |
| 绑定 | 扫码登录时选择 | 该账号的登录请求固定经这条出口发出 |

平台侧由出口网关承接控制面与数据面：扫码登录时平台把这条连接注入 Pad，
Pad 的登录请求就经由你的主机出网。

## 准备条件

- 一台有公网访问能力的主机：Linux / macOS / Windows 均可（家宽、云主机、NAS、树莓派都行）。
- 主机能出站访问 `https://cloud.juhe.chat:443` 与微信登录域名（默认白名单见下方配置）。
- 控制台「住宅出口」页面权限（管理员或已开通的租户）。

## 步骤 1 · 创建出口

1. 打开控制台 **住宅出口 → 新建出口**。
2. **来源 CIDR 可以直接留空**：托管模式下由平台服务器拨号，系统会自动填入平台出口白名单。
   只有自己的 Pad 从其它固定 IP 接入时，才需要填写来源地址（例如 `203.0.113.7/32`）。
3. 创建成功后会弹出「配置指引」，其中 `aid`、`agent_id`、`agent_token` 已填好完整的
   `config.json`，直接复制即可。

> **`agent_token` 只在创建时展示一次**。丢失后只能在该出口的「编辑 → 轮换 token」
> 重新生成；轮换会让旧 token 立即失效。

## 步骤 2 · 下载客户端

控制台「住宅出口」页底部提供各平台安装包（按系统与架构分组，随平台清单更新）：

| 系统 | 架构 |
| --- | --- |
| Linux | x86_64 / ARM64 / ARMv7（树莓派 / 路由器 / NAS）/ x86 32 位 |
| macOS | Intel / Apple 芯片 |
| Windows | x64 / ARM64 |

Linux / macOS 下载后需要 `chmod +x`。版本与校验值以控制台清单为准。

## 步骤 3 · 写入配置

把控制台生成的 JSON 保存到客户端数据目录下的 `config.json`（权限 `0600`）：

| 系统 | 数据目录 |
| --- | --- |
| Linux | `~/.config/wechatpad-cloud-agent` |
| macOS | `~/Library/Application Support/wechatpad-cloud-agent` |
| Windows | `%AppData%\wechatpad-cloud-agent` |

```json
{
  "control_url": "https://cloud.juhe.chat",
  "aid": "创建出口时返回的 aid",
  "agent_id": "创建出口时返回的 agent_id",
  "agent_token": "创建出口时返回的 agent_token",
  "socks_listen": "127.0.0.1:1080",
  "allowed_hosts": [
    "*.weixin.qq.com:80",
    "*.weixin.qq.com:443",
    "*.wechat.com:80",
    "*.wechat.com:443",
    "weixin110.qq.com:443"
  ]
}
```

说明：

- 一份配置已包含 `aid` / `agent_id` / `agent_token`，**不需要**单独写 `identity.json`
  （旧版本客户端仍兼容该文件）；客户端首次启动会把 `agent_id` 同步进去。
- `socks_listen` 保持 `127.0.0.1`，不要监听公网地址。
- `allowed_hosts` 是**双侧**白名单：客户端与平台各一份，两边必须一致；
  收窄时至少要保留微信登录域名，否则登录会失败。

## 步骤 4 · 启动并确认在线

```bash
chmod +x cloud-agent-linux-amd64
./cloud-agent-linux-amd64 --config ~/.config/wechatpad-cloud-agent/config.json
```

也可以用环境变量 `CLOUD_AGENT_TOKEN` 覆盖 token，或 `--config` 指定配置文件路径。

启动后回到控制台，出口列表应显示**在线**。长期运行建议交给
systemd / launchd / Windows 服务托管，并配置开机自启与日志轮转：

```ini
# /etc/systemd/system/wechatpad-cloud-agent.service
[Unit]
Description=WechatPad Cloud Agent (AID residential egress)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/cloud-agent-linux-amd64 --config %h/.config/wechatpad-cloud-agent/config.json
Restart=always
RestartSec=5
User=your-user

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now wechatpad-cloud-agent
```

## 步骤 5 · 绑定到账号

在控制台**扫码登录托管账号时选择这条出口**。绑定后：

- 该账号的登录请求固定从这条出口出去；
- 已经登录的账号不受出口离线影响，继续照常收发消息；
- 出口离线或 token 被吊销时，**重新登录会失败**，恢复出口后重试即可。

## 运维建议

| 事项 | 做法 |
| --- | --- |
| 主机变更 / 换网 | 更新出口记录或重建 agent，登录时重新选择出口 |
| token 泄露 | 控制台「编辑 → 轮换 token」，旧 token 立即失效 |
| 出口不再使用 | 先解绑账号，再删除出口记录；删除不会影响已登录账号 |
| 多账号 | 每条出口可绑定多个账号，但建议按登录地点拆分，便于排障 |
| 监控 | 控制台出口列表显示在线状态；主机侧看 agent 日志与进程存活 |

## 故障排查

| 现象 | 排查 |
| --- | --- |
| `registration rejected` | `agent_id` 与平台记录不一致，或 token 已被轮换 / 吊销 |
| 一直显示离线 | 主机能否出站访问 `https://cloud.juhe.chat:443`；系统时间是否准确；`control_url` 是否填了平台域名 |
| `socks_auth_failed` 或登录失败 | `socks_listen` 必须是 `127.0.0.1`；`allowed_hosts` 未被收窄到缺少微信登录域名 |
| 端口冲突 | 修改 `socks_listen`（例如 `127.0.0.1:1081`）后重启 agent |
| 登录后消息仍不通 | 与出口无关：检查网关令牌、回调地址与插件入站端口，见 [README 排障](../README.md#排障) |

## 与插件的关系

| 能力 | 是否经过住宅出口 |
| --- | --- |
| 扫码登录 / 重新登录 | ✅ 经过 |
| 已登录账号的消息收发 | ❌ 插件直连平台网关 |
| 普通回调（平台 → 插件） | ❌ 平台直接投递 |
| MCP / 网关 API 调用 | ❌ 直连平台网关 |

因此出口只解决「登录 IP」问题；账号登录成功后，插件侧只需要网关地址与令牌。

---

相关：[Pad 人脸 / 滑块验证使用方案](face-verify.md)、[安装与部署](../DEPLOY.md)。
