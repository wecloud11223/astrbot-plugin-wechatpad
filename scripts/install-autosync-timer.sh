#!/usr/bin/env bash
# 安装 systemd timer：每 5 分钟把配置的仓库工作区改动自动提交 + 推送。
#
#   sudo bash scripts/install-autosync-timer.sh                # 只装当前仓库
#   sudo bash scripts/install-autosync-timer.sh /path/a /path/b # 装多个仓库
#
# 为什么需要这一层：git hook 只在"有人 commit"时触发；agent 可能改了文件却忘了
# commit。timer 是最后一道兜底，保证"改了就算同步"，无需依赖 agent 自觉。
# 卸载：systemctl disable --now wpp-git-autosync.timer && rm /etc/systemd/system/wpp-git-autosync.*
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"

repos=("$@")
if (( ${#repos[@]} == 0 )); then
    repos=("$here")
    # 同一父目录下常见的姊妹仓库一并纳入（两套 WPP 插件都在 ~/dev 下）
    for sib in "$(dirname "$here")/astrbot-plugin-wpp-cloud" "$(dirname "$here")/wpp-hermes-cloud"; do
        [[ -d "$sib/.git" && "$sib" != "$here" ]] && repos+=("$sib")
    done
fi

wrapper=/usr/local/bin/wpp-git-autosync
{
    echo '#!/usr/bin/env bash'
    echo '# 由 scripts/install-autosync-timer.sh 生成：逐个仓库跑 git-autosync.sh。'
    echo 'set -u'
    for r in "${repos[@]}"; do
        printf 'repo=%q\n' "$r"
        cat <<'INNER'
if [[ -d "$repo/.git" ]]; then
    bash "$repo/scripts/git-autosync.sh" || echo "autosync 失败：$repo（见上）" >&2
fi
INNER
    done
} > "$wrapper"
chmod 755 "$wrapper"

install -m 0644 "$here/scripts/systemd/wpp-git-autosync.service" /etc/systemd/system/
install -m 0644 "$here/scripts/systemd/wpp-git-autosync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wpp-git-autosync.timer >/dev/null
echo "✅ timer 已启用：$(systemctl is-active wpp-git-autosync.timer)"
echo "   监控仓库："
for r in "${repos[@]}"; do echo "   - $r"; done
echo "   手动触发一次：systemctl start wpp-git-autosync.service"
