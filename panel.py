#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ops-panel — 一台 VPS 一个面板：Nginx 站点/证书 + systemd 服务 + 部署回滚

设计约束：
  * 只依赖 Python 3 标准库，不需要 pip / venv / node
  * 只监听 127.0.0.1（通过 SSH 隧道访问），不做公网暴露
  * 所有会改动系统的操作：先备份，再校验，失败自动恢复
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "web")

CFG = {}
CFG_PATH = ""
CFG_LOCK = threading.Lock()
MONITOR = None
SESSIONS = {}
SESS_LOCK = threading.Lock()
FAILS = {}
FAILS_LOCK = threading.Lock()
AUDIT_LOCK = threading.Lock()

SESSION_TTL = 8 * 3600


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    BEIJING_TZ = None


def now_dt():
    """统一使用北京时间，不受服务器时区影响。"""
    if BEIJING_TZ is not None:
        return datetime.now(BEIJING_TZ)
    return datetime.now(timezone(timedelta(hours=8)))


def now_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def run(cmd, timeout=120, input_text=None):
    """执行命令，返回 (rc, stdout, stderr)，不抛异常。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "命令不存在: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时: %s" % " ".join(cmd)


def audit(action, detail, ok=True):
    path = CFG.get("audit_log", "/var/log/ops-panel/audit.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": now_str(), "action": action, "detail": detail, "ok": ok,
                }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("bind", "127.0.0.1")
    cfg.setdefault("port", 9100)
    cfg.setdefault("max_upload_mb", 300)
    cfg.setdefault("totp_enabled", False)
    cfg.setdefault("totp_secret", "")
    cfg.setdefault("totp_recovery", [])
    cfg.setdefault("tls_cert", "")
    cfg.setdefault("tls_key", "")
    cfg.setdefault("certs_dir", "/etc/ssl/cloudflare")
    cfg.setdefault("backup_dir", "/var/backups/ops-panel")
    cfg.setdefault("state_dir", "/var/lib/ops-panel")
    cfg.setdefault("audit_log", "/var/log/ops-panel/audit.log")
    cfg.setdefault("nginx", {})
    cfg["nginx"].setdefault("bin", "nginx")
    cfg["nginx"].setdefault("sites_enabled", "/etc/nginx/sites-enabled")
    cfg["nginx"].setdefault("sites_available", "/etc/nginx/sites-available")
    cfg["nginx"].setdefault("error_log", "/var/log/nginx/error.log")
    cfg["nginx"].setdefault("access_log", "/var/log/nginx/access.log")
    cfg.setdefault("apps", [])
    for app in cfg["apps"]:
        app.setdefault("exclude", ["data"])
        app.setdefault("owner", "www-data:www-data")
        app.setdefault("health_url", "")
    cfg.setdefault("notify", {})
    n = cfg["notify"]
    n.setdefault("enabled", False)
    n.setdefault("base_url", "")
    n.setdefault("token", "")
    n.setdefault("channels", [])
    n.setdefault("daily_at", "09:00")
    n.setdefault("heartbeat_name", "")
    th = n.setdefault("thresholds", {})
    th.setdefault("disk_percent", 85)
    th.setdefault("cert_days", 14)
    th.setdefault("http_failures", 2)
    th.setdefault("check_interval", 60)
    th.setdefault("response_ms", 5000)
    th.setdefault("load_ratio", 2)
    th.setdefault("mem_percent", 90)
    return cfg


def save_config():
    with CFG_LOCK:
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)


def ensure_dirs():
    for d in (CFG["backup_dir"], CFG["state_dir"],
              os.path.join(CFG["backup_dir"], "nginx"),
              os.path.join(CFG["backup_dir"], "releases"),
              os.path.join(CFG["state_dir"], "incoming"),
              os.path.join(CFG["state_dir"], "staging")):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------
# 密码与会话
# --------------------------------------------------------------------------
def hash_password(pw, iterations=200000, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt.hex(), dk.hex())


def verify_password(pw, stored):
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def gen_totp_secret():
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode().rstrip("=")


def totp_code(secret, offset=0, step=30):
    pad = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + pad)
    counter = int(time.time() // step) + offset
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    pos = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[pos:pos + 4])[0] & 0x7FFFFFFF
    return "%06d" % (value % 1000000)


def verify_totp(secret, code):
    code = str(code or "").replace(" ", "").strip()
    if not secret or not code.isdigit() or len(code) != 6:
        return False
    return any(hmac.compare_digest(totp_code(secret, offset=o), code) for o in (-1, 0, 1))


def otpauth_uri(secret, account="admin", issuer="ops-panel"):
    from urllib.parse import quote
    label = quote("%s:%s" % (issuer, account))
    return ("otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=6&period=30"
            % (label, secret, quote(issuer)))


def recovery_hashes(codes):
    return [hashlib.sha256(c.strip().upper().encode()).hexdigest() for c in codes]


def consume_recovery(code):
    code = str(code or "").strip().upper()
    if not code:
        return False
    h = hashlib.sha256(code.encode()).hexdigest()
    items = CFG.get("totp_recovery") or []
    if h in items:
        items.remove(h)
        CFG["totp_recovery"] = items
        save_config()
        return True
    return False


def new_session():
    token = secrets.token_urlsafe(32)
    with SESS_LOCK:
        SESSIONS[token] = time.time() + SESSION_TTL
    return token


def session_ok(token):
    if not token:
        return False
    with SESS_LOCK:
        exp = SESSIONS.get(token)
        if not exp:
            return False
        if exp < time.time():
            SESSIONS.pop(token, None)
            return False
        return True


def drop_session(token):
    with SESS_LOCK:
        SESSIONS.pop(token, None)


# --------------------------------------------------------------------------
# systemd / nginx / 证书
# --------------------------------------------------------------------------
def service_status(unit):
    rc, out, err = run(["systemctl", "show", unit,
                        "-p", "ActiveState", "-p", "SubState",
                        "-p", "MainPID", "-p", "ActiveEnterTimestamp",
                        "-p", "MemoryCurrent", "-p", "NRestarts"], timeout=15)
    data = {"unit": unit, "ok": rc == 0}
    if rc != 0:
        data["error"] = err or out
        return data
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    mem = data.get("MemoryCurrent", "")
    data["memory_mb"] = round(int(mem) / 1048576, 1) if mem.isdigit() else None
    data["active"] = data.get("ActiveState") == "active"
    return data


def journal(unit, lines=120):
    rc, out, err = run(["journalctl", "-u", unit, "-n", str(lines),
                        "--no-pager", "--output=short-iso"], timeout=20)
    return out if rc == 0 else (err or out)


def nginx_status():
    rc, out, _ = run(["systemctl", "is-active", "nginx"], timeout=10)
    return out == "active"


def nginx_test():
    rc, out, err = run([CFG["nginx"]["bin"], "-t"], timeout=30)
    return rc == 0, (out + "\n" + err).strip()


def parse_site(content):
    domains = []
    for m in re.finditer(r"server_name\s+([^;]+);", content):
        domains.extend(m.group(1).split())
    upstreams = re.findall(r"proxy_pass\s+https?://([^;/]+)", content)
    roots = re.findall(r"(?<![\w])root\s+([^;]+);", content)
    certs = re.findall(r"ssl_certificate\s+([^;\s]+);", content)
    return {
        "domains": sorted({x for x in domains if x and x != "_"}),
        "upstreams": sorted(set(upstreams)),
        "roots": sorted({x.strip() for x in roots}),
        "certs": sorted(set(certs)),
    }


def list_sites():
    enabled_dir = CFG["nginx"]["sites_enabled"]
    avail_dir = CFG["nginx"]["sites_available"]
    names = set()
    for d in (enabled_dir, avail_dir):
        if os.path.isdir(d):
            names |= {x for x in os.listdir(d) if x.endswith(".conf")}
    result = []
    for name in sorted(names):
        link = os.path.join(enabled_dir, name)
        src = os.path.join(avail_dir, name)
        enabled = os.path.lexists(link)
        path = os.path.realpath(link) if enabled else src
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                meta = parse_site(f.read())
            size = os.path.getsize(path)
        except OSError:
            meta, size = {"domains": [], "upstreams": [], "roots": []}, 0
        result.append(dict(name=name, path=path, size=size, enabled=enabled, **meta))
    return result


def site_path(name):
    if not re.match(r"^[A-Za-z0-9._-]+\.conf$", name):
        raise ValueError("非法站点名")
    for d in (CFG["nginx"]["sites_enabled"], CFG["nginx"]["sites_available"]):
        p = os.path.join(d, name)
        if os.path.lexists(p):
            return os.path.realpath(p) if os.path.islink(p) else p
    raise ValueError("站点不存在: %s" % name)


def proxy_template(name, domains, port):
    return (
        "# " + name + "\n"
        "server {\n"
        "    listen 80;\n"
        "    server_name " + " ".join(domains) + ";\n\n"
        "    location / {\n"
        "        proxy_pass http://127.0.0.1:" + str(port) + ";\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Connection \"\";\n"
        "    }\n"
        "}\n"
    )


def static_template(name, domains, root):
    return (
        "# " + name + "\n"
        "server {\n"
        "    listen 80;\n"
        "    server_name " + " ".join(domains) + ";\n\n"
        "    root " + root + ";\n"
        "    index index.html;\n\n"
        "    location / {\n"
        "        try_files $uri $uri/ =404;\n"
        "    }\n"
        "}\n"
    )


def _server_block(domains, body, ssl=False, cert=None):
    lines = ["server {", "    listen 443 ssl;" if ssl else "    listen 80;"]
    if ssl and cert:
        lines += [
            "    ssl_certificate     " + cert["pem"] + ";",
            "    ssl_certificate_key " + cert["key"] + ";",
        ]
    lines.append("    server_name " + " ".join(domains) + ";")
    lines.append("")
    lines += ["    " + x for x in body]
    lines.append("}")
    return "\n".join(lines)


def _proxy_body(port):
    return [
        "location / {",
        "    proxy_pass http://127.0.0.1:" + str(port) + ";",
        "    proxy_set_header Host $host;",
        "    proxy_set_header X-Real-IP $remote_addr;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "    proxy_set_header X-Forwarded-Proto $scheme;",
        "    proxy_http_version 1.1;",
        "    proxy_set_header Connection \"\";",
        "}",
    ]


def _static_body(root):
    return [
        "root " + root + ";",
        "index index.html;",
        "location / {",
        "    try_files $uri $uri/ =404;",
        "}",
    ]


def build_site_conf(name, domains, mode, target, cert=None):
    body = _proxy_body(int(target)) if mode == "proxy" else _static_body(target)
    parts = ["# " + name, _server_block(domains, body)]
    if cert:
        parts.append(_server_block(domains, body, ssl=True, cert=cert))
    return "\n\n".join(parts) + "\n"


def resolve_cert(name_or_path):
    if not name_or_path:
        return None
    name_or_path = name_or_path.strip()
    for c in list_certs() + letsencrypt_certs():
        if c["name"] == name_or_path or c["path"] == name_or_path:
            return {"name": c["name"], "pem": c["path"],
                    "key": c.get("key_path") or c["path"].replace(".pem", ".key")}
    raise ValueError("找不到证书: " + name_or_path)


def extract_any(src, dest):
    """支持 zip / tar(.gz/.bz2/.xz)，返回解包后的内容目录。"""
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                p = os.path.abspath(os.path.join(dest, n))
                if p != dest and not p.startswith(dest + os.sep):
                    raise ValueError("压缩包包含非法路径: " + n)
            z.extractall(dest)
    else:
        with tarfile.open(src, "r:*") as tar:
            safe_extract(tar, dest)
    entries = [x for x in os.listdir(dest) if x not in ("__MACOSX", ".DS_Store")]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        return os.path.join(dest, entries[0])
    return dest


def write_site(name, content):
    avail = os.path.join(CFG["nginx"]["sites_available"], name)
    link = os.path.join(CFG["nginx"]["sites_enabled"], name)
    with open(avail, "w", encoding="utf-8") as f:
        f.write(content)
    same = os.path.abspath(avail) == os.path.abspath(link)
    if not same and not os.path.lexists(link):
        os.symlink(avail, link)
    ok, msg = nginx_test()
    if not ok:
        try:
            os.remove(avail)
        except OSError:
            pass
        if not same and os.path.islink(link):
            os.remove(link)
        return False, "nginx -t 失败，已撤销：" + msg
    run(["systemctl", "reload", "nginx"], timeout=30)
    return True, "已写入并重载"


def create_full_site(data, archive=None):
    name = (data.get("name") or "").strip().lower()
    if not name.endswith(".conf"):
        name += ".conf"
    if not re.match(r"^[a-z0-9][a-z0-9._-]*\.conf$", name):
        raise ValueError("站点名只能用字母数字点横线")
    if os.path.exists(os.path.join(CFG["nginx"]["sites_available"], name)):
        raise ValueError("同名站点已存在")
    domains = [x for x in re.split(r"[\s,]+", (data.get("domains") or "").strip()) if x]
    if not domains:
        raise ValueError("至少填一个域名")
    for d in domains:
        if not re.match(r"^(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$", d):
            raise ValueError("域名格式不对: " + d)
    mode = data.get("mode") or "proxy"
    base = name[:-5]
    path = (data.get("path") or ("/var/www/" + base)).strip()
    if not path.startswith("/"):
        raise ValueError("部署路径必须是绝对路径")
    cert = resolve_cert(data.get("cert")) if data.get("cert") else None

    app = None
    port = None
    if mode == "proxy":
        try:
            port = int(data.get("port"))
        except (TypeError, ValueError):
            raise ValueError("代理模式需要填端口")
        if not 1 <= port <= 65535:
            raise ValueError("端口不合法")
        app = next((a for a in CFG["apps"]
                    if str(a.get("port") or "") == str(port)), None)
        if not app and data.get("command"):
            os.makedirs(path, exist_ok=True)
            ok, msg = create_app({
                "id": base, "name": data.get("title") or base, "unit": base + ".service",
                "path": path, "command": data.get("command"), "port": port,
                "health_url": "http://127.0.0.1:%s/" % port,
            })
            if not ok:
                raise ValueError(msg)
            app = find_app(base)
        elif not app and not data.get("command"):
            raise ValueError("这个端口没有对应服务，请填启动命令让面板自动创建 systemd 单元")
    else:
        path = (data.get("path") or "/var/www/" + base).strip()
        os.makedirs(path, exist_ok=True)

    if archive:
        tmp = tempfile.mkdtemp(prefix="upload-", dir=os.path.join(CFG["state_dir"], "staging"))
        try:
            content_dir = extract_any(archive, tmp)
            excludes = ["--exclude", "data", "--exclude", ".git",
                        "--exclude", "__pycache__", "--exclude", ".DS_Store"]
            rc, out, err = run(["rsync", "-a"] + excludes +
                               [content_dir + "/", path + "/"], timeout=600)
            if rc != 0:
                raise RuntimeError("源码写入失败: " + (err or out))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        run(["chown", "-R", CFG.get("default_owner", "www-data:www-data"), path], timeout=120)
        if app:
            run(["systemctl", "restart", app["unit"]], timeout=60)

    conf = build_site_conf(name, domains, mode, port if mode == "proxy" else path, cert)
    ok, msg = write_site(name, conf)
    if not ok:
        raise ValueError(msg)
    audit("site.create", "%s %s" % (name, " ".join(domains)))
    return {"name": name, "path": path, "app": app["id"] if app else None,
            "cert": cert["name"] if cert else None, "message": msg}


def create_site(data):
    name = (data.get("name") or "").strip().lower()
    if not name.endswith(".conf"):
        name += ".conf"
    if not re.match(r"^[a-z0-9][a-z0-9._-]*\.conf$", name):
        raise ValueError("文件名只能用字母数字点横线，如 example.conf")
    avail = os.path.join(CFG["nginx"]["sites_available"], name)
    link = os.path.join(CFG["nginx"]["sites_enabled"], name)
    if os.path.exists(avail) or os.path.lexists(link):
        raise ValueError("同名站点已存在")
    domains = [x for x in re.split(r"[\s,]+", (data.get("domains") or "").strip()) if x]
    if not domains:
        raise ValueError("至少填一个域名")
    for d in domains:
        if not re.match(r"^(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$", d):
            raise ValueError("域名格式不对: " + d)
    mode = data.get("mode") or "proxy"
    if mode == "proxy":
        try:
            port = int(data.get("port"))
        except (TypeError, ValueError):
            raise ValueError("代理模式需要填端口")
        if not 1 <= port <= 65535:
            raise ValueError("端口不合法")
        content = proxy_template(name, domains, port)
    elif mode == "static":
        root = (data.get("root") or "").strip() or ("/var/www/" + name[:-5])
        if not root.startswith("/"):
            raise ValueError("根目录必须是绝对路径")
        content = static_template(name, domains, root)
    else:
        raise ValueError("未知站点类型")
    with open(avail, "w", encoding="utf-8") as f:
        f.write(content)
    same_dir = os.path.abspath(avail) == os.path.abspath(link)
    if not same_dir:
        os.symlink(avail, link)
    ok, msg = nginx_test()
    if not ok:
        if not same_dir:
            os.remove(link)
        os.remove(avail)
        return False, "nginx -t 失败，已撤销：" + msg
    rc, out, err = run(["systemctl", "reload", "nginx"], timeout=30)
    audit("site.create", "%s %s" % (name, " ".join(domains)), ok=rc == 0)
    return True, "已创建 %s（%s）" % (name, " ".join(domains))


def toggle_site(name, enabled):
    if not re.match(r"^[A-Za-z0-9._-]+\.conf$", name):
        raise ValueError("非法站点名")
    avail = os.path.join(CFG["nginx"]["sites_available"], name)
    link = os.path.join(CFG["nginx"]["sites_enabled"], name)
    if not os.path.exists(avail):
        raise ValueError("sites-available 里没有 " + name)
    if enabled and os.path.lexists(link):
        return True, "已经是启用状态"
    if not enabled:
        if not os.path.islink(link):
            raise ValueError("不是软链接，无法自动停用")
        os.remove(link)
    else:
        os.symlink(avail, link)
    ok, msg = nginx_test()
    if not ok:
        if enabled:
            os.remove(link)
        else:
            os.symlink(avail, link)
        return False, "nginx -t 失败，已还原：" + msg
    run(["systemctl", "reload", "nginx"], timeout=30)
    audit("site.toggle", "%s -> %s" % (name, "on" if enabled else "off"))
    return True, "已" + ("启用" if enabled else "停用") + " " + name


def delete_site(name, delete_app=False, delete_files=False, confirm=""):
    if not re.match(r"^[A-Za-z0-9._-]+\.conf$", name):
        raise ValueError("非法站点名")
    avail = os.path.join(CFG["nginx"]["sites_available"], name)
    link = os.path.join(CFG["nginx"]["sites_enabled"], name)
    if not os.path.exists(avail):
        raise ValueError("sites-available 里没有 " + name)
    with open(avail, "r", encoding="utf-8", errors="replace") as f:
        meta = parse_site(f.read())
    port = None
    for up in meta.get("upstreams", []):
        m = re.search(r":(\d+)$", up)
        if m:
            port = m.group(1)
            break
    app = next((a for a in CFG["apps"]
                if port and (str(a.get("port") or "") == port
                             or (":%s" % port) in (a.get("health_url") or ""))), None)
    path = app["path"] if app else ((meta.get("roots") or [""])[0])
    others = [x for x in list_sites() if x["name"] != name]

    if delete_app and app:
        for other in others:
            if port and any((":%s" % port) in u for u in other.get("upstreams", [])):
                return False, "站点 %s 也在用端口 %s，不能删除这个服务" % (other["name"], port)
        if not os.path.exists(os.path.join(CFG.get("systemd_dir", "/etc/systemd/system"), app["unit"])):
            delete_app = False

    if delete_files:
        if confirm != name:
            raise ValueError("删除网站目录需要输入站点名确认")
        if not path or len(path.strip("/").split("/")) < 2:
            raise ValueError("目录路径不安全，拒绝删除")
        for other in others:
            other_paths = (other.get("roots") or []) + [u.split(":")[0] for u in other.get("upstreams", [])]
            if path in other_paths or any(p.startswith(path + "/") for p in other_paths):
                return False, "路径 %s 仍被其他站点使用，拒绝删除" % path

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(CFG["backup_dir"], "nginx", "deleted-%s-%s" % (ts, name))
    shutil.copy2(avail, backup)
    had_link = os.path.islink(link)
    if had_link:
        os.remove(link)
    os.remove(avail)
    ok, msg = nginx_test()
    if not ok:
        shutil.copy2(backup, avail)
        if had_link:
            os.symlink(avail, link)
        return False, "nginx -t 失败，已恢复：" + msg
    run(["systemctl", "reload", "nginx"], timeout=30)

    done = ["nginx 配置"]
    if delete_app and app:
        run(["systemctl", "stop", app["unit"]], timeout=60)
        run(["systemctl", "disable", app["unit"]], timeout=30)
        unit_path = os.path.join(CFG.get("systemd_dir", "/etc/systemd/system"), app["unit"])
        try:
            os.remove(unit_path)
        except OSError:
            pass
        run(["systemctl", "daemon-reload"], timeout=30)
        CFG["apps"] = [a for a in CFG["apps"] if a["id"] != app["id"]]
        save_config()
        done.append("systemd %s" % app["unit"])
    if delete_files and path:
        shutil.rmtree(path, ignore_errors=True)
        done.append("目录 %s" % path)
    audit("site.delete", "%s -> %s" % (name, ", ".join(done)))
    return True, "已删除 %s（%s；配置备份：%s）" % (name, "、".join(done), backup)


def save_site(name, content):
    path = site_path(name)
    os.makedirs(os.path.join(CFG["backup_dir"], "nginx"), exist_ok=True)
    backup = os.path.join(CFG["backup_dir"], "nginx",
                          "%s-%s.bak" % (datetime.now().strftime("%Y%m%d-%H%M%S"), name))
    shutil.copy2(path, backup)
    tmp = path + ".ops-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    shutil.move(tmp, path)
    ok, msg = nginx_test()
    if not ok:
        shutil.copy2(backup, path)
        audit("site.save", "%s 语法错误，已还原" % name, ok=False)
        return False, "nginx -t 失败，已还原原配置：\n" + msg
    rc, out, err = run(["systemctl", "reload", "nginx"], timeout=30)
    audit("site.save", "%s 已保存并 reload" % name, ok=rc == 0)
    if rc != 0:
        return False, "已保存，但 reload 失败：" + (err or out)
    return True, "已保存并生效（备份：%s）" % backup


def cert_info(path, key_path=None):
    rc, out, _ = run(["openssl", "x509", "-in", path, "-noout",
                      "-subject", "-issuer", "-enddate"], timeout=15)
    if rc != 0:
        return None
    info = {"name": os.path.basename(path).replace(".pem", ""), "path": path}
    for line in out.splitlines():
        if line.startswith("subject="):
            info["subject"] = line[8:].strip()
        elif line.startswith("issuer="):
            info["issuer"] = line[7:].strip()
        elif line.startswith("notAfter="):
            raw = line[9:].strip()
            try:
                dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z")
                info["not_after"] = dt.strftime("%Y-%m-%d")
                info["days_left"] = (dt - datetime.now(timezone.utc).replace(tzinfo=None)).days
            except ValueError:
                info["not_after"] = raw
    key = key_path or (path[:-4] + ".key")
    info["key_path"] = key
    info["has_key"] = os.path.exists(key)
    return info


def list_certs():
    d = CFG["certs_dir"]
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if name.endswith(".pem"):
            info = cert_info(os.path.join(d, name))
            if info:
                out.append(info)
    return out


def save_cert(name, cert, key):
    if not re.match(r"^[A-Za-z0-9._-]+$", name):
        raise ValueError("证书名只能用字母、数字、点、下划线、横线")
    cert = (cert or "").strip()
    key = (key or "").strip()
    if "BEGIN CERTIFICATE" not in cert or "PRIVATE KEY" not in key:
        raise ValueError("证书或私钥格式不正确（缺少 PEM 头）")
    with tempfile.TemporaryDirectory() as td:
        cp = os.path.join(td, "cert.pem")
        kp = os.path.join(td, "key.pem")
        with open(cp, "w", encoding="utf-8") as f:
            f.write(cert + "\n")
        with open(kp, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        rc1, mod1, err1 = run(["openssl", "x509", "-in", cp, "-noout", "-modulus"])
        rc2, mod2, _ = run(["openssl", "rsa", "-in", kp, "-noout", "-modulus"])
        if rc1 == 0 and rc2 == 0 and mod1 and mod2:
            if mod1 != mod2:
                raise ValueError("证书与私钥不匹配")
        else:
            rc1, pub1, err1 = run(["openssl", "x509", "-in", cp, "-noout", "-pubkey"])
            rc2, pub2, err2 = run(["openssl", "pkey", "-in", kp,
                                   "-noout", "-pubout", "-outform", "PEM"])
            if rc1 != 0 or not pub1:
                raise ValueError("证书解析失败：" + (err1 or "未知错误"))
            if rc2 != 0 or not pub2:
                raise ValueError("私钥解析失败：" + (err2 or "未知错误"))
            if hashlib.sha256(pub1.encode()).hexdigest() != hashlib.sha256(pub2.encode()).hexdigest():
                raise ValueError("证书与私钥不匹配")
    d = CFG["certs_dir"]
    os.makedirs(d, mode=0o700, exist_ok=True)
    pem = os.path.join(d, name + ".pem")
    kp = os.path.join(d, name + ".key")
    with open(pem, "w", encoding="utf-8") as f:
        f.write(cert.strip() + "\n")
    with open(kp, "w", encoding="utf-8") as f:
        f.write(key.strip() + "\n")
    os.chmod(pem, 0o644)
    os.chmod(kp, 0o600)
    audit("cert.save", name)
    return cert_info(pem)


# --------------------------------------------------------------------------
# 部署
# --------------------------------------------------------------------------
def find_app(app_id):
    for a in CFG["apps"]:
        if a["id"] == app_id:
            return a
    return None


def releases_path(app_id):
    return os.path.join(CFG["state_dir"], "releases-%s.json" % app_id)


def load_releases(app_id):
    p = releases_path(app_id)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_releases(app_id, items):
    p = releases_path(app_id)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[:30], f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def prune_backups(app, keep=5):
    d = os.path.join(CFG["backup_dir"], "releases")
    files = sorted([os.path.join(d, f) for f in os.listdir(d)
                    if f.startswith(app["id"] + "-") and f.endswith(".tgz")])
    for f in files[:-keep]:
        try:
            os.remove(f)
        except OSError:
            pass


def safe_extract(tar, dest):
    dest = os.path.abspath(dest)
    for m in tar.getmembers():
        p = os.path.abspath(os.path.join(dest, m.name))
        if p != dest and not p.startswith(dest + os.sep):
            raise ValueError("压缩包包含非法路径: %s" % m.name)
        if m.issym() or m.islnk():
            raise ValueError("压缩包包含链接，已拒绝: %s" % m.name)
    tar.extractall(dest)


def restore_backup(app, tgz):
    """把备份包还原到目标目录（rsync --delete，保留 data/）。"""
    tmp = tempfile.mkdtemp(prefix="restore-", dir=os.path.join(CFG["state_dir"], "staging"))
    try:
        with tarfile.open(tgz, "r:gz") as tar:
            safe_extract(tar, tmp)
        excludes = []
        for e in app["exclude"]:
            excludes += ["--exclude", e]
        rc, out, err = run(["rsync", "-a", "--delete"] + excludes +
                           [tmp + "/", app["path"] + "/"], timeout=600)
        return rc == 0, (err or out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def health_check(url, retries=6, delay=2):
    if not url:
        return True, "未配置健康检查"
    last = ""
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, method="GET",
                                         headers={"User-Agent": "ops-panel"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 400:
                    return True, "健康检查通过 (%s)" % resp.status
                last = "HTTP %s" % resp.status
        except Exception as e:
            last = str(e)
        time.sleep(delay)
    return False, "健康检查失败: %s" % last


def stop_start(app):
    rc, out, err = run(["systemctl", "restart", app["unit"]], timeout=60)
    if rc != 0:
        return False, "重启失败: " + (err or out)
    time.sleep(2)
    st = service_status(app["unit"])
    if not st.get("active"):
        return False, "服务未进入 active 状态: %s" % st.get("error", "")
    return True, "服务已重启"


def chown(app):
    rc, out, err = run(["chown", "-R", app["owner"], app["path"]], timeout=120)
    return rc == 0, (err or out)


def extract_release(app, tgz_path):
    staging = tempfile.mkdtemp(prefix="staging-", dir=os.path.join(CFG["state_dir"], "staging"))
    with tarfile.open(tgz_path, "r:gz") as tar:
        safe_extract(tar, staging)
    return staging


def snapshot(path, dest, excludes, timeout=300):
    """把目录打成 tar.gz（排除 data 等），用于失败回滚或版本快照。"""
    args = ["tar", "czf", dest, "-C", path]
    for e in excludes:
        args += ["--exclude", e]
    args.append(".")
    rc, out, err = run(args, timeout=timeout)
    return rc == 0, (err or out)


def deploy(app_id, tgz_path):
    app = find_app(app_id)
    if not app:
        raise ValueError("未知应用")
    staging = extract_release(app, tgz_path)
    if not any(os.scandir(staging)):
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError("压缩包是空的")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rel_dir = os.path.join(CFG["backup_dir"], "releases")
    prev = os.path.join(rel_dir, "%s-%s-prev.tgz" % (app["id"], ts))
    artifact = os.path.join(rel_dir, "%s-%s.tgz" % (app["id"], ts))
    had_old = os.path.isdir(app["path"]) and bool(os.listdir(app["path"]))
    if had_old:
        ok, msg = snapshot(app["path"], prev, app["exclude"])
        if not ok:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError("备份旧版本失败: " + msg)

    excludes = []
    for e in app["exclude"]:
        excludes += ["--exclude", e]
    rc, out, err = run(["rsync", "-a", "--delete"] + excludes +
                       [staging + "/", app["path"] + "/"], timeout=600)
    shutil.rmtree(staging, ignore_errors=True)
    if rc != 0:
        raise RuntimeError("同步文件失败: " + (err or out))

    def fail(reason):
        if had_old:
            restore_backup(app, prev)
            chown(app)
            run(["systemctl", "restart", app["unit"]], timeout=60)
        items = load_releases(app_id)
        items.insert(0, {"id": ts, "ts": ts, "status": "failed", "note": reason})
        save_releases(app_id, items)
        audit("deploy", "%s 部署失败: %s" % (app_id, reason), ok=False)
        if os.path.exists(prev):
            os.remove(prev)
        prefix = "部署失败，已自动回滚：" if had_old else "部署失败："
        return {"ok": False, "error": prefix + reason}

    ok, msg = chown(app)
    if not ok:
        return fail("改属主失败: " + msg)

    ok, msg = stop_start(app)
    if ok:
        ok, hmsg = health_check(app["health_url"])
        msg = hmsg
    if not ok:
        return fail(msg)

    # 部署成功：把当前状态打成快照，作为这个版本的“回滚点”
    ok, smsg = snapshot(app["path"], artifact, app["exclude"])
    note = msg if ok else (msg + "（快照失败，无法回滚到此版本）")
    if os.path.exists(prev):
        os.remove(prev)
    prune_backups(app)
    items = load_releases(app_id)
    items.insert(0, {"id": ts, "ts": ts, "status": "ok", "note": note,
                     "artifact": artifact if ok else "",
                     "size": os.path.getsize(artifact) if ok else 0})
    save_releases(app_id, items)
    audit("deploy", "%s 部署成功 (%s)" % (app_id, msg))
    return {"ok": True, "release": ts, "message": note}


def rollback(app_id, release_id):
    app = find_app(app_id)
    if not app:
        raise ValueError("未知应用")
    items = load_releases(app_id)
    item = next((i for i in items if i["id"] == release_id), None)
    if not item or not item.get("artifact") or not os.path.exists(item["artifact"]):
        raise ValueError("找不到这个版本的快照")
    ok, msg = restore_backup(app, item["artifact"])
    if not ok:
        raise RuntimeError("还原失败: " + msg)
    chown(app)
    ok, msg = stop_start(app)
    if ok:
        ok, msg = health_check(app["health_url"])
    audit("rollback", "%s -> %s: %s" % (app_id, release_id, msg), ok=ok)
    if not ok:
        return {"ok": False, "error": msg}
    return {"ok": True, "message": "已回滚到 " + release_id + "，" + msg}


# --------------------------------------------------------------------------
# 服务创建 / 发现
# --------------------------------------------------------------------------
def create_app(data):
    app_id = (data.get("id") or "").strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", app_id):
        raise ValueError("应用 ID 只能是小写字母、数字、横线")
    if find_app(app_id):
        raise ValueError("应用 ID 已存在")
    name = (data.get("name") or app_id).strip()
    unit = (data.get("unit") or (app_id + ".service")).strip()
    if not re.match(r"^[A-Za-z0-9_.@-]+\.service$", unit):
        raise ValueError("unit 名称不合法")
    path = (data.get("path") or ("/var/www/" + app_id)).strip()
    if not path.startswith("/"):
        raise ValueError("目录必须是绝对路径")
    command = (data.get("command") or "").strip()
    if not command:
        raise ValueError("启动命令不能为空")
    port = data.get("port") or ""
    health = (data.get("health_url") or "").strip()
    if not health and str(port).isdigit():
        health = "http://127.0.0.1:%s/" % port

    unit_path = os.path.join(CFG.get("systemd_dir", "/etc/systemd/system"), unit)
    if os.path.exists(unit_path):
        raise ValueError("systemd 单元已存在: " + unit_path)

    os.makedirs(path, exist_ok=True)
    owner = CFG.get("default_owner", "www-data:www-data")
    run(["chown", "-R", owner, path], timeout=60)

    lines = [
        "[Unit]",
        "Description=" + name,
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        "User=www-data",
        "Group=www-data",
        "WorkingDirectory=" + path,
        "ExecStart=" + command,
        "Restart=always",
        "RestartSec=5",
        "Environment=PYTHONUNBUFFERED=1",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    with open(unit_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    run(["systemctl", "daemon-reload"], timeout=30)
    rc, out, err = run(["systemctl", "enable", "--now", unit], timeout=60)
    audit("app.create", "%s (%s)" % (app_id, unit), ok=rc == 0)
    if rc != 0:
        os.remove(unit_path)
        run(["systemctl", "daemon-reload"], timeout=30)
        return False, "服务启动失败：" + (err or out)

    CFG["apps"].append({
        "id": app_id, "name": name, "unit": unit, "path": path,
        "owner": owner, "health_url": health, "url": data.get("url", ""),
        "port": int(port) if str(port).isdigit() else "",
        "exclude": ["data", ".git", "__pycache__", "*.tar.gz", "*.log", "._*", ".DS_Store"],
    })
    save_config()
    return True, "已创建应用 %s，目录 %s，服务 %s" % (app_id, path, unit)


def import_app(data):
    app_id = (data.get("id") or "").strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", app_id):
        raise ValueError("应用 ID 只能是小写字母、数字、横线")
    if find_app(app_id):
        raise ValueError("应用 ID 已存在")
    unit = (data.get("unit") or "").strip()
    path = (data.get("path") or "").strip()
    if not unit or not path:
        raise ValueError("缺少 unit 或目录")
    if not os.path.exists(os.path.join(CFG.get("systemd_dir", "/etc/systemd/system"), unit)):
        raise ValueError("systemd 单元不存在: " + unit)
    port = data.get("port") or ""
    health = (data.get("health_url") or "").strip()
    if not health and str(port).isdigit():
        health = "http://127.0.0.1:%s/" % port
    CFG["apps"].append({
        "id": app_id, "name": data.get("name") or app_id, "unit": unit,
        "path": path, "owner": data.get("owner") or CFG.get("default_owner", "www-data:www-data"),
        "health_url": health, "url": data.get("url", ""),
        "port": int(port) if str(port).isdigit() else "",
        "exclude": ["data", ".git", "__pycache__", "*.tar.gz", "*.log", "._*", ".DS_Store"],
    })
    save_config()
    audit("app.import", "%s (%s)" % (app_id, unit))
    return True, "已加入监控与部署列表：" + unit


def letsencrypt_certs():
    le = "/etc/letsencrypt/live"
    out = []
    if os.path.isdir(le):
        for d in sorted(os.listdir(le)):
            full = os.path.join(le, d, "fullchain.pem")
            if os.path.exists(full):
                info = cert_info(full, os.path.join(le, d, "privkey.pem"))
                if info:
                    info["name"] = d
                    out.append(info)
    return out


def discover():
    known_units = {a["unit"] for a in CFG["apps"]}
    units = []
    rc, out, _ = run(["systemctl", "list-units", "--type=service", "--state=running",
                      "--no-legend", "--no-pager"], timeout=20)
    for line in (out or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if unit in known_units or not unit.endswith(".service"):
            continue
        rc2, info, _ = run(["systemctl", "show", unit,
                            "-p", "ExecStart", "-p", "WorkingDirectory"], timeout=10)
        if "/var/www/" not in info:
            continue
        wd = ""
        m = re.search(r"WorkingDirectory=(\S+)", info)
        if m:
            wd = m.group(1)
        argv = []
        m = re.search(r"argv\[\]=([^;]+);", info)
        if m:
            argv = m.group(1).split()
        path = wd if wd.startswith("/var/www/") else ""
        if not path:
            path = next((a for a in argv if a.startswith("/var/www/")), "")
        if not path:
            m = re.search(r"/var/www/([A-Za-z0-9._-]+)", info)
            if m:
                path = "/var/www/" + m.group(1)
        port = 0
        for a in reversed(argv):
            if a.isdigit() and 0 < int(a) < 65536:
                port = int(a)
                break
        units.append({"unit": unit, "path": path, "port": port, "command": " ".join(argv)[:200]})

    return {"sites": list_sites(), "units": units,
            "certs": list_certs(), "letsencrypt": letsencrypt_certs(),
            "apps": [{"id": a["id"], "unit": a["unit"], "path": a["path"]} for a in CFG["apps"]]}


# --------------------------------------------------------------------------
# 服务器指标
# --------------------------------------------------------------------------
def _proc_cpu_sample():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("cpu "):
                    return [int(x) for x in line.split()[1:8]]
    except (OSError, ValueError):
        pass
    return None


def _proc_net_sample():
    rx = tx = 0
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if len(parts) < 10 or parts[0].endswith("lo:"):
                    continue
                rx += int(parts[1])
                tx += int(parts[9])
    except (OSError, ValueError, IndexError):
        pass
    return rx, tx


def collect_metrics():
    """采集 CPU / 内存 / 负载 / 磁盘 / 网络。demo 模式给假数据（Mac 无 /proc）。"""
    demo = bool(CFG.get("metrics_demo"))
    out = {"demo": demo, "ts": now_str()}

    if demo:
        base = (time.time() % 60) / 60
        out["cpu"] = round(6 + 14 * abs(base - 0.5) * 2, 1)
        out["mem"] = {"percent": 41.0, "used": int(0.84e9), "total": int(2e9)}
        out["net"] = {"rx_kbps": 120.5, "tx_kbps": 88.2}
        out["uptime_days"] = 26.4
    else:
        a_cpu = _proc_cpu_sample()
        a_rx, a_tx = _proc_net_sample()
        time.sleep(0.25)
        b_cpu = _proc_cpu_sample()
        b_rx, b_tx = _proc_net_sample()
        if a_cpu and b_cpu:
            delta = [b_cpu[i] - a_cpu[i] for i in range(min(len(a_cpu), len(b_cpu)))]
            total = sum(delta)
            idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
            out["cpu"] = round(100.0 * (1 - idle / total), 1) if total > 0 else 0.0
        if a_rx is not None and b_rx is not None:
            out["net"] = {
                "rx_kbps": round(max(b_rx - a_rx, 0) / 0.25 / 1024, 1),
                "tx_kbps": round(max(b_tx - a_tx, 0) / 0.25 / 1024, 1),
            }
        try:
            info = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.strip().split()[0])
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", info.get("MemFree", 0))
            if total:
                used = total - avail
                out["mem"] = {"percent": round(used / total * 100, 1),
                              "used": used * 1024, "total": total * 1024}
        except (OSError, ValueError):
            pass
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                out["uptime_days"] = round(float(f.read().split()[0]) / 86400, 1)
        except (OSError, ValueError):
            pass

    try:
        l1, l5, l15 = os.getloadavg()
        out["load"] = {"1": round(l1, 2), "5": round(l5, 2), "15": round(l15, 2),
                       "cpu": os.cpu_count() or 1}
    except OSError:
        pass

    disk = shutil.disk_usage("/")
    out["disk"] = {"percent": round(disk.used / disk.total * 100, 1),
                   "used": disk.used, "total": disk.total, "free": disk.free}
    return out


# --------------------------------------------------------------------------
# 推送与巡检
# --------------------------------------------------------------------------
def push(title, body, key, fire_at=None):
    n = CFG.get("notify") or {}
    if not n.get("enabled"):
        return False, "推送未启用"
    base = (n.get("base_url") or "").rstrip("/")
    token = n.get("token") or ""
    if not base or not token:
        return False, "缺少服务地址或 Token"
    payload = {"key": key, "title": title, "body": body}
    if fire_at:
        payload["fireAt"] = fire_at
    if n.get("channels"):
        payload["channels"] = n["channels"]
    req = urllib.request.Request(
        base + "/api/reminders",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "HTTP %s" % resp.status
    except urllib.error.HTTPError as e:
        return False, "HTTP %s: %s" % (e.code, e.read()[:300])
    except Exception as e:
        return False, str(e)


def heartbeat(name, status="ok", message=""):
    n = CFG.get("notify") or {}
    base = (n.get("base_url") or "").rstrip("/")
    token = n.get("token") or ""
    if not name or not base or not token:
        return False, "未配置心跳"
    payload = {"name": name, "status": status, "message": message[:500]}
    req = urllib.request.Request(
        base + "/api/heartbeat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "HTTP %s" % resp.status
    except Exception as e:
        return False, str(e)


class Monitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.path = os.path.join(CFG["state_dir"], "monitor.json")
        self.data = self._load()
        self.data.setdefault("services", {})
        self.data.setdefault("events", [])
        self.data.setdefault("stats", {"checks": {}, "incidents": 0})
        self.stop_flag = False

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def run(self):
        while not self.stop_flag:
            started = time.time()
            try:
                self.tick()
            except Exception as e:
                audit("monitor.error", str(e), ok=False)
            interval = int((CFG.get("notify", {}).get("thresholds") or {})
                           .get("check_interval", 60))
            elapsed = time.time() - started
            time.sleep(max(5, interval - elapsed))

    def event(self, level, kind, title, detail="", sent=None, error=""):
        self.data["events"].insert(0, {
            "ts": now_str(), "level": level, "kind": kind,
            "title": title, "detail": detail[:1500],
            "sent": sent, "error": (error or "")[:500],
        })
        del self.data["events"][200:]

    def alert(self, kind, title, detail, key_prefix="incident"):
        slug = hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
        key = "opspanel:%s:%s:%s:%s" % (key_prefix, kind, slug,
                                        datetime.now().strftime("%Y%m%d%H%M%S"))
        ok, msg = push("[ops-panel] " + title, detail, key)
        self.event("alert" if ok else "error", kind, title, detail, sent=ok,
                   error="" if ok else msg)
        self._save()
        return ok

    def check_app(self, app):
        rc, out, _ = run(["systemctl", "is-active", app["unit"]], timeout=10)
        state = (out or "").strip()
        if state != "active":
            return False, 0, 0, "systemd %s" % (state or "unknown")
        url = app.get("health_url") or ""
        if not url:
            return True, 0, 0, ""
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ops-panel-monitor"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
            ms = int((time.time() - t0) * 1000)
            if 200 <= code < 400:
                return True, code, ms, ""
            return False, code, ms, "HTTP %s" % code
        except Exception as e:
            return False, 0, int((time.time() - t0) * 1000), str(e)

    def tick(self):
        th = CFG.get("notify", {}).get("thresholds") or {}
        fail_need = int(th.get("http_failures", 2))
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        d = self.data
        if d.get("today") != today:
            d["today"] = today
            d["stats"] = {"checks": {}, "incidents": 0}
        stats = d["stats"]

        for app in CFG.get("apps", []):
            st = d["services"].setdefault(app["id"], {"ok": None, "failures": 0})
            ok, code, ms, err = self.check_app(app)
            stats["checks"].setdefault(app["id"], {"ok": 0, "fail": 0})
            stats["checks"][app["id"]]["ok" if ok else "fail"] += 1
            was_down = st.get("ok") is False
            if ok:
                st.update(ok=True, code=code, ms=ms, failures=0, error="",
                          since=st.get("since") or now_str(), ts=now_str())
                if was_down:
                    self.alert("recovery", app["name"] + " 已恢复",
                               "站点 %s（%s）恢复访问，HTTP %s，%s ms。"
                               % (app["name"], app["unit"], code, ms), "recovery")
            else:
                st["failures"] = int(st.get("failures", 0)) + 1
                st.update(ok=False, code=code, ms=ms, error=err, ts=now_str())
                if st["failures"] == fail_need:
                    stats["incidents"] += 1
                    self.alert("site-down", app["name"] + " 无法访问",
                               "站点：%s\n服务：%s\n连续失败：%d 次\n原因：%s"
                               % (app["name"], app["unit"], st["failures"], err))
            st["ts"] = now_str()

        rc, out, _ = run(["systemctl", "is-active", "nginx"], timeout=10)
        active = (out or "").strip() == "active"
        prev = d.get("nginx", {}).get("active")
        d["nginx"] = {"active": active, "ts": now_str()}
        if prev is True and not active:
            self.alert("nginx", "nginx 已停止", "nginx 进程不在 active 状态。")
        elif prev is False and active:
            self.alert("nginx", "nginx 已恢复", "nginx 重新进入 active 状态。", "recovery")

        disk = shutil.disk_usage("/")
        pct = round(disk.used / disk.total * 100, 1)
        warn = float(th.get("disk_percent", 85))
        disk_prev = d.get("disk", {}).get("percent")
        d["disk"] = {"percent": pct, "used": disk.used, "total": disk.total, "ts": now_str()}
        if pct >= warn and (disk_prev is None or disk_prev < warn):
            self.alert("disk", "磁盘使用率 %.1f%%" % pct,
                       "根分区已用 %.1f%%（%s / %s），阈值 %.0f%%。"
                       % (pct, human_size(disk.used), human_size(disk.total), warn))

        cpu = os.cpu_count() or 1
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = 0.0
        ratio = float(th.get("load_ratio", 2))
        load_prev = d.get("load") or {}
        strikes = int(load_prev.get("strikes", 0))
        if load1 > cpu * ratio:
            strikes += 1
        else:
            strikes = 0
        d["load"] = {"load1": round(load1, 2), "cpu": cpu, "strikes": strikes, "ts": now_str()}
        if strikes == 3:
            self.alert("load", "负载偏高 %.2f" % load1,
                       "1 分钟负载 %.2f，CPU 核数 %d，阈值 %.1f 倍。" % (load1, cpu, ratio))

        mem_pct = None
        try:
            info = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.strip().split()[0])
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", info.get("MemFree", 0))
            if total:
                mem_pct = round((total - avail) / total * 100, 1)
        except (OSError, ValueError):
            pass
        mem_warn = float(th.get("mem_percent", 90))
        mem_prev = (d.get("mem") or {}).get("percent")
        d["mem"] = {"percent": mem_pct, "ts": now_str()}
        if mem_pct is not None and mem_pct >= mem_warn and (mem_prev is None or mem_prev < mem_warn):
            self.alert("mem", "内存使用率 %.1f%%" % mem_pct,
                       "内存已用 %.1f%%，阈值 %.0f%%。" % (mem_pct, mem_warn))

        cert_days = int(th.get("cert_days", 14))
        alerted = d.setdefault("certs_alerted", {})
        for c in list_certs():
            days = c.get("days_left")
            if days is None:
                continue
            if days <= cert_days:
                if alerted.get(c["name"]) != today:
                    alerted[c["name"]] = today
                    self.alert("cert", "证书 %s 剩 %d 天" % (c["name"], days),
                               "证书：%s\n到期：%s\n剩余：%d 天\n请及时续期。"
                               % (c["name"], c.get("not_after"), days))
            elif alerted.get(c["name"]):
                alerted.pop(c["name"], None)

        self.maybe_daily(now)
        self.data["updated"] = now_str()
        self._save()

        hb = (CFG.get("notify") or {}).get("heartbeat_name") or ""
        if hb:
            heartbeat(hb, "ok", "巡检完成：%d 个服务，磁盘 %.1f%%" % (len(CFG["apps"]), pct))

    def maybe_daily(self, now):
        at = (CFG.get("notify") or {}).get("daily_at") or "09:00"
        try:
            hh, mm = [int(x) for x in at.split(":")[:2]]
        except ValueError:
            return
        if (now.hour, now.minute) < (hh, mm):
            return
        today = now.strftime("%Y-%m-%d")
        if self.data.get("daily_sent") == today:
            return
        if time.time() - float(self.data.get("daily_last_try", 0)) < 600:
            return
        body = self.daily_text(today)
        ok, msg = push("ops-panel 日报 " + today, body, "opspanel:daily:" + today)
        self.data["daily_last_try"] = time.time()
        if ok:
            self.data["daily_sent"] = today
        self.event("daily" if ok else "error", "daily", "日报 " + today, body,
                   sent=ok, error="" if ok else msg)

    def daily_text(self, today):
        d = self.data
        stats = d.get("stats") or {}
        checks = stats.get("checks") or {}
        lines = ["巡检日期：" + today, ""]
        for app in CFG.get("apps", []):
            c = checks.get(app["id"]) or {}
            ok_n, fail_n = int(c.get("ok", 0)), int(c.get("fail", 0))
            total = ok_n + fail_n
            rate = ("%.2f%%" % (ok_n / total * 100)) if total else "—"
            st = d.get("services", {}).get(app["id"], {})
            now_state = "正常" if st.get("ok") else ("异常" if st.get("ok") is False else "未知")
            lines.append("· %s：可用率 %s（%d/%d），当前 %s"
                         % (app["name"], rate, ok_n, total, now_state))
        lines.append("· 故障次数：%d" % int(stats.get("incidents", 0)))
        nginx = d.get("nginx") or {}
        lines.append("· nginx：%s" % ("运行中" if nginx.get("active") else "未运行"))
        disk = d.get("disk") or {}
        if disk:
            lines.append("· 磁盘：%.1f%%（%s / %s）"
                         % (disk.get("percent", 0), human_size(disk.get("used", 0)),
                            human_size(disk.get("total", 0))))
        certs = list_certs()
        if certs:
            lines.append("· 证书：" + "；".join(
                "%s 剩 %s 天" % (c["name"], c.get("days_left", "—")) for c in certs))
        events = [e for e in d.get("events", []) if e.get("kind") in ("site-down", "nginx", "disk", "cert")]
        if events:
            lines.append("· 最近告警：" + "；".join(
                "%s %s" % (e["ts"][5:16], e["title"]) for e in events[:5]))
        return "\n".join(lines)


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024
    return "%.1f PB" % n


# --------------------------------------------------------------------------
# 防火墙（ufw）
# --------------------------------------------------------------------------
CLOUDFLARE_V4 = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.90.0/24", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]


def firewall_status():
    rc, out, err = run(["ufw", "status", "numbered"], timeout=30)
    installed = rc != 127
    active = False
    rules = []
    if installed:
        for line in (out or "").splitlines():
            if line.startswith("Status:"):
                active = "active" in line and "inactive" not in line
            m = re.match(r"\[\s*(\d+)\]\s+(.+)", line)
            if m:
                rules.append({"index": int(m.group(1)), "rule": m.group(2).strip()})
    return {"installed": installed, "active": active, "rules": rules}


def _ensure_ssh_rule():
    st = firewall_status()
    if not any(r["rule"].startswith("22/") for r in st["rules"]):
        run(["ufw", "allow", "22/tcp"], timeout=30)


def firewall_add(port, proto="tcp", cidr=None):
    if not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("端口不合法")
    if proto not in ("tcp", "udp"):
        raise ValueError("协议只支持 tcp/udp")
    cmd = ["ufw", "allow"]
    if cidr:
        cmd += ["from", cidr]
    cmd += ["to", "any", "port", str(port), "proto", proto]
    rc, out, err = run(cmd, timeout=30)
    audit("firewall.add", "%s %s %s" % (port, proto, cidr or ""), ok=rc == 0)
    return rc == 0, (out + "\n" + err).strip()


def firewall_delete(index):
    rc, out, err = run(["ufw", "--force", "delete", str(index)], timeout=30)
    audit("firewall.delete", str(index), ok=rc == 0)
    return rc == 0, (out + "\n" + err).strip()


def firewall_set(active):
    if active:
        _ensure_ssh_rule()
        rc, out, err = run(["ufw", "--force", "enable"], timeout=60)
    else:
        rc, out, err = run(["ufw", "--force", "disable"], timeout=60)
    audit("firewall.set", "active" if active else "inactive", ok=rc == 0)
    return rc == 0, (out + "\n" + err).strip()


def firewall_cloudflare():
    _ensure_ssh_rule()
    added = skipped = 0
    for cidr in CLOUDFLARE_V4:
        for port in ("80", "443"):
            rc, out, err = run(["ufw", "allow", "from", cidr,
                                "to", "any", "port", port, "proto", "tcp"], timeout=30)
            if rc == 0:
                if "Skipping" in (out or ""):
                    skipped += 1
                else:
                    added += 1
    audit("firewall.cloudflare", "added %d skipped %d" % (added, skipped))
    return True, "新增 %d 条，已存在 %d 条（共 %d 段 Cloudflare IP）" % (added, skipped, len(CLOUDFLARE_V4))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ops-panel/1.0"

    def log_message(self, fmt, *args):
        pass

    def handle_expect_100(self):
        # 大文件上传时 curl 会先发 Expect: 100-continue
        self.send_response_only(100)
        self.end_headers()
        return True

    # ---- 基础 ----
    def _send(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, obj)

    def _cookie(self, name):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _authed(self):
        return session_ok(self._cookie("op_session"))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _require(self):
        if not self._authed():
            self._json({"ok": False, "error": "未登录"}, 401)
            return False
        return True

    # ---- 路由 ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                if not self._authed():
                    return self._redirect("/login")
                return self._file("index.html")
            if path == "/login":
                if self._authed():
                    return self._redirect("/")
                return self._file("login.html")
            if path == "/healthz":
                return self._json({"ok": True, "app": "ops-panel"})
            if path.startswith("/static/"):
                return self._file(os.path.basename(path))
            if not path.startswith("/api/"):
                return self._send(404, "not found", "text/plain; charset=utf-8")
            if path == "/api/overview":
                return self._api_overview()
            if path == "/api/services":
                return self._api_services()
            if path == "/api/logs":
                return self._api_logs(q)
            if path == "/api/sites":
                return self._api_sites()
            if path.startswith("/api/sites/"):
                return self._api_site_get(unquote(path[len("/api/sites/"):]))
            if path == "/api/discover":
                return self._api_discover()
            if path == "/api/firewall":
                return self._api_firewall()
            if path == "/api/settings":
                return self._api_settings()
            if path == "/api/security":
                return self._api_security()
            if path == "/api/monitor":
                return self._api_monitor()
            if path == "/api/certs":
                return self._api_certs()
            if path == "/api/nginx/logs":
                return self._api_nginx_logs(q)
            if path == "/api/audit":
                return self._api_audit(q)
            if path.startswith("/api/releases/"):
                return self._api_releases(path[len("/api/releases/"):])
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        try:
            if path == "/api/login":
                return self._api_login()
            if path == "/api/logout":
                return self._api_logout()
            if path.startswith("/api/deploy/"):
                return self._api_deploy(path[len("/api/deploy/"):])
            if self.headers.get("X-Ops") != "1":
                return self._json({"ok": False, "error": "缺少 CSRF 头"}, 403)
            if not self._require():
                return
            if path.startswith("/api/services/"):
                return self._api_service_action(path)
            if path == "/api/sites/wizard":
                return self._api_site_wizard(u)
            if path == "/api/sites/preview":
                return self._api_site_preview()
            if path == "/api/sites":
                return self._api_site_create()
            if path.startswith("/api/sites/") and path.endswith("/toggle"):
                return self._api_site_toggle(unquote(path[len("/api/sites/"):-len("/toggle")]))
            if path == "/api/apps":
                return self._api_app_create()
            if path == "/api/apps/import":
                return self._api_app_import()
            if path == "/api/settings":
                return self._api_settings_save()
            if path == "/api/security/setup":
                return self._api_security_setup()
            if path == "/api/security/enable":
                return self._api_security_enable()
            if path == "/api/security/disable":
                return self._api_security_disable()
            if path == "/api/monitor/config":
                return self._api_monitor_config()
            if path == "/api/monitor/test":
                return self._api_monitor_test()
            if path == "/api/firewall/add":
                d = self._body()
                ok, msg = firewall_add(d.get("port"), d.get("proto") or "tcp", d.get("cidr"))
                return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            if path == "/api/firewall/delete":
                d = self._body()
                ok, msg = firewall_delete(d.get("index"))
                return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            if path == "/api/firewall/toggle":
                d = self._body()
                ok, msg = firewall_set(bool(d.get("active")))
                return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            if path == "/api/firewall/cloudflare":
                ok, msg = firewall_cloudflare()
                return self._json({"ok": ok, "message": msg})
            if path.startswith("/api/sites/"):
                return self._api_site_save(unquote(path[len("/api/sites/"):]))
            if path == "/api/nginx/test":
                ok, msg = nginx_test()
                return self._json({"ok": ok, "output": msg})
            if path == "/api/nginx/reload":
                rc, out, err = run(["systemctl", "reload", "nginx"], timeout=30)
                audit("nginx.reload", err or out, ok=rc == 0)
                return self._json({"ok": rc == 0, "output": (out + " " + err).strip()})
            if path == "/api/certs":
                return self._api_cert_save()
            if path.startswith("/api/rollback/"):
                return self._api_rollback(path[len("/api/rollback/"):])
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def do_DELETE(self):
        try:
            if self.headers.get("X-Ops") != "1":
                return self._json({"ok": False, "error": "缺少 CSRF 头"}, 403)
            if not self._require():
                return
            if self.path.startswith("/api/sites/"):
                parsed = urlparse(self.path)
                name = unquote(parsed.path[len("/api/sites/"):])
                qs = parse_qs(parsed.query)
                ok, msg = delete_site(
                    name,
                    delete_app=(qs.get("app") or ["0"])[0] in ("1", "true"),
                    delete_files=(qs.get("files") or ["0"])[0] in ("1", "true"),
                    confirm=(qs.get("confirm") or [""])[0],
                )
                return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            if self.path.startswith("/api/certs/"):
                name = unquote(self.path[len("/api/certs/"):])
                if not re.match(r"^[A-Za-z0-9._-]+$", name):
                    return self._json({"ok": False, "error": "非法名称"}, 400)
                for ext in (".pem", ".key"):
                    p = os.path.join(CFG["certs_dir"], name + ext)
                    if os.path.exists(p):
                        os.remove(p)
                audit("cert.delete", name)
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    # ---- 页面 ----
    def _redirect(self, loc):
        self._send(302, b"", "text/plain", {"Location": loc})

    def _file(self, name):
        if name not in ("index.html", "login.html", "app.js", "style.css"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        p = os.path.join(WEB_DIR, name)
        if not os.path.exists(p):
            return self._send(404, "missing " + name, "text/plain; charset=utf-8")
        ctype = {"js": "application/javascript", "css": "text/css"}.get(
            name.rsplit(".", 1)[-1], "text/html")
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype + "; charset=utf-8",
                       {"Cache-Control": "no-store"})

    # ---- 登录 ----
    def _api_login(self):
        data = self._body()
        pw = data.get("password", "")
        ip = self.client_address[0]
        with FAILS_LOCK:
            rec = FAILS.get(ip, [0, 0])
        if rec[0] >= 5 and time.time() - rec[1] < 300:
            return self._json({"ok": False, "error": "尝试过多，请 5 分钟后再试"}, 429)
        if not verify_password(pw, CFG["password_hash"]):
            with FAILS_LOCK:
                FAILS[ip] = [rec[0] + 1, time.time()]
            audit("login", "密码错误", ok=False)
            return self._json({"ok": False, "error": "密码错误"}, 401)
        if CFG.get("totp_enabled"):
            code = str(data.get("code") or "").strip()
            if not verify_totp(CFG.get("totp_secret"), code) and not consume_recovery(code):
                with FAILS_LOCK:
                    FAILS[ip] = [rec[0] + 1, time.time()]
                audit("login", "动态验证码错误", ok=False)
                return self._json({"ok": False, "error": "动态验证码错误"}, 401)
        with FAILS_LOCK:
            FAILS.pop(ip, None)
        token = new_session()
        audit("login", "登录成功")
        self._send(200, json.dumps({"ok": True}), headers={
            "Set-Cookie": "op_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d"
                          % (token, SESSION_TTL),
        })

    def _api_logout(self):
        drop_session(self._cookie("op_session"))
        self._send(200, json.dumps({"ok": True}), headers={
            "Set-Cookie": "op_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
        })

    # ---- 概览 / 服务 ----
    def _api_overview(self):
        if not self._require():
            return
        services = [service_status(a["unit"]) for a in CFG["apps"]]
        disk = shutil.disk_usage("/")
        try:
            metrics = collect_metrics()
        except Exception as e:
            metrics = {"error": str(e)}
        return self._json({
            "ok": True,
            "services": services,
            "apps": [{"id": a["id"], "name": a["name"], "unit": a["unit"],
                      "path": a["path"], "url": a.get("url", "")} for a in CFG["apps"]],
            "nginx": {"active": nginx_status()},
            "metrics": metrics,
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free,
                     "percent": round(disk.used / disk.total * 100, 1)},
            "certs": list_certs(),
            "time": now_str(),
        })

    def _api_services(self):
        if not self._require():
            return
        return self._json({"ok": True,
                           "services": [service_status(a["unit"]) for a in CFG["apps"]]})

    def _api_service_action(self, path):
        parts = path[len("/api/services/"):].split("/")
        if len(parts) != 2:
            return self._json({"ok": False, "error": "参数错误"}, 400)
        unit, action = unquote(parts[0]), parts[1]
        allowed_units = {a["unit"] for a in CFG["apps"]} | {"nginx.service"}
        if unit not in allowed_units:
            return self._json({"ok": False, "error": "不允许操作该服务"}, 403)
        if action not in ("start", "stop", "restart"):
            return self._json({"ok": False, "error": "不支持的操作"}, 400)
        rc, out, err = run(["systemctl", action, unit], timeout=60)
        audit("service." + action, unit, ok=rc == 0)
        return self._json({"ok": rc == 0, "output": (out + "\n" + err).strip()})

    def _api_logs(self, q):
        if not self._require():
            return
        unit = (q.get("unit") or [""])[0]
        allowed = {a["unit"] for a in CFG["apps"]} | {"nginx.service"}
        if unit not in allowed:
            return self._json({"ok": False, "error": "不允许"}, 403)
        n = min(int((q.get("lines") or ["120"])[0]), 1000)
        return self._json({"ok": True, "log": journal(unit, n)})

    def _api_nginx_logs(self, q):
        if not self._require():
            return
        which = (q.get("which") or ["error"])[0]
        path = CFG["nginx"]["error_log"] if which == "error" else CFG["nginx"]["access_log"]
        n = min(int((q.get("lines") or ["200"])[0]), 2000)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-n:]
            return self._json({"ok": True, "log": "".join(lines)})
        except OSError as e:
            return self._json({"ok": False, "error": str(e)})

    def _api_audit(self, q):
        if not self._require():
            return
        n = min(int((q.get("lines") or ["200"])[0]), 1000)
        try:
            with open(CFG["audit_log"], "r", encoding="utf-8") as f:
                return self._json({"ok": True, "log": "".join(f.readlines()[-n:])})
        except OSError:
            return self._json({"ok": True, "log": ""})

    # ---- 站点 ----
    def _api_sites(self):
        if not self._require():
            return
        sites = list_sites()
        apps = CFG["apps"]
        all_certs = list_certs() + letsencrypt_certs()
        for s in sites:
            port = None
            for up in s.get("upstreams", []):
                m = re.search(r":(\d+)$", up)
                if m:
                    port = m.group(1)
                    break
            app = None
            if port:
                app = next((a for a in apps
                            if str(a.get("port") or "") == port
                            or (":%s" % port) in (a.get("health_url") or "")), None)
            s["app"] = ({"id": app["id"], "name": app["name"], "unit": app["unit"]}
                        if app else None)
            if app:
                rels = load_releases(app["id"])
                s["last_release"] = rels[0] if rels else None
                st = service_status(app["unit"])
                s["app"]["active"] = bool(st.get("active"))
                s["app"]["memory_mb"] = st.get("memory_mb")
            cert_path = (s.get("certs") or [""])[0]
            cert = next((c for c in all_certs
                         if cert_path and c.get("path") == cert_path), None)
            if cert:
                s["cert"] = {"name": cert["name"], "days_left": cert.get("days_left"),
                             "not_after": cert.get("not_after")}
            elif cert_path:
                s["cert"] = {"name": cert_path, "days_left": None, "not_after": None}
            else:
                s["cert"] = None
        return self._json({"ok": True, "sites": sites})

    def _api_site_get(self, name):
        if not self._require():
            return
        path = site_path(name)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        meta = parse_site(content)
        port = None
        for up in meta.get("upstreams", []):
            m = re.search(r":(\d+)$", up)
            if m:
                port = m.group(1)
                break
        app = next((a for a in CFG["apps"]
                    if port and (str(a.get("port") or "") == port
                                 or (":%s" % port) in (a.get("health_url") or ""))), None)
        return self._json({"ok": True, "name": name, "path": path, "content": content,
                           **meta,
                           "app": ({"id": app["id"], "name": app["name"],
                                    "unit": app["unit"]} if app else None)})

    def _api_site_save(self, name):
        data = self._body()
        ok, msg = save_site(name, data.get("content", ""))
        return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

    def _api_site_create(self):
        ok, msg = create_site(self._body())
        return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

    def _api_site_preview(self):
        d = self._body()
        domains = [x for x in re.split(r"[\s,]+", (d.get("domains") or "").strip()) if x]
        if not domains:
            return self._json({"ok": False, "error": "至少填一个域名"}, 400)
        mode = d.get("mode") or "proxy"
        target = (d.get("target") or "").strip()
        cert = resolve_cert(d.get("cert")) if d.get("cert") else None
        try:
            conf = build_site_conf(d.get("name") or "site.conf", domains, mode, target, cert)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        return self._json({"ok": True, "content": conf})

    def _api_site_wizard(self, u):
        data = {k: v[0] for k, v in parse_qs(u.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        archive = None
        if length > 0:
            max_body = int(CFG.get("max_upload_mb", 300)) * 1024 * 1024
            if length > max_body:
                return self._json({"ok": False, "error": "文件超过 %s MB" % CFG.get("max_upload_mb", 300)}, 400)
            archive = os.path.join(CFG["state_dir"], "incoming",
                                   "wizard-%s.bin" % datetime.now().strftime("%Y%m%d-%H%M%S"))
            remaining = length
            with open(archive, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        try:
            result = create_full_site(data, archive)
        except Exception as e:
            audit("site.wizard", str(e), ok=False)
            return self._json({"ok": False, "error": str(e)}, 500)
        finally:
            if archive:
                try:
                    os.remove(archive)
                except OSError:
                    pass
        return self._json({"ok": True, **result})

    def _api_site_toggle(self, name):
        data = self._body()
        ok, msg = toggle_site(name, bool(data.get("enabled")))
        return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

    def _api_app_create(self):
        ok, msg = create_app(self._body())
        return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

    def _api_app_import(self):
        ok, msg = import_app(self._body())
        return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

    def _api_discover(self):
        if not self._require():
            return
        return self._json({"ok": True, **discover()})

    def _api_firewall(self):
        if not self._require():
            return
        return self._json({"ok": True, **firewall_status()})

    def _api_settings(self):
        if not self._require():
            return
        return self._json({
            "ok": True,
            "max_upload_mb": int(CFG.get("max_upload_mb", 300)),
            "bind": CFG.get("bind"),
            "port": CFG.get("port"),
            "apps": len(CFG.get("apps", [])),
        })

    def _api_security(self):
        if not self._require():
            return
        return self._json({"ok": True,
                           "enabled": bool(CFG.get("totp_enabled")),
                           "recovery_left": len(CFG.get("totp_recovery") or [])})

    def _api_security_setup(self):
        secret = gen_totp_secret()
        return self._json({"ok": True, "secret": secret, "uri": otpauth_uri(secret)})

    def _api_security_enable(self):
        d = self._body()
        secret = (d.get("secret") or "").strip()
        if not verify_totp(secret, d.get("code", "")):
            return self._json({"ok": False, "error": "验证码不对，请确认手机时间准确"}, 400)
        codes = [secrets.token_hex(4).upper() for _ in range(8)]
        CFG["totp_enabled"] = True
        CFG["totp_secret"] = secret
        CFG["totp_recovery"] = recovery_hashes(codes)
        save_config()
        audit("security.2fa", "enabled")
        return self._json({"ok": True, "recovery": codes})

    def _api_security_disable(self):
        d = self._body()
        if not verify_password(d.get("password", ""), CFG["password_hash"]):
            return self._json({"ok": False, "error": "密码错误"}, 401)
        if not verify_totp(CFG.get("totp_secret"), d.get("code", "")) and not consume_recovery(d.get("code", "")):
            return self._json({"ok": False, "error": "动态验证码错误"}, 401)
        CFG["totp_enabled"] = False
        CFG["totp_secret"] = ""
        CFG["totp_recovery"] = []
        save_config()
        audit("security.2fa", "disabled")
        return self._json({"ok": True})

    def _api_settings_save(self):
        d = self._body()
        if "max_upload_mb" in d:
            try:
                v = int(d["max_upload_mb"])
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "必须是数字"}, 400)
            if not 1 <= v <= 10240:
                return self._json({"ok": False, "error": "范围 1 - 10240 MB"}, 400)
            CFG["max_upload_mb"] = v
        save_config()
        audit("settings.save", "max_upload_mb=%s" % CFG.get("max_upload_mb"))
        return self._json({"ok": True, "max_upload_mb": CFG.get("max_upload_mb")})

    def _api_monitor(self):
        if not self._require():
            return
        data = dict(MONITOR.data) if MONITOR else {}
        data["ok"] = True
        n = CFG["notify"]
        data["notify"] = {
            "enabled": n["enabled"], "base_url": n["base_url"],
            "has_token": bool(n["token"]), "channels": n["channels"],
            "daily_at": n["daily_at"], "heartbeat_name": n["heartbeat_name"],
            "thresholds": n["thresholds"],
        }
        return self._json(data)

    def _api_monitor_config(self):
        data = self._body()
        n = CFG["notify"]
        for key in ("enabled", "base_url", "daily_at", "heartbeat_name"):
            if key in data:
                n[key] = data[key]
        if data.get("token"):
            n["token"] = data["token"]
        if "channels" in data:
            ch = data["channels"]
            n["channels"] = [x for x in ch if x] if isinstance(ch, list) else \
                [x.strip() for x in str(ch).split(",") if x.strip()]
        if isinstance(data.get("thresholds"), dict):
            for k, v in data["thresholds"].items():
                try:
                    n["thresholds"][k] = int(v)
                except (TypeError, ValueError):
                    pass
        save_config()
        audit("notify.config", "更新告警配置")
        return self._json({"ok": True})

    def _api_monitor_test(self):
        ok, msg = push("ops-panel 测试", "如果你收到这条，说明 ops-panel 的推送配置是通的。\n时间："
                       + now_str(), "opspanel:test:" + datetime.now().strftime("%Y%m%d%H%M%S"))
        return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

    # ---- 证书 ----
    def _api_certs(self):
        if not self._require():
            return
        certs = list_certs()
        for c in certs:
            c["source"] = "local"
        for c in letsencrypt_certs():
            c["source"] = "letsencrypt"
            certs.append(c)
        return self._json({"ok": True, "certs": certs, "dir": CFG["certs_dir"]})

    def _api_cert_save(self):
        data = self._body()
        info = save_cert(data.get("name", "").strip(),
                         data.get("cert", ""), data.get("key", ""))
        return self._json({"ok": True, "cert": info})

    # ---- 部署 ----
    def _api_deploy(self, app_id):
        token = self.headers.get("X-Deploy-Token", "")
        if not (self._authed() or (CFG.get("deploy_token") and
                                   hmac.compare_digest(token, CFG["deploy_token"]))):
            return self._json({"ok": False, "error": "未授权"}, 401)
        if not find_app(app_id):
            return self._json({"ok": False, "error": "未知应用"}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        max_body = int(CFG.get("max_upload_mb", 300)) * 1024 * 1024
        if length <= 0 or length > max_body:
            return self._json({"ok": False, "error": "压缩包为空或超过 %s MB" % CFG.get("max_upload_mb", 300)}, 400)
        tmp = os.path.join(CFG["state_dir"], "incoming",
                           "%s-%s.tgz" % (app_id, datetime.now().strftime("%Y%m%d-%H%M%S")))
        remaining = length
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        try:
            result = deploy(app_id, tmp)
        except Exception as e:
            audit("deploy", "%s 部署异常: %s" % (app_id, e), ok=False)
            push("[ops-panel] 部署异常 " + app_id, str(e),
                 "opspanel:deploy:%s:%s" % (app_id, datetime.now().strftime("%Y%m%d%H%M%S")))
            return self._json({"ok": False, "error": str(e)}, 500)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not result.get("ok"):
            push("[ops-panel] 部署失败 " + app_id, result.get("error", ""),
                 "opspanel:deploy:%s:%s" % (app_id, datetime.now().strftime("%Y%m%d%H%M%S")))
        return self._json(result, 200 if result.get("ok") else 500)

    def _api_releases(self, app_id):
        if not self._require():
            return
        app = find_app(app_id)
        if not app:
            return self._json({"ok": False, "error": "未知应用"}, 404)
        items = load_releases(app_id)
        for i in items:
            i["available"] = bool(i.get("artifact") and os.path.exists(i["artifact"]))
        return self._json({"ok": True, "releases": items})

    def _api_rollback(self, rest):
        parts = rest.split("/")
        if len(parts) != 2:
            return self._json({"ok": False, "error": "参数错误"}, 400)
        result = rollback(parts[0], parts[1])
        return self._json(result, 200 if result.get("ok") else 500)


# --------------------------------------------------------------------------
def main():
    global CFG, CFG_PATH, MONITOR
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/ops-panel/config.json")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default=None)
    ap.add_argument("--hash-password", default=None)
    args = ap.parse_args()

    if args.hash_password is not None:
        print(hash_password(args.hash_password))
        return

    CFG_PATH = args.config
    CFG = load_config(args.config)
    if args.port:
        CFG["port"] = args.port
    if args.bind:
        CFG["bind"] = args.bind
    ensure_dirs()
    MONITOR = Monitor()
    MONITOR.start()

    srv = ThreadingHTTPServer((CFG["bind"], CFG["port"]), Handler)
    srv.daemon_threads = True
    scheme = "http"
    if CFG.get("tls_cert") and CFG.get("tls_key"):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CFG["tls_cert"], CFG["tls_key"])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    if CFG["bind"] not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: 面板监听在 %s，公网可达，请确保已开启 2FA 与 HTTPS" % CFG["bind"])
    print("ops-panel listening on %s://%s:%d" % (scheme, CFG["bind"], CFG["port"]))
    audit("start", "ops-panel 启动")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
