# 部署说明

## 路径

| 项 | 值 |
| --- | --- |
| 宿主机源码 | 本仓库任意路径（下文以 `~/dev/astrbot-plugin-wpp-cloud` 为例） |
| 容器插件目录 | `/AstrBot/data/plugins/astrbot_plugin_wpp_cloud` |
| 容器运行时数据 | `/AstrBot/data/plugin_data/astrbot_plugin_wpp_cloud` |
| 回调地址文件 | `.../plugin_data/astrbot_plugin_wpp_cloud/webhooks/<实例id>.callback-url` |

## 首次部署

```bash
cd ~/dev/astrbot-plugin-wpp-cloud
bash deploy.sh
```

`deploy.sh` 只做三件事：整目录拷贝（排除 tests/缓存）→ 检查 `aiohttp` → 重启
AstrBot。不会改 AstrBot 核心代码，也不会自行注册任何回调（回调由平台控制台管理）。

## 在 Dashboard 添加实例

1. 平台适配器 → 添加 **个人微信(Pad协议)**（下拉里的名字就是技术名）。
2. 填 `wpp_gateway_url`、`wpp_gateway_token`；`webhook_public_url` 填
   **AstrBot 自己的公网域名**（例如 `https://astrbot.example.com`）。
   `unified_webhook_mode` 保持默认开启：回调走
   `https://<域名>/api/platform/webhook/<webhook_uuid>`，由 AstrBot 端口代收，
   多账号不用各自开端口，也不需要为 `/wpp-cloud/` 再配一条反代。
   只有关掉它（`unified_webhook_mode=false`）才需要把 `/wpp-cloud/` 反代到本机
   6199（或你改过的 `webhook_port`）。不要填 `http://<公网 IP>:6199`：IP 直连
   没有 TLS，换机器/换 IP 就断，还会把回调端口直接暴露在公网。
3. 保存并启用；启动日志会打印（脱敏后的）回调地址与文件路径。
4. 打开 `callback-url` 文件，把完整地址填到 WPP 控制台该账号的普通回调。
5. 在 WPP 控制台把回调 secret 设成与 `webhook_secret` 相同。

> 多账号：每个实例单独一条回调地址（统一模式下 uuid 不同，独立监听模式下 token
> 不同），务必把**各自那条**填到对应账号。独立监听模式下多个实例填同一端口不会
> 再报 `errno 98`——它们会复用同一个 server、按路径分发。

## 切换旧插件账号

1. 先停用旧插件里的同一微信实例（避免两边同时拉消息）。
2. 新增本插件实例，填同一账号的 WPP 网关令牌。
3. 在 WPP 控制台接管回调；若账号仍是导入只读，管理员可先点「转为云托管」，
   再普通接管（无需 `?takeover=1`）。
4. 观察日志：`账号已注册` → `回调接收器已启动` → `首次同步：已建立游标`。

## 备份与回滚

- 备份运行时数据：`plugin_data/astrbot_plugin_wpp_cloud/`（含白名单、去重、
  `/Msg/Sync` 游标）。
- 回滚：停用本插件实例 → 重新启用旧插件实例 → 在 WPP 控制台把回调改回旧地址
  （或旧插件自己的回调注册）。
- 不要复用旧插件的 `accounts/<id>/sync.json` 之外的运行时目录；两者平台名不同。

## 验证

```bash
docker cp . astrbot:/tmp/wpp-cloud-test/astrbot_plugin_wpp_cloud
docker exec -w /tmp/wpp-cloud-test astrbot python3 -m pytest \
  astrbot_plugin_wpp_cloud/tests -q
```

测试只在 `/tmp` 中运行，不会加载到 AstrBot 的已安装插件列表。
