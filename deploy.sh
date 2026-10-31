#!/usr/bin/env bash
# astrbot_plugin_wpp_cloud 部署脚本: docker cp → 依赖 → 重启 → 日志检查
set -euo pipefail

PLUGIN_SRC="$(cd "$(dirname "$0")" && pwd)"
CONTAINER="astrbot"
PLUGIN_DIR="/AstrBot/data/plugins/astrbot_plugin_wpp_cloud"

echo "==> [1/4] 拷贝插件到容器 $CONTAINER"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -C "$PLUGIN_SRC" \
    --exclude=.git --exclude=.github --exclude=tests \
    --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=.pytest_cache --exclude=.ruff_cache --exclude=data \
    -cf - . | tar -C "$STAGE" -xf -
docker exec "$CONTAINER" sh -c "rm -rf '$PLUGIN_DIR' && mkdir -p '$PLUGIN_DIR'"
docker cp "$STAGE"/. "$CONTAINER":"$PLUGIN_DIR"/

echo "==> [2/4] 检查依赖 aiohttp"
docker exec "$CONTAINER" sh -c 'python3 -c "import aiohttp" >/dev/null 2>&1 || pip install aiohttp -i https://mirrors.aliyun.com/pypi/simple/'

echo "==> [3/4] 重启 AstrBot"
docker restart "$CONTAINER"

echo "==> [4/4] 等待启动并看日志"
sleep 6
docker logs --tail 40 "$CONTAINER" 2>&1 | grep -iE "wpp|plugin|adapter|error" || true

echo ""
echo "部署完成。Dashboard → 平台适配器 → 添加「个人微信(Pad协议)」，"
echo "填写 wpp_gateway_url / wpp_gateway_token，并把启动日志给出的回调地址填到 WPP 控制台。"
