#!/usr/bin/env bash
# 在本机（Mac/Linux）运行：打包当前项目并通过 SSH 交给服务器上的 ops-panel 部署
#
#   OPS_HOST=root@1.2.3.4 OPS_TOKEN=xxx ./deploy.sh site-a /path/to/site-aweb
#
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "用法: OPS_HOST=root@ip OPS_TOKEN=xxx $0 <site-a|site-b> [项目目录]" >&2
  exit 1
fi

APP="$1"
PROJECT="${2:-$(pwd)}"
: "${OPS_HOST:?请设置 OPS_HOST，例如 root@1.2.3.4}"
: "${OPS_TOKEN:?请设置 OPS_TOKEN（服务器上 /etc/ops-panel/config.json 里的 deploy_token）}"

[[ -d "${PROJECT}" ]] || { echo "目录不存在: ${PROJECT}" >&2; exit 1; }

TMP="$(mktemp -t "ops-${APP}.XXXXXX.tgz")"
trap 'rm -f "${TMP}"' EXIT

echo "→ 打包 ${PROJECT}"
tar \
  --exclude='./data' \
  --exclude='./.git' \
  --exclude='./.DS_Store' \
  --exclude='./._*' \
  --exclude='./__pycache__' \
  --exclude='./.bak_*' \
  --exclude='*.tar.gz' \
  --exclude='*.tgz' \
  -czf "${TMP}" -C "${PROJECT}" .
echo "  包大小：$(du -h "${TMP}" | cut -f1)"

echo "→ 上传并部署到 ${OPS_HOST}"
RESP="$(ssh "${OPS_HOST}" \
  "curl -sS --max-time 600 -X POST \
     -H 'Content-Type: application/octet-stream' \
     -H 'X-Deploy-Token: ${OPS_TOKEN}' \
     --data-binary @- \
     'http://127.0.0.1:9100/api/deploy/${APP}'" < "${TMP}")"

echo "${RESP}"
if ! printf '%s' "${RESP}" | grep -q '"ok": *true'; then
  echo "部署失败，见上面的响应" >&2
  exit 1
fi
echo "✓ 部署完成"
