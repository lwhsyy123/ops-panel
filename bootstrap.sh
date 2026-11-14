#!/usr/bin/env bash
# 从 Git 仓库克隆并安装 ops-panel
# 用法：OPS_PANEL_REPO=https://github.com/you/ops-panel.git bash bootstrap.sh
set -euo pipefail

REPO="${OPS_PANEL_REPO:-https://github.com/your-name/ops-panel.git}"
command -v git >/dev/null 2>&1 || { echo "需要先安装 git" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

echo "→ 克隆 ${REPO}"
git clone --depth 1 "${REPO}" "${TMP}/ops-panel"
bash "${TMP}/ops-panel/install.sh" "$@"
