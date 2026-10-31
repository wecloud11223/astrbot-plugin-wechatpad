# AGENTS.md — 仓库约定（给所有 AI agent / 协作者）

本仓库是 **AstrBot 个人微信（Pad 协议 / WPP Cloud）平台插件**的唯一事实来源。

- 本地路径（示例）：`~/dev/astrbot-plugin-wpp-cloud`
- GitHub：<https://github.com/wecloud11223/astrbot-plugin-wechatpad>（默认分支 `main`）
- 插件标识：`astrbot_plugin_wpp_cloud`；平台名 **个人微信(Pad协议)**；配置 `type` = `wpp_cloud`
  （`LEGACY_PLATFORM_NAMES` 负责旧 `type` 自动迁移；`WEBHOOK_UUID_SEED_SUFFIX="wpp-cloud"`
  固定，改显示名不会换回调地址）

## 🔁 git 自动同步（本仓库已配置，任何人/agent 修改都会自动进 GitHub）

| 链路 | 触发时机 | 命令/位置 |
| --- | --- | --- |
| **手动（推荐）** | 改完就提交 | `bash scripts/git-autosync.sh` |
| **post-commit hook** | `git commit` 成功后自动 `push` | `scripts/git-hooks/post-commit`（后台推送） |
| **systemd timer 兜底** | 每 5 分钟 | `scripts/systemd/wpp-git-autosync.timer` → 逐仓库跑 `scripts/git-autosync.sh` |

- `git-autosync.sh` 幂等：无改动什么都不做；有改动才 `git add -A` → 敏感扫描 →
  提交（`chore(autosync): <时间>`）→ 推送；推送失败提交留在本地、下一轮重试。
- 新机器/新克隆装一次：

  ```bash
  bash scripts/install-git-hooks.sh            # pre-commit 扫描 + post-commit 推送
  sudo bash scripts/install-autosync-timer.sh  # 5 分钟兜底 timer（建议）
  ```

- SSH 走专名 Host（不干扰本机其他 GitHub 账号）：

  ```
  Host github-wecloud11223
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_github
    IdentitiesOnly yes
  ```

  远端：`git@github-wecloud11223:wecloud11223/astrbot-plugin-wechatpad.git`。

## 🚫 铁律

1. **不要改 AstrBot 核心**：只动本插件目录；`/AstrBot/data/` 下是容器运行时数据
   （含账号凭据与回调地址），永远不进仓库。
2. **不提交凭证**：`accounts/`、`data/`、`.webhook_token` 已在 `.gitignore`；
   真实 `wpp_gateway_token`（`wxa_...`）、回调 secret、生产 wxid/群号都不许出现。
3. **公开仓**：`scripts/check_public_tree.py` 会拦截厂商域名、内网地址、本机绝对路径、
   真实标识形态；pre-commit hook 会跑它。本机专属黑名单放 `public_tree_denylist.txt`
   （不入库，一行一个正则）。
4. **改行为必须带测试**：`tests/` 覆盖每个语义分支；行为变更同步 `CHANGELOG.md`。
5. 插件目录是**扁平模块**（`main.py` + `wpp_*.py`），新增模块记得进 `metadata.yaml`
   与 `tests/test_config_uniformity.py` 的期望清单（如有）。

## ✅ 提交前自检

```bash
ruff check .                      # 或 ~/.local/bin/ruff
python3 -m pytest tests -q
python3 scripts/check_public_tree.py
```

## 📝 提交信息

Conventional Commits + 中文摘要，一个提交只做一件事：

```
feat(webhook): 统一 Webhook 支持多账号按路径分发
fix(send): 写端点 408/429/503 重试一次并尊重 Retry-After
docs: 补充 callback-url 填写步骤
```

## 📦 部署

`bash deploy.sh` 把本目录同步进容器（排除 `tests/`、缓存），不碰 AstrBot 核心，
也不向 vendor 注册回调；具体步骤见 [DEPLOY.md](DEPLOY.md)。
