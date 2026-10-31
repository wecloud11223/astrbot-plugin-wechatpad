#!/usr/bin/env bash
# 提交前敏感信息扫描：把「会泄漏什么」挡在 commit 之前。
#
#   bash scripts/git-secret-scan.sh            # 扫已跟踪 + 待提交 + 未跟踪(受 .gitignore 约束)
#   bash scripts/git-secret-scan.sh --staged   # 只扫暂存区（pre-commit hook 用）
#
# 命中即以 1 退出。规则来源：
#   * scripts/check_public_tree.py 的通用形态 + 本机专属黑名单（public_tree_denylist.txt，不入库）
#   * 本脚本兜底：私钥块、常见云密钥前缀
set -euo pipefail
cd "$(dirname "$0")/.."

# 1) 仓库自带的结构化扫描（公开树规则；无 git 时它自己会返回 2，这里不当失败）
if [[ -f scripts/check_public_tree.py ]] && command -v python3 >/dev/null 2>&1; then
    set +e
    python3 scripts/check_public_tree.py
    rc=$?
    set -e
    if [[ $rc -eq 1 ]]; then
        echo "❌ 敏感信息扫描未通过（见上），已拦截提交" >&2
        exit 1
    fi
fi

# 2) 兜底 grep：只看文本差异，跳过二进制
if [[ "${1:-}" == "--staged" ]]; then
    mapfile -t files < <(git diff --cached --name-only --diff-filter=ACM)
else
    mapfile -t files < <(git ls-files -z --cached --others --exclude-standard | tr '\0' '\n')
fi
(( ${#files[@]} == 0 )) && { echo "敏感信息扫描：没有文件需要检查"; exit 0; }

patterns='BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{36}|-----BEGIN OPENSSH'
hits=0
self_rel="scripts/git-secret-scan.sh"
for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    # 扫描器自身的规则字面量会"命中自己"，跳过
    [[ "$f" == "$self_rel" ]] && continue
    if grep -Iq . "$f" 2>/dev/null && grep -nE "$patterns" "$f" >/dev/null 2>&1; then
        echo "❌ $f 命中密钥形态：" >&2
        grep -nE "$patterns" "$f" | head -5 >&2
        hits=1
    fi
done
(( hits == 1 )) && { echo "❌ 敏感信息扫描未通过：先移除/改成占位符再提交" >&2; exit 1; }
echo "✅ 敏感信息扫描通过（${#files[@]} 个文件）"
