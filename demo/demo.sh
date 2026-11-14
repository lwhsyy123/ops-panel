#!/usr/bin/env bash
# 本地演示：造假 systemd/nginx 环境，在 127.0.0.1:9100 起面板
# 用法：bash demo/demo.sh    （密码 demo1234）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO="/tmp/ops-panel-demo"
PORT="${PORT:-9100}"
PASSWORD="demo1234"
OWNER="$(id -un):$(id -gn)"

rm -rf "$DEMO"
mkdir -p "$DEMO/fakebin" "$DEMO/sites-available" "$DEMO/sites-enabled" "$DEMO/certs" "$DEMO/logs" \
         "$DEMO/backups" "$DEMO/state/staging" "$DEMO/state/incoming" "$DEMO/systemd" \
         "$DEMO/www/site-a/en" "$DEMO/www/site-b" "$DEMO/packages"

cat > "$DEMO/fakebin/systemctl" <<'SH'
#!/bin/sh
case "$1" in
  show)
    case "$2" in
      demo-web.service) pid=2040312; mem=18874368 ;;
      shop-web.service) pid=1907340; mem=24379392 ;;
      nginx.service)     pid=2990641; mem=8388608 ;;
      *)                 pid=1;       mem=1048576 ;;
    esac
    echo "ActiveState=active"
    echo "SubState=running"
    echo "MainPID=$pid"
    echo "ActiveEnterTimestamp=Sat 2026-11-14 14:21:19 CST"
    echo "MemoryCurrent=$mem"
    echo "NRestarts=0"
    ;;
  is-active) echo active ;;
esac
exit 0
SH

cat > "$DEMO/fakebin/journalctl" <<'SH'
#!/bin/sh
unit=""
while [ $# -gt 0 ]; do
  case "$1" in -u) unit="$2"; shift 2 ;; *) shift ;; esac
done
echo "2026-11-14T14:21:19+0800 host systemd[1]: Started $unit."
case "$unit" in
  demo-web.service)
    echo "2026-11-14T14:21:19+0800 host python3[2040312]: [✔] Demo Dual-Site 1:1 Mirror Server running at http://127.0.0.1:8080/"
    echo "2026-11-14T14:21:19+0800 host python3[2040312]:   -> 🇨🇳 中文官网: http://127.0.0.1:8080/"
    echo "2026-11-14T14:21:19+0800 host python3[2040312]:   -> 🌐 英文官网: http://127.0.0.1:8080/en/"
    echo "2026-11-14T14:22:03+0800 host python3[2040312]: 200 GET / 12ms"
    echo "2026-11-14T14:22:07+0800 host python3[2040312]: 200 GET /en/ 15ms" ;;
  shop-web.service)
    echo "2026-11-13T21:02:32+0800 host python3[1907340]: 示例站点 B CMS listening on 127.0.0.1:8002"
    echo "2026-11-14T14:20:11+0800 host python3[1907340]: 200 GET / 8ms"
    echo "2026-11-14T14:20:44+0800 host python3[1907340]: 200 POST /api/inquiry 33ms" ;;
  nginx.service)
    echo "2026-11-14T14:21:19+0800 host systemd[1]: Reloading nginx." ;;
esac
SH

cat > "$DEMO/fakebin/nginx" <<'SH'
#!/bin/sh
echo "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok"
echo "nginx: configuration file /etc/nginx/nginx.conf test is successful"
exit 0
SH

cat > "$DEMO/fakebin/ufw" <<'SH'
#!/bin/sh
F=/tmp/ops-panel-demo/ufw.state
[ -f "$F" ] || printf 'inactive
' > "$F"
case "$1" in
  status)
    head -1 "$F" | sed 's/^/Status: /'
    n=1
    tail -n +2 "$F" | while IFS= read -r rule; do
      [ -n "$rule" ] || continue
      printf '[%3d] %s  ALLOW IN    Anywhere
' "$n" "$rule"
      n=$((n+1))
    done
    ;;
  allow|deny)
    action="$1"
    shift
    printf '%s
' "$action $*" >> "$F"
    echo "Rule added"
    ;;
  --force)
    action="$2"
    tmp="$F.tmp"
    if [ "$action" = "enable" ]; then
      { echo "active"; tail -n +2 "$F"; } > "$tmp"
    else
      { echo "inactive"; tail -n +2 "$F"; } > "$tmp"
    fi
    mv "$tmp" "$F"
    echo "ok"
    ;;
  delete)
    num="$3"
    tmp="$F.tmp"
    head -1 "$F" > "$tmp"
    n=1
    tail -n +2 "$F" | while IFS= read -r rule; do
      [ "$n" != "$num" ] && printf '%s
' "$rule" >> "$tmp"
      n=$((n+1))
    done
    mv "$tmp" "$F"
    echo "Deleted"
    ;;
esac
exit 0
SH

chmod +x "$DEMO/fakebin/"*

