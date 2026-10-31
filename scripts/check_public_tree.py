#!/usr/bin/env python3
"""公开树敏感信息扫描（AstrBot 插件仓）。

扫描 Git 跟踪/待提交的文件，命中即以非零码退出：

* 真实 wxid / 微信号 / 群号形态；
* 网关令牌（``wxa_...``）/ 云密钥 / 私钥块；
* 厂商域名、内网地址、本机绝对路径。

本机专属的真实标识放在 ``public_tree_denylist.txt``（一行一个正则，**不入库**），
公开仓里没有它，CI 与外部贡献者只跑通用形态。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SELF = Path(__file__).resolve()
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz"}

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("真实 wxid 形态", re.compile(r"wxid_(?!test|self|dummy|sample|example|replace|your)[a-z0-9]{11,}")),
    ("网关令牌", re.compile(r"\bwxa_[A-Za-z0-9]{24,}\b")),
    ("云密钥", re.compile(r"\b(?:sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36})\b")),
    ("私钥", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("厂商域名", re.compile(r"\b(?:wx|oss|adm)\.juhe\.chat\b|(?<!cloud\.)\bjuhe\.chat\b")),
    ("内网主机", re.compile(r"\b(?:10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+)\b")),
    ("本机绝对路径", re.compile(r"/root/")),
)


def _load_denylist() -> tuple[tuple[str, re.Pattern[str]], ...]:
    path = Path(os.environ.get("WPP_PUBLIC_TREE_DENYLIST")
                or Path(__file__).resolve().parents[1] / "public_tree_denylist.txt")
    if not path.exists():
        return ()
    rules: list[tuple[str, re.Pattern[str]]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append((f"本机黑名单:{lineno}", re.compile(line)))
        except re.error as exc:
            print(f"public_tree_denylist.txt:{lineno} 正则无效（{exc}），已跳过", file=sys.stderr)
    return tuple(rules)


PATTERNS = PATTERNS + _load_denylist()


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"无法读取 git 文件列表：{exc}", file=sys.stderr)
        return []
    return [Path(p) for p in out.decode("utf-8", "replace").split("\0") if p]


def main() -> int:
    files = _tracked_files()
    if not files:
        print("没有可扫描的 Git 跟踪文件", file=sys.stderr)
        return 2
    hits: list[str] = []
    for path in files:
        if path.resolve() == SELF or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    hits.append(f"{path}:{lineno}: [{label}] {line.strip()[:200]}")
    if hits:
        print(f"发现 {len(hits)} 处疑似敏感信息：\n", file=sys.stderr)
        for hit in hits:
            print(hit, file=sys.stderr)
        return 1
    print(f"公开树扫描通过：{len(files)} 个文件，未发现敏感信息")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
