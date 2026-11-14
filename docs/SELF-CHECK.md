# 自检记录

日期：2026-11-14
环境：macOS + Python 3.10 + 假 systemd/nginx/ufw（演示模式），真实 systemd 未参与。

## 已通过

| 项目 | 方式 | 结果 |
|---|---|---|
| Python 语法 | `python3 -m py_compile panel.py` | 通过 |
| 前端语法 | JavaScriptCore 解析 app.js | 通过 |
| Shell 语法 | `bash -n install.sh deploy.sh bootstrap.sh demo/demo.sh` | 通过 |
| 登录 / 会话 | curl 登录、未登录跳转 | 通过 |
| 2FA 启用 | 生成 TOTP、6 位码确认、8 个恢复码 | 通过 |
| 2FA 二维码 | 本地 qrcodejs 渲染 otpauth URI | 通过 |
| 2FA 登录 | 无码 401、有码 200 | 通过 |
| 恢复码 | 一次性使用、剩余数量递减、复用 401 | 通过 |
| 关闭 2FA | 密码 + 动态码 | 通过 |
| 修改密码 | 旧密码失效、新密码可登录、会话清空 | 通过 |
| 首次强制改密 | 登录后跳转、接口拦截、改完恢复 | 通过 |
| 重置部署令牌 | 旧令牌 401、新令牌可部署 | 通过 |
| HTTPS 直连 | 自签证书 + tls_cert/tls_key | 通过 |
| 北京时间 | 面板时间戳 +08、与系统一致 | 通过 |
| 概览指标 | CPU/内存/负载/磁盘/网络/运行时长 | 通过（演示数据） |
| 站点列表 | 域名、服务、证书、部署关联 | 通过 |
| 站点预览 | /api/sites/preview 生成 80+443 | 通过 |
| 新建站点 | 上传 zip、自动解压、路径、权限 | 通过 |
| 新建服务 | 端口无服务时自动写 systemd 单元 | 通过 |
| 端口反查 | 命令行无端口时用 ss + MainPID 识别 | 通过 |
| 更新已纳管服务 | 同一 unit 更新端口/健康检查 | 通过 |
| 级联删除 | 配置 + 单元 + 目录，二次确认 | 通过 |
| 删除护栏 | 共用端口/路径拒绝删除 | 通过 |
| 证书上传 | 配对校验、过期天数 | 通过 |
| 防火墙 | 允许/拒绝规则、删规则、启停 | 通过（假 ufw） |
| SSH 自动放行 | 检测 sshd 端口、缺规则提示与一键放行 | 通过（假 sshd） |
| Cloudflare 预设 | 15 网段 80/443 | 通过（假 ufw） |
| 设置 | 上传上限修改并持久化 | 通过 |
| 部署 | 上传包、备份、rsync、重启、快照 | 通过（假 systemd） |
| 回滚 | 快照还原 | 通过 |
| 告警推送 | mock RemindHub 收到 Bearer + 报文 | 通过 |
| 负载告警门槛 | load 高 + 队列/I-O 压力才触发 | 通过 |
| fail2ban 状态 | 解析 jail、封禁数、IP 列表 | 通过 |
| 解封 | 校验 IP 并调用 fail2ban-client | 通过 |
| 日报 | 生成并投递 | 通过 |
| 故障/恢复 | 连续失败触发、恢复通知 | 通过 |
| 心跳 | /api/heartbeat | 通过 |

## 未验证

| 项目 | 原因 | 上线前动作 |
|---|---|---|
| 真实 systemd 启停 | 本机无 systemd | 先在 VPS 上点一次「重启」，看 journal |
| 真实 nginx -t / reload | 本机无 nginx | 先点「仅校验」，再保存一个小改动 |
| 真实 ufw | 本机无 ufw | 先 `ufw status`，启用前确认 22/tcp 已放行 |
| 直连 0.0.0.0 公网访问 | 需真实公网环境 | 开 2FA + 防火墙白名单后再改 bind |
| Cloudflare Tunnel | 需 CF 账号与域名 | 按 DEPLOY.md 第五节 D 配置 |
| install.sh | 需要 root + systemd | 在 VPS 上首次安装时观察输出 |
| RHEL 系兼容 | 未测 | 需 firewalld 支持，当前只支持 ufw |

## 上线前 checklist

1. VPS 快照 / 备份 `/etc/nginx`
2. 安装后先看概览，确认服务与证书数据真实
3. **先启用 2FA 并保存恢复码**
4. 站点页点「仅校验」
5. 改一个小配置保存，确认 reload 正常
6. 发一次部署，确认自动备份与回滚可用
7. 配好 RemindHub，点「测试推送」
8. 防火墙启用前保留另一个 SSH 会话
9. 决定访问方式：SSH 隧道 / nginx 反代 / 直连 / Cloudflare Tunnel
