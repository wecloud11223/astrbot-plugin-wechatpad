#!/usr/bin/env bash
# 安装/更新本仓库的 git hooks（pre-commit 敏感扫描 + post-commit 自动推送）。
#
#   bash scripts/install-git-hooks.sh
#
# 用符号链接而不是拷贝：仓库里的 hook 更新后无需重装。core.hooksPath 不改成
# scripts/，避免破坏其他人已有的 .git/hooks 内容。
set -euo pipefail
cd "$(dirname "$0")/.."
hooks_dir="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks_dir"
for h in pre-commit post-commit; do
    ln -sf ../../scripts/git-hooks/"$h" "$hooks_dir/$h"
    chmod +x "scripts/git-hooks/$h"
    echo "已安装 $h -> $hooks_dir/$h"
done
git config --local --get user.name  >/dev/null || git config --local user.name  "wecloud11223"
git config --local --get user.email >/dev/null || git config --local user.email "wecloud11223@users.noreply.github.com"
echo "✅ hooks 就绪；之后每次 commit 会自动 push origin <branch>"
