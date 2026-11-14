/* ops-panel 前端 */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3800);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    ...opts,
    headers: { 'X-Ops': '1', ...(opts.headers || {}) },
  });
  if (res.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  const data = await res.json().catch(() => ({ ok: false, error: '响应解析失败' }));
  if (res.status === 403 && data.must_change_password) {
    location.href = '/force-password';
    throw new Error('请先修改初始密码');
  }
  return data;
}

function statusBadge(svc) {
  if (!svc) return '<span class="badge">未知</span>';
  if (svc.active) return '<span class="badge ok">运行中</span>';
  if (svc.ActiveState === 'activating') return '<span class="badge warn">启动中</span>';
  if (svc.ActiveState === 'failed') return '<span class="badge err">失败</span>';
  if (svc.ActiveState === 'inactive') return '<span class="badge">已停止</span>';
  return '<span class="badge">' + esc(svc.ActiveState || '未知') + '</span>';
}

let refreshTimer = null;
function stopRefresh() { if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; } }

/* ------------------------------ 概览 ------------------------------ */
async function renderOverview() {
  stopRefresh();
  $('#main').innerHTML = '<h1>概览</h1><div class="sub">加载中…</div>';
  const d = await api('/api/overview');
  if (!d.ok) { $('#main').innerHTML = '<h1>概览</h1><div class="sub">' + esc(d.error) + '</div>'; return; }

  const byUnit = {};
  (d.services || []).forEach(s => { byUnit[s.unit] = s; });

  const appCards = (d.apps || []).map(app => {
    const s = byUnit[app.unit] || {};
    return '<div class="card">' +
      '<div class="svc"><div><div class="name">' + esc(app.name) + '</div>' +
      '<div class="unit">' + esc(app.unit) + '</div></div>' + statusBadge(s) + '</div>' +
      '<div class="meta">' +
      '<span>内存 <b>' + (s.memory_mb != null ? esc(s.memory_mb) + ' MB' : '—') + '</b></span>' +
      '<span>PID <b>' + esc(s.MainPID || '—') + '</b></span>' +
      '<span>重启 <b>' + esc(s.NRestarts || '0') + '</b></span>' +
      '</div>' +
      '<div class="meta"><span>' + esc(app.path) + '</span></div>' +
      '<div class="toolbar">' +
      '<button class="sm" data-restart="' + esc(app.unit) + '">重启</button>' +
      '<button class="sm ghost" data-logs="' + esc(app.unit) + '">日志</button>' +
      (app.url ? '<a class="btn sm ghost" href="' + esc(app.url) + '" target="_blank" rel="noreferrer">打开 ↗</a>' : '') +
      '</div></div>';
  }).join('');

  const m = d.metrics || {};
  const bar = (pct) => {
    const cls = pct > 88 ? 'err' : pct > 72 ? 'warn' : '';
    return '<div class="bar"><i class="' + cls + '" style="width:' + Math.min(pct, 100) + '%"></i></div>';
  };
  const mem = m.mem || {};
  const load = m.load || {};
  const disk = d.disk || {};
  const diskPct = disk.percent || 0;

  const kpiCards =
    '<div class="card"><h3>CPU</h3><div class="big">' + (m.cpu != null ? m.cpu + '%' : '—') + '</div>' +
    '<div class="meta"><span>' + esc(load.cpu || '—') + ' 核</span>' + (m.demo ? '<span class="hint">演示数据</span>' : '') + '</div></div>' +
    '<div class="card"><h3>内存</h3><div class="big">' + (mem.percent != null ? mem.percent + '%' : '—') + '</div>' +
    (mem.percent != null ? bar(mem.percent) : '') +
    '<div class="meta"><span>已用 <b>' + fmtBytes(mem.used) + '</b></span><span>共 <b>' + fmtBytes(mem.total) + '</b></span></div></div>' +
    '<div class="card"><h3>负载</h3><div class="big">' + esc(load['1'] != null ? load['1'] : '—') + '</div>' +
    '<div class="meta"><span>5m <b>' + esc(load['5'] != null ? load['5'] : '—') + '</b></span>' +
    '<span>15m <b>' + esc(load['15'] != null ? load['15'] : '—') + '</b></span></div></div>' +
    '<div class="card"><h3>磁盘</h3><div class="big">' + diskPct + '%</div>' + bar(diskPct) +
    '<div class="meta"><span>已用 <b>' + fmtBytes(disk.used) + '</b></span><span>可用 <b>' + fmtBytes(disk.free) + '</b></span></div></div>' +
    (m.net ? '<div class="card"><h3>网络</h3><div class="big">' + esc(m.net.rx_kbps) + '</div>' +
      '<div class="meta"><span>↓ <b>' + esc(m.net.rx_kbps) + ' KB/s</b></span><span>↑ <b>' + esc(m.net.tx_kbps) + ' KB/s</b></span></div></div>' : '') +
    (m.uptime_days != null ? '<div class="card"><h3>运行时长</h3><div class="big">' + esc(m.uptime_days) + ' 天</div></div>' : '');

  const certRows = (d.certs || []).map(c => {
    const days = c.days_left;
    const cls = days == null ? '' : days < 14 ? 'err' : days < 30 ? 'warn' : 'ok';
    return '<tr><td class="mono">' + esc(c.name) + '</td><td>' + esc(c.not_after || '—') +
      '</td><td><span class="badge ' + cls + '">' + (days == null ? '—' : days + ' 天') + '</span></td></tr>';
  }).join('') || '<tr><td colspan="3" class="hint">还没有上传证书</td></tr>';

  $('#main').innerHTML =
    '<h1>概览</h1><div class="sub">' + esc(d.time) + ' · 每 15 秒自动刷新</div>' +
    '<div class="grid">' + kpiCards + '</div>' +
    '<div class="card" style="margin-top:14px"><div class="svc">' +
    '<div class="name">' + (d.nginx && d.nginx.active ? '<span class="dot ok"></span>nginx 运行中' : '<span class="dot err"></span>nginx 未运行') +
    (d.nginx && d.nginx.version ? '<span class="hint" style="margin-left:10px">v' + esc(d.nginx.version) + '</span>' : '') +
    '<span class="hint" style="margin-left:10px">证书 ' + (d.certs || []).length + ' 张</span></div>' +
    '<button class="sm" data-nginx-reload="1">重载配置</button></div></div>' +
    '<h1 style="font-size:16px;margin-top:22px">站点服务</h1>' +
    '<div class="svc-grid">' + appCards + '</div>' +
    '<div class="card" style="margin-top:14px"><h3>证书</h3><table><thead><tr><th>名称</th><th>到期</th><th>剩余</th></tr></thead><tbody>' +
    certRows + '</tbody></table></div>';

  $$('[data-restart]').forEach(b => b.onclick = () => restartUnit(b.dataset.restart, b));
  $$('[data-logs]').forEach(b => b.onclick = () => { location.hash = '#/logs?unit=' + encodeURIComponent(b.dataset.logs); });
  const nr = $('[data-nginx-reload]');
  if (nr) nr.onclick = async () => {
    const r = await api('/api/nginx/reload', { method: 'POST' });
    toast(r.ok ? 'nginx 已重载' : ('重载失败：' + (r.output || r.error)), r.ok ? 'ok' : 'err');
  };
  refreshTimer = setInterval(() => { if ((location.hash || '#/overview').startsWith('#/overview')) renderOverview(); }, 15000);
}

