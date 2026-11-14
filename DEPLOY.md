# 部署到新 VPS

## 一、准备

- Ubuntu 20.04+ / Debian 11+，root 权限
- 已装 nginx，80/443 由宿主机 nginx 监听
- Python 3.8+（系统自带即可）
- 一个能 SSH 登录的账号

## 二、发布到 GitHub（推荐）

仓库里只有代码和演示数据，**没有真实域名、公司信息、Token**。推送前可以再确认：

```bash
grep -RInE "your-domain|company|token|password" . --exclude-dir=.git
```

1. 在 GitHub 新建空仓库（Private 也可以）
2. 本地：

```bash
cd ops-panel
git init
git add .
git commit -m "ops-panel: nginx + systemd + deploy console"
git branch -M main
git remote add origin git@github.com:lwhsyy123/ops-panel.git
git push -u origin main
```

3. 服务器上安装：

```bash
apt update && apt install -y git
OPS_PANEL_REPO=https://github.com/lwhsyy123/ops-panel.git bash bootstrap.sh
```

或者不用 bootstrap：

```bash
git clone --depth 1 https://github.com/lwhsyy123/ops-panel.git /opt/ops-panel-src
bash /opt/ops-panel-src/install.sh
```

## 三、不上 GitHub 的装法

本地打包后 scp：

```bash
tar czf ops-panel.tar.gz ops-panel
scp ops-panel.tar.gz root@<IP>:/root/
ssh root@<IP> 'cd /root && tar xzf ops-panel.tar.gz && bash ops-panel/install.sh'
```

## 四、首次配置

1. 建立隧道：`ssh -N -L 9100:127.0.0.1:9100 root@<IP>`
2. 浏览器打开 `http://127.0.0.1:9100`，密码在安装输出里（也在 `/root/.ops-panel-password`）
3. **服务器 → 发现 → 扫描本机**，把已有的 systemd 服务加入面板
4. **站点**页会自动列出 `sites-enabled` 里的站点，并关联服务与证书
5. **服务器 → 设置**里改上传上限（默认 300MB）
6. **告警**页填 RemindHub 地址和 Token，点「测试推送」
7. 需要锁源站时，用「放行 Cloudflare (80/443)」，再确认 22/tcp 已放行

## 五、访问方式（四选一）

### A. SSH 隧道（默认，最安全）

```bash
ssh -N -L 9100:127.0.0.1:9100 root@<IP>
```

浏览器打开 `http://127.0.0.1:9100`。适合偶尔管理，不需要开放任何端口。

### B. 域名 + nginx 反代 + HTTPS（推荐想直接打开网页的）

面板继续只监听 `127.0.0.1:9100`，用现有 nginx 反代出去：

```nginx
server {
    listen 443 ssl http2;
    server_name panel.example.com;

    ssl_certificate     /etc/ssl/cloudflare/panel.example.com.pem;
    ssl_certificate_key /etc/ssl/cloudflare/panel.example.com.key;

    location / {
        proxy_pass http://127.0.0.1:9100;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

没有证书就 `certbot --nginx -d panel.example.com`，然后 `ufw allow 443/tcp`。
访问 `https://panel.example.com`，记得先开 2FA。

### C. 直接 IP:9100（不推荐，但你坚持就按这个加固）

面板有改 nginx、启停服务、执行部署的权限，直接暴露等于把半个 root 挂公网。**必须同时满足**：

1. 先开 2FA（见下一节）
2. 改 `/etc/ops-panel/config.json`：`"bind": "0.0.0.0"`
3. 配 TLS（二选一）：
   - 面板自带：`"tls_cert"` / `"tls_key"` 指向证书和私钥，重启后走 `https://IP:9100`（自签证书浏览器会警告）
   - 前面套 nginx 反代（同方案 B），面板仍留在 127.0.0.1
4. 防火墙只放行你的固定 IP：

```bash
ufw allow from 你的家用IP to any port 9100 proto tcp
ufw allow from 你的公司IP to any port 9100 proto tcp
ufw deny 9100/tcp
systemctl restart ops-panel
```

5. 没有 2FA、没有 HTTPS、没有 IP 白名单，这三样缺一个都别开。

### D. Cloudflare Tunnel + Access（不开端口，手机也能访问）

适合“要域名、要 HTTPS、不想暴露源站”的场景。

```bash
# 1. 安装 cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
dpkg -i /tmp/cf.deb

# 2. 登录并建隧道
cloudflared tunnel login
cloudflared tunnel create ops-panel
```

`/root/.cloudflared/config.yml`：

```yaml
tunnel: <tunnel-uuid>
credentials-file: /root/.cloudflared/<tunnel-uuid>.json
ingress:
  - hostname: panel.example.com
    service: http://127.0.0.1:9100
  - service: http_status:404
```

```bash
cloudflared tunnel route dns ops-panel panel.example.com
cloudflared service install
systemctl enable --now cloudflared
```

然后在 Cloudflare Zero Trust → Access → Applications 新建 Self-hosted 应用：
- 域名填 `panel.example.com`
- 策略：Allow → Emails → 填你自己的邮箱
- 认证方式选 One-time PIN

之后访问 `https://panel.example.com`，先收邮箱验证码，再输面板密码 + 动态码。源站不需要开放 9100。

## 六、两步验证（2FA）

服务器 → 安全 / 两步验证 → 启用：

1. 页面会给出一个 TOTP 密钥和 `otpauth://` 链接
2. 在 Google Authenticator / 1Password / 微软验证器里手动添加该密钥
3. 输入手机上的 6 位码确认
4. **保存弹出的 8 个恢复码**，每个只能用一次

开启后登录需要「密码 + 动态码」。关闭需要密码 + 动态码（或恢复码）。

## 七、时区

- `install.sh` 会自动执行 `timedatectl set-timezone Asia/Shanghai`
- 面板自身的时间戳统一用北京时间（不依赖系统时区）
- systemd 单元带 `Environment=TZ=Asia/Shanghai`
- nginx 日志跟随系统时区，设置后重启一次 nginx：`systemctl restart nginx`
- 检查：`timedatectl` 应显示 `Asia/Shanghai (CST, +0800)`

## 八、更新版本

```bash
cd /opt/ops-panel-src && git pull
bash install.sh          # 会覆盖代码，保留 /etc/ops-panel/config.json
systemctl restart ops-panel
```

## 九、备份与卸载

- 配置：`/etc/ops-panel/config.json`（含密码哈希、TOTP 密钥、Token，权限 600）
- 状态：`/var/lib/ops-panel`、`/var/backups/ops-panel`、`/var/log/ops-panel`
- 卸载：`systemctl disable --now ops-panel && rm -rf /opt/ops-panel /etc/ops-panel`

## 十、安全注意

- 能不开公网就不开；要开就走方案 B 或 D，并保持 2FA 开启
- 面板以 root 运行（要操作 systemd 和 nginx）
- 部署 Token 泄露时，改 `config.json` 的 `deploy_token` 并重启
- 恢复码只显示一次，丢了就只能用密码 + 动态码；都丢了需要改配置重置

