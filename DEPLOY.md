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

## 五、更新版本

```bash
cd /opt/ops-panel-src && git pull
bash install.sh          # 会覆盖代码，保留 /etc/ops-panel/config.json
systemctl restart ops-panel
```

## 六、备份与卸载

- 配置：`/etc/ops-panel/config.json`（含密码哈希与 Token，权限 600）
- 状态：`/var/lib/ops-panel`、`/var/backups/ops-panel`、`/var/log/ops-panel`
- 卸载：`systemctl disable --now ops-panel && rm -rf /opt/ops-panel /etc/ops-panel`

## 七、安全注意

- 面板只监听 `127.0.0.1`，不要改成 0.0.0.0 直接暴露公网
- SSH 隧道账号建议用密钥登录
- 面板以 root 运行（要操作 systemd 和 nginx），所以隧道是唯一入口
- 推送 Token 泄露时，改 `config.json` 的 `deploy_token` 并重启