async function restartUnit(unit, btn) {
  if (!confirm('重启 ' + unit + '？')) return;
  btn.disabled = true;
  const r = await api('/api/services/' + encodeURIComponent(unit) + '/restart', { method: 'POST' });
  btn.disabled = false;
  toast(r.ok ? unit + ' 已重启' : ('重启失败：' + (r.output || r.error)), r.ok ? 'ok' : 'err');
  if (location.hash.startsWith('#/overview')) renderOverview();
  if (location.hash.startsWith('#/server')) renderServer();
}

/* ------------------------------ 服务器 ------------------------------ */
async function renderServer() {
  stopRefresh();
  $('#main').innerHTML = '<h1>服务器</h1><div class="sub">加载中…</div>';
  const d = await api('/api/overview');
  const fw = await api('/api/firewall');
  const st = await api('/api/settings');
  if (!d.ok) { $('#main').innerHTML = '<h1>服务器</h1><div class="sub">' + esc(d.error) + '</div>'; return; }

  /* 防火墙 */
  let fwHtml;
  if (!fw.installed) {
    fwHtml = '<div class="hint">服务器上没有 ufw（apt install ufw 后刷新）。</div>';
  } else {
    const ruleRows = (fw.rules || []).map(r =>
      '<tr><td class="mono">' + esc(r.rule) + '</td>' +
      '<td><button class="sm danger" data-delrule="' + esc(r.index) + '">删除</button></td></tr>'
    ).join('') || '<tr><td colspan="2" class="hint">没有规则（默认允许出站、拒绝入站的话，SSH 22 必须先放行）</td></tr>';
    fwHtml =
      '<div class="svc"><div class="name">' +
      (fw.active ? '<span class="dot ok"></span>ufw 已启用' : '<span class="dot warn"></span>ufw 未启用') +
      '</div><div class="row">' +
      '<button class="sm" id="fwToggle">' + (fw.active ? '停用防火墙' : '启用防火墙') + '</button>' +
      '<button class="sm primary" id="fwCloudflare">放行 Cloudflare (80/443)</button>' +
      '</div></div>' +
      '<table style="margin-top:10px"><thead><tr><th>规则</th><th></th></tr></thead><tbody>' + ruleRows + '</tbody></table>' +
      '<div class="row" style="margin-top:10px">' +
      '<input type="text" id="fwPort" placeholder="端口，如 22" style="width:110px">' +
      '<select id="fwProto"><option value="tcp">tcp</option><option value="udp">udp</option></select>' +
      '<input type="text" id="fwCidr" placeholder="来源 IP/CIDR（可空）" style="flex:1;min-width:180px">' +
      '<button class="sm primary" id="fwAdd">添加规则</button></div>' +
      '<div class="hint">「放行 Cloudflare」会先确保 22/tcp 放行，再为 15 个 CF 网段加 80/443 入站规则；配合默认拒绝，即可锁死源站。</div>';
  }

  $('#main').innerHTML = '<h1>服务器</h1><div class="sub">nginx · 防火墙 · 发现</div>' +
    '<div class="grid">' +
    '<div class="card"><h3>NGINX</h3><div class="svc"><div class="name">' +
    (d.nginx && d.nginx.active ? '<span class="dot ok"></span>运行中' : '<span class="dot err"></span>未运行') +
    '</div><button class="sm" id="nginxReload">重载配置</button></div>' +
    '<div class="meta"><span>版本 <b>' + esc(d.nginx && d.nginx.version ? d.nginx.version : '—') + '</b></span>' +
    '<span>应用 <b>' + (d.apps || []).length + '</b> 个</span>' +
    '<span>证书 <b>' + (d.certs || []).length + '</b> 张</span></div></div>' +
    '<div class="card"><h3>入口</h3><div class="meta"><span>站点、证书、服务都在「站点」页按站点管理</span></div>' +
    '<div class="row" style="margin-top:8px"><a class="btn sm" href="#/sites">去站点配置</a></div></div>' +
    '</div>' +
    '<div class="card" style="margin-top:14px"><h3>防火墙（ufw）</h3>' + fwHtml + '</div>' +
    '<div class="card" style="margin-top:14px"><h3>设置</h3><div class="row">' +
    '<span class="hint">上传上限（MB）</span>' +
    '<input type="text" id="setUpload" value="' + esc(st.max_upload_mb) + '" style="width:100px">' +
    '<button class="sm primary" id="saveSettings">保存</button></div>' +
    '<div class="hint">部署包和源码包共用这个上限，保存后立即生效。</div></div>' +

    '<div class="card" style="margin-top:14px"><h3>发现</h3>' +
    '<div class="row"><button class="sm primary" id="scanBtn">扫描本机</button>' +
    '<span class="hint">站点 / 运行中的 /var/www 服务 / 证书，可一键纳管</span></div>' +
    '<div id="scanOut" style="margin-top:10px"></div></div>';
  const nr2 = $('#nginxReload');
  if (nr2) nr2.onclick = async () => {
    const r = await api('/api/nginx/reload', { method: 'POST' });
    toast(r.ok ? 'nginx 已重载' : ('重载失败：' + (r.output || r.error)), r.ok ? 'ok' : 'err');
  };

  /* 防火墙 handlers */
  const tg = $('#fwToggle');
  if (tg) tg.onclick = async () => {
    const enable = !fw.active;
    if (enable && !confirm('启用 ufw？会自动放行 22/tcp 防止 SSH 被断。')) return;
    const r = await api('/api/firewall/toggle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active: enable }) });
    toast(r.ok ? r.message : (r.error || r.message), r.ok ? 'ok' : 'err');
    renderServer();
  };
  const cfBtn = $('#fwCloudflare');
  if (cfBtn) cfBtn.onclick = async () => {
    if (!confirm('为 15 个 Cloudflare 网段添加 80/443 入站规则？')) return;
    const r = await api('/api/firewall/cloudflare', { method: 'POST' });
    toast(r.ok ? r.message : (r.error || r.message), r.ok ? 'ok' : 'err');
    renderServer();
  };
  const fwAdd = $('#fwAdd');
  if (fwAdd) fwAdd.onclick = async () => {
    const r = await api('/api/firewall/add', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port: $('#fwPort').value.trim(), proto: $('#fwProto').value, cidr: $('#fwCidr').value.trim() }) });
    toast(r.ok ? r.message : (r.error || r.message), r.ok ? 'ok' : 'err');
    if (r.ok) renderServer();
  };
  $$('[data-delrule]').forEach(b => b.onclick = async () => {
    if (!confirm('删除规则 #' + b.dataset.delrule + '？')) return;
    const r = await api('/api/firewall/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ index: parseInt(b.dataset.delrule, 10) }) });
    toast(r.ok ? r.message : (r.error || r.message), r.ok ? 'ok' : 'err');
    renderServer();
  });

  /* 发现 handlers */
  const saveSet = $('#saveSettings');
  if (saveSet) saveSet.onclick = async () => {
    const r = await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_upload_mb: $('#setUpload').value.trim() }),
    });
    toast(r.ok ? ('已保存，上限 ' + r.max_upload_mb + ' MB') : (r.error || '保存失败'), r.ok ? 'ok' : 'err');
  };

  const scanBtn = $('#scanBtn');
  if (scanBtn) scanBtn.onclick = async () => {
    $('#scanOut').innerHTML = '<div class="hint">扫描中…</div>';
    const dd = await api('/api/discover');
    if (!dd.ok) { $('#scanOut').innerHTML = '<div class="sub">' + esc(dd.error) + '</div>'; return; }
    const siteRows = (dd.sites || []).map(s =>
      '<tr><td class="mono">' + esc(s.name) + '</td><td>' +
      (s.domains || []).map(x => '<span class="badge">' + esc(x) + '</span>').join(' ') + '</td></tr>'
    ).join('');
    const unitRows = (dd.units || []).map(u =>
      '<tr><td class="mono">' + esc(u.unit) + '</td><td class="mono">' + esc(u.path || '—') +
      '</td><td class="mono">' + esc(u.port || '未识别') + '</td>' +
      '<td>' + (u.managed ? '<span class="badge ok">已纳管</span>' : '<span class="badge">未纳管</span>') + '</td>' +
      '<td><button class="sm" data-import="' + esc(u.unit) + '" data-path="' + esc(u.path || '') +
      '" data-port="' + esc(u.port || '') + '" data-managed="' + (u.managed ? '1' : '0') +
      '" data-appid="' + esc(u.app_id || '') + '">' + (u.managed ? '更新端口' : '加入面板') + '</button></td></tr>'
    ).join('') || '<tr><td colspan="5" class="hint">没有找到 /var/www 下的服务（检查 systemd 的 WorkingDirectory 或 ExecStart）</td></tr>';
    const leRows = (dd.letsencrypt || []).map(c =>
      '<tr><td class="mono">' + esc(c.name) + '</td><td class="mono">' + esc(c.not_after || '—') + '</td>' +
      '<td>' + esc(c.days_left) + ' 天</td></tr>'
    ).join('');
    $('#scanOut').innerHTML =
      '<div class="hint">站点（' + (dd.sites || []).length + '）</div>' +
      '<table><thead><tr><th>文件</th><th>域名</th></tr></thead><tbody>' + siteRows + '</tbody></table>' +
      '<div class="hint" style="margin-top:8px">运行中的服务</div>' +
      '<table><thead><tr><th>Unit</th><th>目录</th><th>端口</th><th>状态</th><th></th></tr></thead><tbody>' + unitRows + '</tbody></table>' +
      (leRows ? '<div class="hint" style="margin-top:8px">Let\'s Encrypt</div><table><thead><tr><th>域名</th><th>到期</th><th>剩余</th></tr></thead><tbody>' + leRows + '</tbody></table>' : '');
    $$('[data-import]').forEach(b => b.onclick = async () => {
      const unit = b.dataset.import;
      const managed = b.dataset.managed === '1';
      const def = unit.replace(/\.service$/, '').replace(/[^a-z0-9-]/g, '-');
      const id = managed ? b.dataset.appid : prompt('应用 ID（小写字母/数字/横线）', def);
      if (!id) return;
      const name = managed ? id : (prompt('显示名称', id) || id);
      const port = prompt('监听端口（站点靠它关联服务）', b.dataset.port || '8080');
      if (port === null) return;
      b.disabled = true;
      const r = await api('/api/apps/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id, name: name, unit: unit, path: b.dataset.path, port: port }),
      });
      b.disabled = false;
      toast(r.ok ? r.message : r.error, r.ok ? 'ok' : 'err');
      if (r.ok) renderServer();
    });
  };
}

