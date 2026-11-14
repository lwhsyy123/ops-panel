#!/usr/bin/env bash
# ops-panel 安装脚本（Ubuntu / Debian，root 运行）
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请用 root 运行：sudo bash install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/ops-panel"
CONF_DIR="/etc/ops-panel"
CONF="${CONF_DIR}/config.json"
PASSWORD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --password) PASSWORD="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if command -v timedatectl >/dev/null 2>&1; then
  timedatectl set-timezone Asia/Shanghai >/dev/null 2>&1 || true
  echo "时区：$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
fi

command -v python3 >/dev/null 2>&1 || { echo "缺少 python3" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
  || { echo "需要 Python 3.8 及以上" >&2; exit 1; }

mkdir -p "${APP_DIR}" "${CONF_DIR}"
install -m 0755 "${SRC_DIR}/panel.py" "${APP_DIR}/panel.py"
rm -rf "${APP_DIR}/web"
cp -r "${SRC_DIR}/web" "${APP_DIR}/web"

FIRST_RUN=0
if [[ ! -f "${CONF}" ]]; then
  FIRST_RUN=1
  if [[ -z "${PASSWORD}" ]]; then
    PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  fi
  HASH="$(python3 "${APP_DIR}/panel.py" --hash-password "${PASSWORD}")"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  python3 - "${SRC_DIR}/config.example.json" "${CONF}" "${HASH}" "${TOKEN}" <<'PY'
import json, sys
src, dst, pwd_hash, token = sys.argv[1:5]
with open(src, "r", encoding="utf-8") as f:
    cfg = json.load(f)
cfg["password_hash"] = pwd_hash
cfg["deploy_token"] = token
with open(dst, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PY
  chmod 600 "${CONF}"
  printf '%s\n' "${PASSWORD}" > /root/.ops-panel-password
  chmod 600 /root/.ops-panel-password
fi

cat > /etc/systemd/system/ops-panel.service <<'UNIT'
[Unit]
Description=ops-panel (nginx + systemd + deploy console)
After=network.target nginx.service
Wants=nginx.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ops-panel
ExecStart=/usr/bin/python3 /opt/ops-panel/panel.py --config /etc/ops-panel/config.json
Environment=TZ=Asia/Shanghai
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now ops-panel
sleep 1

if systemctl is-active --quiet ops-panel; then
  echo
  echo "==============================================================="
  echo " ops-panel 已启动：http://127.0.0.1:9100 （仅本机）"
  echo "==============================================================="
  if [[ ${FIRST_RUN} -eq 1 ]]; then
    echo " 登录密码：${PASSWORD}   （已存 /root/.ops-panel-password）"
    TOKEN_SHOW="$(python3 -c 'import json;print(json.load(open("/etc/ops-panel/config.json"))["deploy_token"])')"
    echo " 部署令牌：${TOKEN_SHOW}   （填进 deploy.sh 的 OPS_TOKEN）"
    echo
    echo " 从你的电脑建立隧道："
    echo "   ssh -N -L 9100:127.0.0.1:9100 root@<这台机器的IP>"
    echo " 然后浏览器打开：http://127.0.0.1:9100"
  else
    echo " 已存在配置，未重置密码。"
  fi
  echo "==============================================================="
else
  echo "启动失败，查看日志：journalctl -u ops-panel -n 50 --no-pager" >&2
  exit 1
fi