cat > "$DEMO/sites-available/site-a.conf" <<'CONF'
# 示例站点 A：双语站点共用一个后端
server {
    listen 80;
    server_name example.com www.example.com
                example.org www.example.org;

    location /media/ {
        alias /var/www/site-a/data/media/;
        expires 30d;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

server {
    listen 443 ssl;
    server_name www.example.com example.com
                www.example.org example.org;
    ssl_certificate     /tmp/ops-panel-demo/certs/example.com.pem;
    ssl_certificate_key /tmp/ops-panel-demo/certs/example.com.key;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
    }
}
CONF

cat > "$DEMO/sites-available/site-b.conf" <<'CONF'
# 示例站点 B
server {
    listen 80;
    server_name demo.example.net www.demo.example.net;

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

server {
    listen 443 ssl;
    server_name www.demo.example.net demo.example.net;
    ssl_certificate     /tmp/ops-panel-demo/certs/demo.example.net.pem;
    ssl_certificate_key /tmp/ops-panel-demo/certs/demo.example.net.key;

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host $host;
    }
}
CONF

ln -s "$DEMO/sites-available/site-a.conf" "$DEMO/sites-enabled/site-a.conf"
ln -s "$DEMO/sites-available/site-b.conf" "$DEMO/sites-enabled/site-b.conf"

echo '<h1>示例站点 A · 演示首页</h1>' > "$DEMO/www/site-a/index.html"
echo '<h1>Demo Site A · EN</h1>' > "$DEMO/www/site-a/en/index.html"
echo '<h1>示例站点 B · 演示首页</h1>' > "$DEMO/www/site-b/index.html"
echo '<h1>Demo Admin</h1>' > "$DEMO/www/site-b/admin.html"

openssl req -x509 -newkey rsa:2048 -nodes -days 90 \
  -keyout "$DEMO/certs/example.com.key" -out "$DEMO/certs/example.com.pem" \
  -subj "/CN=www.example.com" 2>/dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 12 \
  -keyout "$DEMO/certs/demo.example.net.key" -out "$DEMO/certs/demo.example.net.pem" \
  -subj "/CN=www.demo.example.net" 2>/dev/null

echo '2026/11/14 14:20:11 [error] 2990644#0: *18 connect() failed (111: Connection refused) while connecting to upstream' > "$DEMO/logs/nginx-error.log"
echo '104.21.9.66 - - [14/Nov/2026:14:22:03 +0800] "GET / HTTP/1.1" 200 18342 "-" "Mozilla/5.0"' > "$DEMO/logs/nginx-access.log"

HASH="$(python3 "$ROOT/panel.py" --hash-password "$PASSWORD")"

python3 - "$ROOT/config.example.json" "$DEMO" "$HASH" "$OWNER" <<'PY'
import json, sys
src, demo, pwd_hash, owner = sys.argv[1:5]
cfg = json.load(open(src, encoding="utf-8"))
cfg["password_hash"] = pwd_hash
cfg["deploy_token"] = "demo-token"
cfg["certs_dir"] = demo + "/certs"
cfg["backup_dir"] = demo + "/backups"
cfg["state_dir"] = demo + "/state"
cfg["audit_log"] = demo + "/logs/audit.log"
cfg["systemd_dir"] = demo + "/systemd"
cfg["default_owner"] = owner
cfg["metrics_demo"] = True
cfg["must_change_password"] = False
cfg["nginx"].update({
    "bin": demo + "/fakebin/nginx",
    "sites_enabled": demo + "/sites-enabled",
    "sites_available": demo + "/sites-available",
    "error_log": demo + "/logs/nginx-error.log",
    "access_log": demo + "/logs/nginx-access.log",
})
cfg["apps"] = [
    {"id": "site-a", "name": "示例站点 A", "unit": "demo-web.service",
     "path": demo + "/www/site-a", "owner": owner, "health_url": "", "port": 8080,
     "url": "https://www.example.com",
     "exclude": ["data", ".git", "._*", ".DS_Store"]},
    {"id": "site-b", "name": "示例站点 B", "unit": "shop-web.service",
     "path": demo + "/www/site-b", "owner": owner, "health_url": "", "port": 8002,
     "url": "https://www.demo.example.net",
     "exclude": ["data", ".git", "._*", ".DS_Store"]},
]
json.dump(cfg, open(demo + "/config.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

tar czf "$DEMO/packages/site-a-v1.tgz" -C "$DEMO/www/site-a" .
tar czf "$DEMO/packages/site-b-v1.tgz" -C "$DEMO/www/site-b" .

PATH="$DEMO/fakebin:$PATH" python3 - "$ROOT" "$DEMO" <<'PY'
import sys
root, demo = sys.argv[1:3]
sys.path.insert(0, root)
import panel
panel.CFG = panel.load_config(demo + "/config.json")
panel.ensure_dirs()
for app, pkg in [("site-a", demo + "/packages/site-a-v1.tgz"),
                 ("site-b", demo + "/packages/site-b-v1.tgz")]:
    print("seed", app, panel.deploy(app, pkg))
PY

echo
echo "=============================================="
echo " ops-panel 演示环境"
echo " 地址：http://127.0.0.1:$PORT"
echo " 密码：$PASSWORD"
echo " 假数据目录：$DEMO"
echo "=============================================="
echo

exec env PATH="$DEMO/fakebin:$PATH" python3 "$ROOT/panel.py" --config "$DEMO/config.json" --port "$PORT"