/* ------------------------------ 站点 ------------------------------ */
async function renderSites() {
  stopRefresh();
  const d = await api('/api/sites');
  if (!d.ok) { $('#main').innerHTML = '<h1>站点</h1><div class="sub">' + esc(d.error) + '</div>'; return; }
  const cards = (d.sites || []).map(s => {
    const doms = (s.domains || []).map(x => '<span class="badge">' + esc(x) + '</span>').join(' ') || '<span class="hint">—</span>';
    const target = (s.upstreams && s.upstreams.length) ? s.upstreams.join(', ')
      : ((s.roots && s.roots.length) ? s.roots.join(', ') : '—');

    let svc = '<span class="hint">静态站，无 systemd</span>';
    if (s.app) {
      svc = '<span class="mono">' + (s.app.active ? '<span class="dot ok"></span>' : '<span class="dot err"></span>') +
        esc(s.app.unit) + '</span>' +
        '<span style="margin-left:8px;white-space:nowrap">' +
        '<button class="sm ghost" data-restart="' + esc(s.app.unit) + '">重启</button> ' +
        '<button class="sm ghost" data-logs="' + esc(s.app.unit) + '">日志</button></span>';
    }

    let cert = '<span class="hint">未配置 HTTPS</span>';
    if (s.cert) {
      const days = s.cert.days_left;
      const cls = days == null ? '' : days < 14 ? 'err' : days < 30 ? 'warn' : 'ok';
      cert = '<span class="mono">' + esc(s.cert.name) + '</span> ' +
        '<span class="badge ' + cls + '">' + (days == null ? '路径未匹配' : days + ' 天') + '</span>';
    }

    let dep = '<span class="hint">未部署</span>';
    if (s.app) {
      const rel = s.last_release;
      dep = (rel
        ? '<span class="badge ' + (rel.status === 'ok' ? 'ok' : 'err') + '">' + esc(rel.id) + '</span>'
        : '<span class="hint">还没有发布记录</span>') +
        '<a class="btn sm" style="margin-left:8px" href="#/deploy?app=' + esc(s.app.id) + '">去部署</a>';
    }

    return '<div class="card site-card">' +
      '<div class="svc"><div style="min-width:0"><div class="name mono">' + esc(s.name) + '</div>' +
      '<div class="row" style="margin-top:6px">' + doms + '</div></div>' +
      '<span class="badge ' + (s.enabled ? 'ok' : '') + '">' + (s.enabled ? '启用' : '停用') + '</span></div>' +
      '<div class="site-line"><span class="k">服务</span><span class="v">' + svc + '</span></div>' +
      '<div class="site-line"><span class="k">证书</span><span class="v">' + cert + '</span></div>' +
      '<div class="site-line"><span class="k">部署</span><span class="v">' + dep + '</span></div>' +
      '<div class="site-line"><span class="k">入口</span><span class="v mono hint">' + esc(target) + '</span></div>' +
      '<div class="toolbar" style="margin:12px 0 0">' +
      '<button class="sm" data-edit="' + esc(s.name) + '">配置</button>' +
      '<button class="sm ghost" data-toggle="' + esc(s.name) + '" data-on="' + (s.enabled ? '1' : '0') + '">' +
      (s.enabled ? '停用' : '启用') + '</button>' +
      '<span class="grow"></span>' +
      '<button class="sm danger" data-del="' + esc(s.name) + '" data-hasapp="' + (s.app ? '1' : '0') + '">删除</button>' +
      '</div></div>';
  }).join('') || '<div class="card"><div class="hint">还没有站点，点上面「新建站点」开始</div></div>';

  $('#main').innerHTML = '<h1>站点</h1><div class="sub">server 块 · 新建/编辑都会先 nginx -t，失败自动还原</div>' +
    '<div class="toolbar"><button class="primary" id="newSite">+ 新建站点</button>' +
    '<span class="grow"></span><button class="ghost sm" id="refreshSites">刷新</button></div>' +
    '<div class="card" id="siteForm" style="display:none;margin-bottom:14px">' +
    '<h3>新建站点</h3>' +
    '<div class="row" style="margin-bottom:10px">' +
    '<input type="text" id="fName" placeholder="站点名，如 blog.conf" style="width:180px">' +
    '<select id="fMode"><option value="proxy">反向代理</option><option value="static">静态目录</option></select>' +
    '<input type="text" id="fTarget" placeholder="端口 8080 或根目录 /var/www/site" style="flex:1;min-width:220px">' +
    '</div>' +
    '<div class="hint">域名一行一个，或用空格/逗号分隔，支持多个（www 与裸域）。</div>' +
    '<textarea class="editor tall" id="fDomains" placeholder="example.com\nwww.example.com"></textarea>' +
    '<div class="row" style="margin:10px 0">' +
    '<input type="text" id="fPath" placeholder="部署路径（默认 /var/www/站点名）" style="flex:1;min-width:240px">' +
    '<select id="fCert"><option value="">不使用 HTTPS</option></select>' +
    '</div>' +
    '<div class="row" style="margin-bottom:10px">' +
    '<input type="text" id="fCommand" placeholder="启动命令（端口没有服务时必填，自动建 systemd 单元）" style="flex:1;min-width:280px">' +
    '</div>' +
    '<div class="row" style="margin-bottom:10px">' +
    '<input type="file" id="fFile" accept=".zip,.tar,.gz,.tgz,.bz2,.xz">' +
    '<span class="hint">可选：上传源码包（zip/tar.gz），自动解压到部署路径（上限见 config 的 max_upload_mb，默认 300MB）</span>' +
    '</div>' +
    '<div class="toolbar"><button class="primary" id="createSite">创建站点</button>' +
    '<button class="ghost" id="cancelSite">取消</button></div></div>' +
    '<div class="site-grid">' + cards + '</div>';

  $('#refreshSites').onclick = () => renderSites();
  $('#newSite').onclick = async () => {
    $('#siteForm').style.display = 'block';
    $('#fName').focus();
    const cd = await api('/api/certs');
    const sel = $('#fCert');
    sel.innerHTML = '<option value="">不使用 HTTPS</option>' +
      (cd.certs || []).map(c => '<option value="' + esc(c.name) + '">' + esc(c.name) +
        (c.days_left != null ? '（剩 ' + c.days_left + ' 天）' : '') + '</option>').join('');
  };
  $('#cancelSite').onclick = () => { $('#siteForm').style.display = 'none'; };
  $('#createSite').onclick = async () => {
    const mode = $('#fMode').value;
    const target = $('#fTarget').value.trim();
    const params = new URLSearchParams({
      name: $('#fName').value.trim(),
      domains: $('#fDomains').value.trim(),
      mode: mode,
      path: $('#fPath').value.trim(),
      command: $('#fCommand').value.trim(),
      cert: $('#fCert').value,
    });
    if (mode === 'proxy') params.set('port', target);
    const fileInput = $('#fFile');
    const file = fileInput && fileInput.files[0];
    const btn = $('#createSite'); btn.disabled = true;
    try {
      const res = await fetch('/api/sites/wizard?' + params.toString(), {
        method: 'POST',
        headers: { 'X-Ops': '1', 'Content-Type': 'application/octet-stream' },
        body: file || undefined,
      });
      const r = await res.json().catch(() => ({ ok: false, error: '响应解析失败' }));
      toast(r.ok ? ('已创建 ' + r.name + (r.app ? '，服务 ' + r.app : '')) : r.error, r.ok ? 'ok' : 'err');
      if (r.ok) renderSites();
    } catch (e) {
      toast('上传失败：' + e.message, 'err');
    } finally {
      btn.disabled = false;
    }
  };
  $$('[data-edit]').forEach(b => b.onclick = () => { location.hash = '#/site?name=' + encodeURIComponent(b.dataset.edit); });
  $$('[data-toggle]').forEach(b => b.onclick = async () => {
    const enable = b.dataset.on !== '1';
    b.disabled = true;
    const r = await api('/api/sites/' + encodeURIComponent(b.dataset.toggle) + '/toggle',
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: enable }) });
    b.disabled = false;
    toast(r.ok ? r.message : r.error, r.ok ? 'ok' : 'err');
    renderSites();
  });
  $$('[data-del]').forEach(b => b.onclick = async () => {
    const name = b.dataset.del;
    if (!confirm('删除站点 ' + name + '？\nnginx 配置会先备份，可以恢复。')) return;
    const delApp = b.dataset.hasapp === '1'
      ? confirm('是否同时删除关联的 systemd 服务？\n\n确定 = 停止并删除单元文件\n取消 = 保留服务')
      : false;
    let delFiles = confirm('是否同时删除网站目录？\n\n确定 = 删除目录（不可恢复）\n取消 = 保留文件');
    let confirmName = '';
    if (delFiles) {
      confirmName = prompt('输入站点名确认删除目录：' + name, '') || '';
      if (confirmName !== name) {
        toast('名称不匹配，已取消', 'err');
        return;
      }
    }
    b.disabled = true;
    const qs = '?app=' + (delApp ? '1' : '0') + '&files=' + (delFiles ? '1' : '0') +
      '&confirm=' + encodeURIComponent(confirmName);
    const r = await api('/api/sites/' + encodeURIComponent(name) + qs, { method: 'DELETE' });
    b.disabled = false;
    toast(r.ok ? r.message : (r.error || r.message), r.ok ? 'ok' : 'err');
    renderSites();
  });
}

