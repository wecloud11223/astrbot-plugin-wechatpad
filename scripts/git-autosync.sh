#!/usr/bin/env bash
# 把工作区改动自动提交并推送到 GitHub —— 给"任何 agent 改了就要同步"兜底。
#
#   bash scripts/git-autosync.sh            # 有改动才提交+推送，没有就静默退出
#   bash scripts/git-autosync.sh --status   # 只报告状态
#
# 幂等：无改动 ⇒ 不产生空提交；并发用 flock 串行化（hook 与 systemd timer 可能同时跑）。
# 推送失败不丢改动：提交留在本地，下一轮自动重试。
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--status" ]]; then
    git status --short --branch
    exit 0
fi

exec 9>"${TMPDIR:-/tmp}/wpp-git-autosync.$(basename "$PWD").lock"
flock -n 9 || { echo "另一轮 autosync 正在跑，跳过"; exit 0; }

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" == "HEAD" ]]; then
    echo "❌ 当前是 detached HEAD，拒绝自动提交" >&2
    exit 1
fi

git add -A
if git diff --cached --quiet; then
    exit 0
fi

bash scripts/git-secret-scan.sh --staged
git commit -q -m "chore(autosync): $(date '+%F %T') 自动同步工作区改动"
echo "✅ 已提交：$(git log -1 --oneline)"

if git push -q origin "$branch"; then
    echo "✅ 已推送 origin/$branch"
else
    echo "⚠️  推送失败（网络/密钥/权限？）——提交已留在本地，下一轮自动重试" >&2
    exit 1
fi