async function renderSiteEditor(name) {
  stopRefresh();
  const d = await api('/api/sites/' + encodeURIComponent(name));
  const cd = await api('/api/certs');
  if (!d.ok) { $('#main').innerHTML = '<h1>站点配置</h1><div class="sub">' + esc(d.error) + '</div>'; return; }

  const isProxy = (d.upstreams || []).length > 0;
  const target = isProxy ? String((d.upstreams[0] || '').split(':').pop()) : ((d.roots || [])[0] || '');
  const curCertPath = (d.certs || [])[0] || '';
  const curCert = (cd.certs || []).find(c => c.path === curCertPath);
  const certOpts = '<option value="">不使用 HTTPS</option>' + (cd.certs || []).map(c =>
    '<option value="' + esc(c.name) + '"' + (curCert && curCert.name === c.name ? ' selected' : '') + '>' +
    esc(c.name) + (c.days_left != null ? '（剩 ' + c.days_left + ' 天）' : '') + '</option>').join('');

  let appHtml = '<div class="hint">未关联服务（静态站）</div>';
  if (d.app) {
    appHtml = '<div class="row"><div><div class="mono">' + esc(d.app.unit) + '</div>' +
      '<div class="hint">' + esc(d.app.path || '') + '</div></div><span class="grow"></span>' +
      '<button class="sm" id="eRestart">重启</button>' +
      '<button class="sm ghost" id="eLogs">日志</button>' +
      '<a class="btn sm" href="#/deploy?app=' + esc(d.app.id) + '">部署</a></div>';
  }

  $('#main').innerHTML = '<h1>站点配置</h1><div class="sub mono">' + esc(name) + ' · ' + esc(d.path) + '</div>' +
    '<div class="card"><h3>基本</h3>' +
    '<div class="hint">域名一行一个或空格分隔；保存时自动生成 server 块。</div>' +
    '<textarea class="editor tall" id="eDomains"></textarea>' +
    '<div class="row" style="margin-top:10px">' +
    '<select id="eMode"><option value="proxy"' + (isProxy ? ' selected' : '') + '>反向代理</option>' +
    '<option value="static"' + (isProxy ? '' : ' selected') + '>静态目录</option></select>' +
    '<input type="text" id="eTarget" style="flex:1;min-width:240px" placeholder="端口或根目录" value="' + esc(target) + '">' +
    '<select id="eCert">' + certOpts + '</select>' +
    '</div></div>' +
    '<div class="card" style="margin-top:14px"><h3>配置预览（可直接改）</h3>' +
    '<textarea class="editor" id="editor" spellcheck="false"></textarea>' +
    '<div class="toolbar"><button id="gen">按参数生成</button>' +
    '<button class="primary" id="save">保存并应用</button>' +
    '<button id="verify">仅校验</button>' +
    '<button class="ghost" id="back">返回站点</button></div></div>' +
    '<div class="card" style="margin-top:14px"><h3>关联服务</h3>' + appHtml + '</div>' +
    '<div class="card" style="margin-top:14px"><h3>证书</h3>' +
    '<div class="hint">本页上传的证书会出现在上面的下拉框里。</div>' +
    '<div class="row"><button class="sm" id="showCert">+ 上传证书</button></div>' +
    '<div id="certForm" style="display:none;margin-top:10px"><div class="col">' +
    '<input type="text" id="certname" placeholder="名称，如 example.com">' +
    '<textarea class="editor tall" id="certpem" placeholder="-----BEGIN CERTIFICATE-----"></textarea>' +
    '<textarea class="editor tall" id="certkey" placeholder="-----BEGIN PRIVATE KEY-----"></textarea>' +
    '<div class="row"><button class="primary sm" id="uploadcert">保存证书</button>' +
    '<span class="hint">校验证书与私钥是否配对</span></div></div></div></div>';

  $('#eDomains').value = (d.domains || []).join('\n');
  const ed = $('#editor');
  ed.value = d.content;
  ed.addEventListener('keydown', e => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const s = ed.selectionStart, epos = ed.selectionEnd;
      ed.value = ed.value.slice(0, s) + '    ' + ed.value.slice(epos);
      ed.selectionStart = ed.selectionEnd = s + 4;
    }
  });
  $('#back').onclick = () => { location.hash = '#/sites'; };
  $('#gen').onclick = async () => {
    const r = await api('/api/sites/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name,
        domains: $('#eDomains').value,
        mode: $('#eMode').value,
        target: $('#eTarget').value.trim(),
        cert: $('#eCert').value,
      }),
    });
    if (r.ok) { ed.value = r.content; toast('已按参数生成，确认后保存', 'ok'); }
    else toast(r.error, 'err');
  };
  $('#verify').onclick = async () => {
    const r = await api('/api/nginx/test', { method: 'POST' });
    toast(r.output || (r.ok ? '语法 OK' : '校验失败'), r.ok ? 'ok' : 'err');
  };
  $('#save').onclick = async () => {
    if (!confirm('保存并重载 nginx？')) return;
    const btn = $('#save'); btn.disabled = true; btn.textContent = '保存中…';
    const r = await api('/api/sites/' + encodeURIComponent(name), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: ed.value }),
    });
    btn.disabled = false; btn.textContent = '保存并应用';
    toast(r.message || r.error, r.ok ? 'ok' : 'err');
    if (!r.ok) {
      const again = await api('/api/sites/' + encodeURIComponent(name));
      if (again.ok) ed.value = again.content;
    }
  };
  const rs = $('#eRestart');
  if (rs) rs.onclick = async () => {
    if (!confirm('重启 ' + d.app.unit + '？')) return;
    rs.disabled = true;
    const r = await api('/api/services/' + encodeURIComponent(d.app.unit) + '/restart', { method: 'POST' });
    rs.disabled = false;
    toast(r.ok ? '已重启' : ('失败：' + (r.output || r.error)), r.ok ? 'ok' : 'err');
  };
  const lg = $('#eLogs');
  if (lg) lg.onclick = () => { location.hash = '#/logs?unit=' + encodeURIComponent(d.app.unit); };
  $('#showCert').onclick = () => { $('#certForm').style.display = 'block'; };
  $('#uploadcert').onclick = async () => {
    const r = await api('/api/certs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: $('#certname').value.trim(), cert: $('#certpem').value, key: $('#certkey').value }),
    });
    toast(r.ok ? '证书已保存' : r.error, r.ok ? 'ok' : 'err');
    if (r.ok) renderSiteEditor(name);
  };
}

/* ------------------------------ 部署 ------------------------------ */
async function renderDeploy(params) {
  stopRefresh();
  const ov = await api('/api/overview');
  if (!ov.ok) return;
  const filter = params && params.get('app');
  const apps = (ov.apps || []).filter(a => !filter || a.id === filter);
  const cards = [];
  for (const app of apps) {
    const rel = await api('/api/releases/' + encodeURIComponent(app.id));
    const rows = (rel.releases || []).map(r =>
      '<tr><td class="mono">' + esc(r.id) + '</td>' +
      '<td>' + (r.status === 'ok' ? '<span class="badge ok">成功</span>' : '<span class="badge err">失败</span>') + '</td>' +
      '<td class="hint">' + esc(r.note || '') + '</td>' +
      '<td>' + (r.available ? '<button class="sm" data-rollback="' + esc(app.id) + '|' + esc(r.id) + '">回滚</button>' : '<span class="hint">无快照</span>') + '</td></tr>'
    ).join('') || '<tr><td colspan="4" class="hint">还没有部署记录</td></tr>';
    cards.push('<div class="card" style="margin-bottom:14px"><div class="svc"><div>' +
      '<div class="name">' + esc(app.name) + '</div><div class="unit">' + esc(app.path) + '</div></div>' +
      '<div class="row"><input type="file" accept=".tgz,.tar.gz" data-file="' + esc(app.id) + '">' +
      '<button class="primary sm" data-deploy="' + esc(app.id) + '">部署</button></div></div>' +
      '<div class="hint">流程：上传 → 解包 → 备份当前版本 → rsync 覆盖（排除 data/）→ 改属主 → 重启 → 健康检查；失败自动回滚。</div>' +
      '<table><thead><tr><th>版本</th><th>结果</th><th>说明</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>');
  }
  $('#main').innerHTML = '<h1>部署</h1><div class="sub">上传 release 压缩包（Mac 上运行 deploy.sh 生成）' +
    (filter ? ' · <a href="#/sites">返回站点</a>' : '') + '</div>' + cards.join('');

  $$('[data-deploy]').forEach(b => b.onclick = async () => {
    const id = b.dataset.deploy;
    const input = $('[data-file="' + id + '"]');
    const file = input && input.files[0];
    if (!file) { toast('先选一个 .tgz 文件', 'err'); return; }
    if (!confirm('部署 ' + file.name + ' 到 ' + id + '？')) return;
    b.disabled = true; b.textContent = '部署中…';
    try {
      const res = await fetch('/api/deploy/' + encodeURIComponent(id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file,
      });
      const data = await res.json().catch(() => ({ ok: false, error: '响应解析失败' }));
      toast(data.ok ? ('部署成功：' + (data.message || '')) : ('部署失败：' + (data.error || '')), data.ok ? 'ok' : 'err');
    } catch (e) {
      toast('上传失败：' + e.message, 'err');
    }
    renderDeploy();
  });
  $$('[data-rollback]').forEach(b => b.onclick = async () => {
    const [app, rel] = b.dataset.rollback.split('|');
    if (!confirm('回滚到 ' + rel + '？')) return;
    b.disabled = true;
    const r = await api('/api/rollback/' + encodeURIComponent(app) + '/' + encodeURIComponent(rel), { method: 'POST' });
    toast(r.ok ? r.message : ('回滚失败：' + r.error), r.ok ? 'ok' : 'err');
    renderDeploy();
  });
}

/* ------------------------------ 告警 ------------------------------ */
async function renderAlerts() {
  stopRefresh();
  const d = await api('/api/monitor');
  if (!d.ok) return;
  const n = d.notify || {};
  const th = n.thresholds || {};
  const svc = d.services || {};
  const events = (d.events || []).slice(0, 80).map(e => {
    const cls = e.level === 'alert' ? 'err' : e.level === 'error' ? 'err' : e.level === 'daily' ? 'ok' : 'ok';
    return '<tr><td class="mono">' + esc(e.ts) + '</td>' +
      '<td><span class="badge ' + cls + '">' + esc(e.kind) + '</span></td>' +
      '<td>' + esc(e.title) +
      (e.error ? '<div class="hint">推送失败：' + esc(e.error) + '</div>' : '') +
      (e.detail ? '<div class="hint">' + esc(String(e.detail).split('\n')[0]) + '</div>' : '') +
      '</td>' +
      '<td>' + (e.sent === true ? '<span class="badge ok">已推送</span>' : (e.sent === false ? '<span class="badge err">失败</span>' : '<span class="hint">—</span>')) + '</td></tr>';
  }).join('') || '<tr><td colspan="4" class="hint">还没有事件</td></tr>';

  const svcRows = Object.keys(svc).map(k => {
    const s = svc[k];
    return '<tr><td class="mono">' + esc(k) + '</td><td>' + (s.ok ? '<span class="badge ok">正常</span>' : '<span class="badge err">异常</span>') +
      '</td><td>' + esc(s.code || '—') + '</td><td>' + esc(s.ms != null ? s.ms + ' ms' : '—') + '</td>' +
      '<td class="hint">' + esc(s.error || '') + '</td></tr>';
  }).join('');

  $('#main').innerHTML = '<h1>告警</h1><div class="sub">推送到 RemindHub · 异常即时推送，每日汇总走日报</div>' +
    '<div class="grid">' +
    '<div class="card"><h3>推送配置</h3>' +
    '<div class="col">' +
    '<label class="row"><input type="checkbox" id="aEnabled" ' + (n.enabled ? 'checked' : '') + '> 启用推送</label>' +
    '<input type="text" id="aBase" placeholder="http://10.0.0.10:9095" value="' + esc(n.base_url || '') + '">' +
    '<input type="password" id="aToken" placeholder="' + (n.has_token ? 'Token 已保存，留空不改' : 'App Token') + '">' +
    '<input type="text" id="aChannels" placeholder="通道（留空=发给全部通道）" value="' + esc((n.channels || []).join(',')) + '">' +
    '<div class="row"><input type="text" id="aDaily" placeholder="09:00" style="width:90px" value="' + esc(n.daily_at || '09:00') + '">' +
    '<input type="text" id="aHeartbeat" placeholder="心跳巡检名（可空）" value="' + esc(n.heartbeat_name || '') + '"></div>' +
    '</div></div>' +
    '<div class="card"><h3>阈值</h3><div class="col">' +
    '<div class="row"><span class="hint" style="width:110px">磁盘 %</span><input type="text" id="tDisk" value="' + esc(th.disk_percent) + '" style="width:80px"></div>' +
    '<div class="row"><span class="hint" style="width:110px">证书剩余天</span><input type="text" id="tCert" value="' + esc(th.cert_days) + '" style="width:80px"></div>' +
    '<div class="row"><span class="hint" style="width:110px">连续失败次数</span><input type="text" id="tFail" value="' + esc(th.http_failures) + '" style="width:80px"></div>' +
    '<div class="row"><span class="hint" style="width:110px">巡检间隔秒</span><input type="text" id="tInterval" value="' + esc(th.check_interval) + '" style="width:80px"></div>' +
    '<div class="row"><span class="hint" style="width:110px">负载倍数</span><input type="text" id="tLoad" value="' + esc(th.load_ratio) + '" style="width:80px"></div>' +
    '<div class="row"><span class="hint" style="width:110px">I/O 等待 %</span><input type="text" id="tIo" value="' + esc(th.iowait_percent) + '" style="width:80px"></div>' +
    '<div class="row"><span class="hint" style="width:110px">内存 %</span><input type="text" id="tMem" value="' + esc(th.mem_percent) + '" style="width:80px"></div>' +
    '<div class="toolbar"><button class="primary" id="saveNotify">保存</button>' +
    '<button class="ghost" id="testNotify">测试推送</button></div></div></div></div>' +
    '<div class="card" style="margin-top:14px"><h3>当前状态</h3><table><thead><tr><th>服务</th><th>状态</th><th>HTTP</th><th>响应</th><th>原因</th></tr></thead><tbody>' +
    (svcRows || '<tr><td colspan="5" class="hint">等待第一轮巡检</td></tr>') + '</tbody></table>' +
    '<div class="hint">最后巡检：' + esc(d.updated || '—') + '</div></div>' +
    '<div class="card" style="margin-top:14px"><h3>事件</h3><table><thead><tr><th>时间</th><th>类型</th><th>标题</th><th>推送</th></tr></thead><tbody>' +
    events + '</tbody></table></div>';

  $('#saveNotify').onclick = async () => {
    const body = {
      enabled: $('#aEnabled').checked,
      base_url: $('#aBase').value.trim(),
      daily_at: $('#aDaily').value.trim() || '09:00',
      heartbeat_name: $('#aHeartbeat').value.trim(),
      channels: $('#aChannels').value.split(',').map(x => x.trim()).filter(Boolean),
      thresholds: {
        disk_percent: $('#tDisk').value, cert_days: $('#tCert').value,
        http_failures: $('#tFail').value, check_interval: $('#tInterval').value,
        load_ratio: $('#tLoad').value, mem_percent: $('#tMem').value,
        iowait_percent: $('#tIo').value,
      },
    };
    const token = $('#aToken').value.trim();
    if (token) body.token = token;
    const r = await api('/api/monitor/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(r.ok ? '已保存' : (r.error || '保存失败'), r.ok ? 'ok' : 'err');
    if (r.ok) renderAlerts();
  };
  $('#testNotify').onclick = async () => {
    const r = await api('/api/monitor/test', { method: 'POST' });
    toast(r.ok ? ('测试已发送：' + r.message) : ('发送失败：' + (r.message || r.error)), r.ok ? 'ok' : 'err');
  };
}

/* ------------------------------ 安全 ------------------------------ */
async function renderSecurity() {
  stopRefresh();
  const sec = await api('/api/security');
  const st = await api('/api/settings');
  const fb = await api('/api/fail2ban');
  const ver = sec.version || st.version || '';

  const tfa = sec.enabled
    ? '<div class="svc"><div class="name"><span class="dot ok"></span>已开启</div>' +
      '<button class="sm danger" id="disable2fa">关闭</button></div>' +
      '<div class="hint">恢复码剩余 ' + esc(sec.recovery_left) + ' 个。公网访问时请保持开启。</div>'
    : '<div class="hint">开启后登录需要「密码 + 6 位动态码」。如果要让面板直接暴露公网，这一项必须开。</div>' +
      '<div class="row"><button class="sm primary" id="enable2fa">启用两步验证</button></div>' +
      '<div id="tfaBox" style="display:none;margin-top:10px">' +
      '<div class="hint">用 Google Authenticator / 1Password / 微软验证器扫这个二维码：</div>' +
      '<div id="tfaQr" class="qrbox"></div>' +
      '<div class="hint">扫不了就手动输入下面的密钥：</div>' +
      '<pre class="log" id="tfaSecret" style="height:auto;max-height:80px"></pre>' +
      '<div class="hint mono" id="tfaUri" style="word-break:break-all"></div>' +
      '<div class="row" style="margin-top:8px"><input type="text" id="tfaCode" placeholder="6 位验证码" style="width:150px" inputmode="numeric">' +
      '<button class="sm primary" id="confirm2fa">确认启用</button></div></div>';

  let fbHtml;
  if (!fb.installed) {
    fbHtml = '<div class="hint">未安装 fail2ban。Ubuntu 安装：apt install -y fail2ban</div>';
  } else if (!fb.running) {
    fbHtml = '<div class="hint">fail2ban 未运行或 sshd jail 未启用</div>' +
      (fb.error ? '<div class="hint">' + esc(fb.error) + '</div>' : '');
  } else {
    const ips = (fb.banned || []).map(ip =>
      '<span class="badge err" style="margin:2px 4px 2px 0">' + esc(ip) +
      ' <button class="sm ghost" style="padding:0 4px" data-unban="' + esc(ip) + '">解封</button></span>'
    ).join('') || '<span class="hint">当前没有封禁 IP</span>';
    const logText = (fb.recent || []).join('\n') || '（暂无 ban/unban 记录）';
    fbHtml =
      '<div class="meta"><span>当前失败 <b>' + esc(fb.currently_failed || 0) + '</b></span>' +
      '<span>累计失败 <b>' + esc(fb.total_failed || 0) + '</b></span>' +
      '<span>当前封禁 <b>' + esc(fb.currently_banned || 0) + '</b></span>' +
      '<span>累计封禁 <b>' + esc(fb.total_banned || 0) + '</b></span>' +
      '<span>jail：' + esc((fb.jails || []).join(', ') || 'sshd') + '</span></div>' +
      '<div class="row" style="margin-top:10px">' + ips + '</div>' +
      '<pre class="log" style="height:220px;margin-top:10px">' + esc(logText) + '</pre>' +
      '<div class="row"><button class="sm ghost" id="fbRefresh">刷新</button></div>';
  }

  $('#main').innerHTML = '<h1>安全</h1><div class="sub">密码 · 两步验证 · 部署令牌' +
    (ver ? ' · v' + esc(ver) : '') + '</div>' +
    '<div class="grid">' +
    '<div class="card"><h3>修改密码</h3><div class="col">' +
    '<input type="password" id="pwCur" placeholder="当前密码" autocomplete="current-password">' +
    '<input type="password" id="pwNew" placeholder="新密码（至少 8 位）" autocomplete="new-password">' +
    '<input type="password" id="pwNew2" placeholder="再输一次新密码" autocomplete="new-password">' +
    '<div class="row"><button class="primary sm" id="savePw">保存</button></div></div></div>' +
    '<div class="card"><h3>部署令牌</h3><div class="mono">' + esc(sec.deploy_token_hint) + '</div>' +
    '<div class="hint">deploy.sh 用它调用部署接口，不是登录密码。泄露后在这里重置，旧令牌立即失效。</div>' +
    '<div class="row"><button class="sm danger" id="rotateToken">重置令牌</button></div></div>' +
    '</div>' +
    '<div class="card" style="margin-top:14px"><h3>两步验证（2FA）</h3>' + tfa + '</div>' +
    '<div class="card" style="margin-top:14px"><h3>IP 封禁（fail2ban）</h3>' + fbHtml + '</div>';

  $('#savePw').onclick = async () => {
    const cur = $('#pwCur').value, np = $('#pwNew').value, np2 = $('#pwNew2').value;
    if (!np || np !== np2) { toast('两次新密码不一致', 'err'); return; }
    const r = await api('/api/security/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current: cur, new: np }),
    });
    if (!r.ok) { toast(r.error || '修改失败', 'err'); return; }
    alert('密码已修改，请重新登录');
    location.href = '/login';
  };
  $('#rotateToken').onclick = async () => {
    const pw = prompt('输入当前密码确认'); if (!pw) return;
    const r = await api('/api/security/deploy-token', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }),
    });
    if (!r.ok) { toast(r.error || '重置失败', 'err'); return; }
    alert('新的部署令牌（只显示一次）：\n\n' + r.token + '\n\n记得更新 deploy.sh 的 OPS_TOKEN');
  };

  $$('[data-unban]').forEach(b => b.onclick = async () => {
    const ip = b.dataset.unban;
    if (!confirm('解封 ' + ip + '？')) return;
    b.disabled = true;
    const r = await api('/api/fail2ban/unban', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip: ip }),
    });
    b.disabled = false;
    toast(r.ok ? '已解封 ' + ip : (r.error || r.message || '解封失败'), r.ok ? 'ok' : 'err');
    if (r.ok) renderSecurity();
  });
  const fbRefresh = $('#fbRefresh');
  if (fbRefresh) fbRefresh.onclick = () => renderSecurity();

  const enable2fa = $('#enable2fa');
  if (enable2fa) enable2fa.onclick = async () => {
    const r = await api('/api/security/setup', { method: 'POST' });
    if (!r.ok) { toast(r.error || '生成失败', 'err'); return; }
    window.__tfaSecret = r.secret;
    $('#tfaSecret').textContent = r.secret;
    $('#tfaUri').textContent = r.uri;
    const qrBox = $('#tfaQr');
    qrBox.innerHTML = '';
    if (window.QRCode) {
      new QRCode(qrBox, { text: r.uri, width: 180, height: 180, correctLevel: QRCode.CorrectLevel.M });
    }
    $('#tfaBox').style.display = 'block';
  };
  const confirm2fa = $('#confirm2fa');
  if (confirm2fa) confirm2fa.onclick = async () => {
    const r = await api('/api/security/enable', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ secret: window.__tfaSecret, code: $('#tfaCode').value.trim() }),
    });
    if (!r.ok) { toast(r.error, 'err'); return; }
    alert('两步验证已开启。请保存下面 8 个恢复码（每个只能用一次）：\n\n' + r.recovery.join('\n'));
    renderSecurity();
  };
  const disable2fa = $('#disable2fa');
  if (disable2fa) disable2fa.onclick = async () => {
    const pw = prompt('输入当前密码'); if (!pw) return;
    const code = prompt('输入当前 6 位动态码（或用恢复码）'); if (!code) return;
    const r = await api('/api/security/disable', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw, code: code }),
    });
    toast(r.ok ? '已关闭两步验证' : (r.error || '失败'), r.ok ? 'ok' : 'err');
    if (r.ok) renderSecurity();
  };
}

/* ------------------------------ 日志 ------------------------------ */
async function renderLogs(params) {
  stopRefresh();
  const unit = params.get('unit') || '';
  const ov = await api('/api/overview');
  const apps = (ov && ov.apps) || [];
  const opts = apps.map(a => '<option value="' + esc(a.unit) + '"' + (a.unit === unit ? ' selected' : '') + '>' +
    esc(a.name + ' (' + a.unit + ')') + '</option>').join('') +
    '<option value="nginx.service"' + (unit === 'nginx.service' ? ' selected' : '') + '>nginx</option>';
  $('#main').innerHTML = '<h1>日志</h1><div class="sub">systemd journal / nginx / 审计</div>' +
    '<div class="card"><div class="row">' +
    '<select id="unit">' + opts + '</select>' +
    '<input type="text" id="lines" value="120" style="width:70px">' +
    '<button class="sm primary" id="load">刷新</button><span class="grow"></span>' +
    '<button class="sm ghost" id="nginxerr">nginx error</button>' +
    '<button class="sm ghost" id="nginxacc">nginx access</button>' +
    '<button class="sm ghost" id="audit">审计</button></div>' +
    '<pre class="log" id="log" style="margin-top:12px"></pre></div>';
  const load = async (kind) => {
    const n = parseInt($('#lines').value || '120', 10) || 120;
    let d;
    if (kind === 'audit') d = await api('/api/audit?lines=' + n);
    else if (kind === 'nginx-error') d = await api('/api/nginx/logs?which=error&lines=' + n);
    else if (kind === 'nginx-access') d = await api('/api/nginx/logs?which=access&lines=' + n);
    else d = await api('/api/logs?unit=' + encodeURIComponent($('#unit').value) + '&lines=' + n);
    $('#log').textContent = d.log || d.error || '（空）';
    $('#log').scrollTop = $('#log').scrollHeight;
  };
  $('#load').onclick = () => load('unit');
  $('#nginxerr').onclick = () => load('nginx-error');
  $('#nginxacc').onclick = () => load('nginx-access');
  $('#audit').onclick = () => load('audit');
  $('#unit').onchange = () => load('unit');
  load('unit');
}

/* ------------------------------ 路由 ------------------------------ */
function parseHash() {
  const raw = location.hash || '#/overview';
  const [path, qs] = raw.slice(1).split('?');
  return { path, params: new URLSearchParams(qs || '') };
}

function setActive(tab) {
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.tab === tab));
}

async function route() {
  const { path, params } = parseHash();
  if (path === '/overview') { setActive('overview'); return renderOverview(); }
  if (path === '/services' || path === '/certs' || path === '/discover') { location.hash = '#/server'; return; }
  if (path === '/sites') { setActive('sites'); return renderSites(); }
  if (path === '/site') { setActive('sites'); return renderSiteEditor(params.get('name') || ''); }
  if (path === '/server') { setActive('server'); return renderServer(); }
  if (path === '/security') { setActive('security'); return renderSecurity(); }
  if (path === '/deploy') { setActive('sites'); return renderDeploy(params); }
  if (path === '/alerts') { setActive('alerts'); return renderAlerts(); }
  if (path === '/logs') { setActive('logs'); return renderLogs(params); }
  location.hash = '#/overview';
}

window.addEventListener('hashchange', route);
window.addEventListener('load', () => {
  api('/api/settings').then(s => {
    if (s && s.ok) { const el = $('#appVer'); if (el) el.textContent = 'v' + s.version; }
  }).catch(() => {});
  const lo = $('#logout');
  if (lo) lo.onclick = async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; };
  route();
});
