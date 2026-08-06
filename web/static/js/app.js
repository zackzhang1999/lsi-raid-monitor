/* LSI RAID Monitor — 原生 JS SPA（google-design 体系） */
/* global Chart */
'use strict';

/* ---------- 工具 ---------- */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

function esc(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toDate(ts) {
  if (ts == null || ts === '') return null;
  let d;
  if (typeof ts === 'number') d = new Date(ts < 1e12 ? ts * 1000 : ts);
  else d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}
function pad(n) { return String(n).padStart(2, '0'); }
function fmtDateTime(ts) {
  const d = toDate(ts);
  if (!d) return '—';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtClock(ms) {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDayClock(ms) {
  const d = new Date(ms);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* ---------- Toast ---------- */
function toast(msg, type) {
  const root = $('#toast-root');
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'info');
  el.innerHTML = `<span class="t-dot"></span><span>${esc(msg)}</span>`;
  root.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3400);
  setTimeout(() => el.remove(), 3800);
}

/* ---------- API ---------- */
async function api(path, opts) {
  const o = Object.assign({ credentials: 'same-origin' }, opts || {});
  if (o.body && typeof o.body !== 'string') {
    o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
    o.body = JSON.stringify(o.body);
  }
  const res = await fetch(path, o);
  if (res.status === 401) {
    if (state.authRequired) showLogin();
    throw new Error('未登录或会话已过期');
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (!res.ok) {
    const msg = data && (data.error || data.message) ? (data.error || data.message) : ('请求失败 (' + res.status + ')');
    if (res.status === 403) toast(msg, 'error');
    throw new Error(msg);
  }
  return data;
}
function btnLoading(btn, on) {
  if (!btn) return;
  btn.classList.toggle('loading', !!on);
  btn.disabled = !!on;
}

/* ---------- 全局状态 ---------- */
const state = {
  me: null,
  authRequired: true,
  isAdmin: false,
  status: null,
  alertCfg: null,
  view: 'overview',
  hours: 24,
  chartType: 'temp',
  evLevel: 'all',
  evPage: 1,
  evTotal: 0,
  evPageSize: 20,
  chart: null,
  currentDisk: null,
  smartData: null,
  tempWarn: 45,
  tempCrit: 55,
  vds: [],
  fsUsage: null,
  ctlLines: 100,
  storageLoaded: false,
  usersLoaded: false,
  nfsLoaded: false,
  refreshTimer: null,
  realtimeTimer: null,
  raidSel: new Set(), // 勾选用于创建阵列的磁盘，键为 "eid:slot"
};

const HEALTH_TEXT = { ok: '正常', warn: '警告', crit: '严重', unknown: '未知' };
const BADGE_OK_STATES = ['onln', 'optl', 'optimal', 'ok', 'online', 'good', 'ugood', 'jbod'];
const BADGE_CRIT_STATES = ['ubad', 'failed', 'fail', 'degraded', 'dead', 'offline', 'offln', 'missing'];

function stateTone(s) {
  const v = String(s || '').toLowerCase();
  if (!v) return 'unknown';
  if (BADGE_CRIT_STATES.some(k => v.includes(k))) return 'crit';
  if (BADGE_OK_STATES.some(k => v.includes(k))) return 'ok';
  if (v.includes('rebuild') || v.includes('copyback') || v.includes('init')) return 'warn';
  return 'unknown';
}
function stateBadge(s) {
  const tone = stateTone(s);
  const cls = tone === 'unknown' ? '' : tone;
  return `<span class="badge ${cls}">${esc(s || '—')}</span>`;
}
function tempTone(t) {
  if (t == null || isNaN(t)) return '';
  if (t >= state.tempCrit) return 'crit';
  if (t >= state.tempWarn) return 'warn';
  return '';
}
function fmtTemp(t) {
  if (t == null || isNaN(t)) return '—';
  const tone = tempTone(Number(t));
  const style = tone === 'crit' ? ' style="color:var(--crit)"' : tone === 'warn' ? ' style="color:var(--warn)"' : '';
  return `<span class="mono"${style}>${Number(t)}°C</span>`;
}
function fmtHours(h) {
  if (h == null || isNaN(h)) return '—';
  const d = Math.floor(Number(h) / 24);
  return d > 0 ? `${d} 天` : `${Number(h)} 小时`;
}
function scoreColor(s) {
  if (s >= 80) return 'var(--ok)';
  if (s >= 60) return 'var(--warn)';
  return 'var(--crit)';
}

/* ---------- 主题 ---------- */
function applyTheme(t, save) {
  document.documentElement.classList.toggle('dark', t === 'dark');
  $('#btn-theme').textContent = t === 'dark' ? '☀' : '◐';
  if (save) localStorage.setItem('lsi-theme', t);
  if (state.chart) loadHistory(); // 重建图表以适配坐标轴颜色
}
function initTheme() {
  const saved = localStorage.getItem('lsi-theme');
  const t = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  applyTheme(t, false);
}

/* ---------- 通用对话框 ---------- */
function showModal(opt) {
  $('#modal-title').textContent = opt.title || '确认';
  const body = $('#modal-body');
  body.innerHTML = '';
  if (typeof opt.body === 'string') body.innerHTML = opt.body;
  else if (opt.body) body.appendChild(opt.body);
  const actions = $('#modal-actions');
  actions.innerHTML = '';
  (opt.actions || []).forEach(a => {
    const b = document.createElement('button');
    b.className = 'btn ' + (a.cls || '');
    b.textContent = a.label;
    b.addEventListener('click', () => a.handler(b));
    actions.appendChild(b);
  });
  $('#modal-scrim').classList.remove('hidden');
}
function closeModal() { $('#modal-scrim').classList.add('hidden'); }
function confirmModal(title, html, okLabel, danger, onOk) {
  showModal({
    title,
    body: html,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: okLabel || '确定', cls: danger ? 'danger' : 'primary',
        handler: async (btn) => {
          btnLoading(btn, true);
          try { await onOk(); closeModal(); }
          catch (e) { toast(e.message, 'error'); }
          finally { btnLoading(btn, false); }
        }
      }
    ]
  });
}

/* ---------- 登录 ---------- */
function showLogin() {
  $('#login-overlay').classList.remove('hidden');
  $('#app').classList.add('hidden');
}
function hideLogin() {
  $('#login-overlay').classList.add('hidden');
  $('#app').classList.remove('hidden');
}

/* ---------- 启动 ---------- */
async function boot() {
  initTheme();
  bindUI();
  try {
    const me = await api('/api/me');
    state.me = me;
    state.authRequired = !!me.auth_required;
    if (me.auth_required && !me.logged_in) { showLogin(); return; }
    await afterLogin();
  } catch (e) {
    toast('无法连接后端：' + e.message, 'error');
  }
}

async function afterLogin() {
  const me = state.me || {};
  state.isAdmin = !me.auth_required || me.role === 'admin';
  hideLogin();
  // 角色相关可见性
  $('#nav-users').classList.toggle('hidden', !state.isAdmin);
  $('#btn-collect').classList.toggle('hidden', !state.isAdmin);
  $('#btn-logout').classList.toggle('hidden', !state.authRequired);
  $('#sel-interval').disabled = !state.isAdmin;
  $('#btn-alert-save').disabled = !state.isAdmin;
  $('#btn-alert-test').disabled = !state.isAdmin;
  const name = me.username || 'admin';
  $('#user-name').textContent = name + (state.authRequired ? '' : '（未认证）');
  $('#user-avatar').textContent = (name[0] || '?');
  await loadAll();
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => loadStatus().catch(() => {}), 60000);
  if (state.realtimeTimer) clearInterval(state.realtimeTimer);
  state.realtimeTimer = setInterval(() => {
    if (!document.hidden) loadRealtime().catch(() => {});
  }, 5000);
}

async function loadAll() {
  await Promise.allSettled([
    loadStatus(), loadAlertConfig(), loadCollectionConfig(), loadHistory(),
    loadEvents(), loadRealtime(), loadFsUsage(), loadVdDetail(), loadCtlEvents(),
  ]);
}

/* ---------- 状态数据 ---------- */
async function loadStatus() {
  const st = await api('/api/status');
  state.status = st;
  renderTopbar(st);
  renderHealth(st);
  renderStatCards(st);
  renderTopology(st);
  renderMaintenance(st);
  renderPhysicalDisks(st);
  renderSystem(st);
}

function renderTopbar(st) {
  $('#tb-host').textContent = st.host || '—';
  const h = st.health || 'unknown';
  $('#tb-health').className = 'badge ' + (h === 'unknown' ? '' : h);
  $('#tb-health').textContent = HEALTH_TEXT[h] || h;
  const up = $('#tb-updated');
  if (st.timestamp) {
    const d = toDate(st.timestamp);
    const intervalMin = Number($('#sel-interval').value) || 1;
    const stale = d && (Date.now() - d.getTime()) > (intervalMin * 2 + 1) * 60000;
    up.textContent = '更新于 ' + fmtDateTime(st.timestamp) + (stale ? '（数据过期，采集中断）' : '');
    up.style.color = stale ? 'var(--crit)' : '';
  } else {
    up.textContent = '';
    up.style.color = '';
  }
}

/* ---------- 综合健康评分 ---------- */
function computeHealth(st) {
  const c = st.controller || {};
  const disks = st.physical_disks || [];
  const vds = st.virtual_disks || [];

  // 控制器 + 虚拟盘
  let ctrl = 100;
  const ch = stateTone(c.health);
  if (ch === 'crit') ctrl = 20; else if (ch === 'warn' || ch === 'unknown') ctrl = 60;
  if (vds.some(v => stateTone(v.state) === 'crit')) ctrl = Math.min(ctrl, 30);
  else if (vds.some(v => stateTone(v.state) === 'warn')) ctrl = Math.min(ctrl, 70);

  // BBU
  let bbu = 100;
  const bs = String(c.bbu_state || '').toLowerCase();
  if (!c.bbu_model && !c.bbu_state) bbu = 80;
  else if (bs && !/ok|optimal|good|normal/.test(bs)) bbu = 40;
  if (c.bbu_temperature != null && c.bbu_temperature >= 60) bbu = Math.min(bbu, 40);

  // 温度
  const temps = disks.map(d => d.temperature).filter(t => t != null && !isNaN(t));
  const maxT = temps.length ? Math.max.apply(null, temps) : null;
  let temp = 100;
  if (maxT != null) {
    if (maxT >= state.tempCrit) temp = 10;
    else if (maxT >= state.tempWarn) temp = 55;
    else temp = 100;
  }

  // 错误计数
  let me = 0, oe = 0, pf = 0;
  disks.forEach(d => {
    me += Number(d.media_error) || 0;
    oe += Number(d.other_error) || 0;
    pf += Number(d.predictive_failure) || 0;
  });
  const errors = Math.max(0, Math.min(100, 100 - pf * 40 - me * 10 - oe * 3));

  // SMART 告警
  let smart = 100;
  if (disks.some(d => String(d.smart_alert) === 'Yes')) smart = 20;
  else if (disks.some(d => (Number(d.reallocated) || 0) > 0 || (Number(d.pending) || 0) > 0 || (Number(d.uncorrectable) || 0) > 0)) smart = 60;

  const subs = [
    { name: '控制器', score: ctrl, weight: 0.30 },
    { name: 'BBU', score: bbu, weight: 0.10 },
    { name: '温度', score: temp, weight: 0.25 },
    { name: '错误计数', score: errors, weight: 0.20 },
    { name: 'SMART', score: smart, weight: 0.15 },
  ];
  const total = Math.round(subs.reduce((s, x) => s + x.score * x.weight, 0));
  return { total, subs };
}

function renderHealth(st) {
  const { total, subs } = computeHealth(st);
  const C = 2 * Math.PI * 54;
  const ring = $('#ring-val');
  ring.style.strokeDashoffset = String(C * (1 - total / 100));
  ring.setAttribute('stroke', scoreColor(total).replace('var(--ok)', '#34a853').replace('var(--warn)', '#f9ab00').replace('var(--crit)', '#ea4335'));
  $('#health-score').textContent = total;
  const hb = $('#health-badge');
  hb.className = 'badge ' + (total >= 80 ? 'ok' : total >= 60 ? 'warn' : 'crit');
  hb.textContent = total >= 80 ? '健康' : total >= 60 ? '需要关注' : '存在风险';
  $('#sub-scores').innerHTML = subs.map(s => `
    <div class="sub-score">
      <span>${esc(s.name)}</span>
      <span class="bar"><i style="width:${s.score}%;background:${scoreColor(s.score)}"></i></span>
      <span class="pct">${s.score}</span>
    </div>`).join('');
}

/* ---------- 四个状态卡 ---------- */
function renderStatCards(st) {
  const c = st.controller || {};
  const disks = st.physical_disks || [];
  const temps = disks.map(d => d.temperature).filter(t => t != null && !isNaN(t)).map(Number);
  const avgT = temps.length ? (temps.reduce((a, b) => a + b, 0) / temps.length) : null;
  const maxT = temps.length ? Math.max.apply(null, temps) : null;
  const maxDisk = maxT != null ? (disks.find(d => Number(d.temperature) === maxT) || {}).label || '' : '';
  const cards = [
    {
      label: '控制器状态',
      value: `<span style="font-size:15px">${stateBadge(c.health)}</span>`,
      sub: `${esc(c.model || '—')} · 固件 ${esc(c.fw || '—')}`,
    },
    {
      label: '磁盘数量',
      value: `${c.num_disks != null ? esc(c.num_disks) : disks.length} <span class="tiny">/ ${c.num_vds != null ? esc(c.num_vds) : (st.virtual_disks || []).length} 虚拟盘</span>`,
      sub: '物理磁盘 / 虚拟磁盘',
    },
    {
      label: '磁盘温度',
      value: avgT != null ? `${avgT.toFixed(1)}°C` : '—',
      sub: maxT != null ? `最高 ${maxT}°C（${esc(maxDisk)}）` : '无温度数据',
    },
    {
      label: 'BBU 状态',
      value: `<span style="font-size:15px">${c.bbu_state ? stateBadge(c.bbu_state) : '<span class="muted">—</span>'}</span>`,
      sub: `${esc(c.bbu_model || '无 BBU')}${c.bbu_temperature != null ? ' · ' + esc(c.bbu_temperature) + '°C' : ''}`,
    },
  ];
  $('#stat-cards').innerHTML = cards.map(k => `
    <div class="card">
      <div class="eyebrow">${k.label}</div>
      <div class="stat-value">${k.value}</div>
      <div class="stat-sub">${k.sub}</div>
    </div>`).join('');
}

/* ---------- 磁盘槽位拓扑 ---------- */
function renderTopology(st) {
  const disks = (st.physical_disks || []).slice().sort((a, b) => (Number(a.eid) - Number(b.eid)) || (Number(a.slot) - Number(b.slot)));
  const grid = $('#topo-grid');
  if (!disks.length) { grid.innerHTML = '<div class="loading-line">无物理磁盘数据</div>'; return; }
  grid.innerHTML = '';
  disks.forEach(d => {
    const tone = stateTone(d.state);
    const tTone = tempTone(Number(d.temperature));
    const cls = tone === 'crit' ? 'st-crit' : (tTone || tone) === 'warn' ? 'st-warn' : tone === 'ok' ? 'st-ok' : 'st-unknown';
    const cell = document.createElement('button');
    cell.className = 'topo-cell ' + cls;
    cell.innerHTML = `
      <span class="slot">${esc(d.label || ('E' + d.eid + ':S' + d.slot))}</span>
      <span class="state">${esc(d.state || '—')}</span>
      <span class="temp">${d.temperature != null ? esc(d.temperature) + '°C' : '—'}</span>`;
    cell.addEventListener('click', () => openDrawer(d));
    grid.appendChild(cell);
  });
}

/* ---------- 巡读 / 一致性检查 ---------- */
function renderMaintenance(st) {
  const m = st.maintenance || {};
  const wrap = $('#maint-cards');

  const prOps = state.isAdmin ? `
    <div class="maint-ops">
      <button class="btn sm" data-pr="start">启动</button>
      <button class="btn sm" data-pr="pause">暂停</button>
      <button class="btn sm" data-pr="resume">恢复</button>
      <button class="btn sm danger" data-pr="stop">停止</button>
    </div>` : '';

  const vdOpts = (state.vds || []).map(v =>
    `<option value="${esc(v.vd)}">VD ${esc(v.vd)}（${esc(v.dg_vd || '')} ${esc(v.name || '')}）</option>`).join('');
  const ccOps = state.isAdmin ? `
    <div class="maint-ops">
      <select class="select" id="sel-cc-vd">${vdOpts || '<option value="">无虚拟盘</option>'}</select>
      <button class="btn sm" id="btn-cc-start" ${state.vds.length ? '' : 'disabled'}>启动</button>
      <button class="btn sm danger" id="btn-cc-stop" ${state.vds.length ? '' : 'disabled'}>停止</button>
    </div>` : '';

  const mk = (title, eyebrow, o, opsHtml) => `
    <div class="card">
      <div class="card-head">
        <div><div class="eyebrow">${eyebrow}</div><strong>${title}</strong></div>
        ${o && o.state ? stateBadge(o.state) : ''}
      </div>
      <dl class="kv">
        <dt>模式</dt><dd>${esc((o && o.mode) || '—')}</dd>
        <dt>状态</dt><dd>${esc((o && o.state) || '—')}</dd>
        <dt>下次执行</dt><dd>${esc((o && o.next) || '—')}</dd>
        <dt>迭代次数</dt><dd>${o && o.iterations != null ? esc(o.iterations) : '—'}</dd>
      </dl>
      ${opsHtml}
    </div>`;
  wrap.innerHTML =
    mk('巡读 (Patrol Read)', 'Patrol read', m.patrol_read, prOps) +
    mk('一致性检查', 'Consistency check', m.consistency_check, ccOps);

  if (state.isAdmin) {
    $$('[data-pr]', wrap).forEach(b => b.addEventListener('click', () => {
      const action = b.dataset.pr;
      const text = { start: '启动', pause: '暂停', resume: '恢复', stop: '停止' }[action];
      raidAction({ target: 'patrolread', action }, `巡读：${text}`,
        `确认${text}巡读 (Patrol Read)？`, action === 'stop');
    }));
    const ccVd = () => { const v = $('#sel-cc-vd').value; return v === '' ? null : Number(v); };
    $('#btn-cc-start') && $('#btn-cc-start').addEventListener('click', () => {
      const vd = ccVd();
      if (vd == null) { toast('请选择虚拟盘', 'error'); return; }
      raidAction({ target: 'cc', action: 'start', vd }, `一致性检查：启动`,
        `确认对 VD ${vd} 启动一致性检查？该操作会占用一定的 IO 资源。`, false);
    });
    $('#btn-cc-stop') && $('#btn-cc-stop').addEventListener('click', () => {
      const vd = ccVd();
      if (vd == null) { toast('请选择虚拟盘', 'error'); return; }
      raidAction({ target: 'cc', action: 'stop', vd }, `一致性检查：停止`,
        `确认停止 VD ${vd} 上正在运行的一致性检查？`, true);
    });
  }
}

/* ---------- RAID 操作（巡读 / CC / VD 初始化） ---------- */
function raidAction(body, title, desc, danger) {
  confirmModal(title, `<p>${esc(desc)}</p>`, '确认执行', danger, async () => {
    const r = await api('/api/raid_action', { method: 'POST', body });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(title + ' 已执行', 'ok');
    await loadStatus();
    await loadVdDetail();
  });
}

/* ---------- 虚拟磁盘（vd_detail） ---------- */
async function loadVdDetail() {
  let data;
  try { data = await api('/api/vd_detail'); }
  catch (e) { return; }
  state.vds = data.vds || [];
  renderVirtualDisks();
  if (state.status) renderMaintenance(state.status); // VD 列表就绪后重绘维护卡的 VD 下拉
}

function renderVirtualDisks() {
  const vds = state.vds || [];
  const tb = $('#vd-table tbody');
  const hideOps = !state.isAdmin;
  $$('#vd-table .admin-col').forEach(el => el.classList.toggle('col-hidden', hideOps));
  if (!vds.length) { tb.innerHTML = '<tr><td colspan="9" class="muted">无虚拟磁盘</td></tr>'; return; }
  tb.innerHTML = '';
  vds.forEach(v => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="num">${esc(v.dg_vd || '—')}</td>
      <td>${esc(v.name || '—')}</td>
      <td>${esc(v.type || '—')}</td>
      <td class="num">${esc(v.size || '—')}</td>
      <td>${stateBadge(v.state)}</td>
      <td class="num">${esc(v.os_device || '—')}</td>
      <td class="num">${esc(v.write_cache || '—')}</td>
      <td class="num">${esc(v.current_operation || 'None')}</td>
      <td class="ops admin-col ${hideOps ? 'col-hidden' : ''}"></td>`;
    if (state.isAdmin) {
      const ops = tr.querySelector('.ops');
      const sel = document.createElement('select');
      sel.className = 'select';
      sel.innerHTML = `
        <option value="">操作…</option>
        <option value="init_start">初始化开始</option>
        <option value="init_stop">初始化停止</option>
        <option value="cc_start">CC 开始</option>
        <option value="cc_stop">CC 停止</option>`;
      sel.addEventListener('change', () => {
        const action = sel.value;
        sel.value = '';
        if (action) vdAction(v, action);
      });
      ops.appendChild(sel);
    }
    tb.appendChild(tr);
  });
}

const VD_ACTION_MAP = {
  init_start: { target: 'vd_init', action: 'start', text: '初始化开始', danger: true,
    desc: 'VD 初始化会擦除虚拟盘上的现有数据（快速初始化擦除首尾区域）。存在数据丢失风险。' },
  init_stop: { target: 'vd_init', action: 'stop', text: '初始化停止', danger: false,
    desc: '停止该虚拟盘上正在运行的初始化任务。' },
  cc_start: { target: 'cc', action: 'start', text: 'CC 开始', danger: false,
    desc: '对该虚拟盘启动一致性检查，会占用一定的 IO 资源。' },
  cc_stop: { target: 'cc', action: 'stop', text: 'CC 停止', danger: true,
    desc: '停止该虚拟盘上正在运行的一致性检查。' },
};

function vdAction(v, key) {
  const a = VD_ACTION_MAP[key];
  if (!a) return;
  const label = `VD ${v.vd}（${v.dg_vd || ''} ${v.name || ''}）`;
  raidAction(
    { target: a.target, action: a.action, vd: v.vd },
    `${a.text}`,
    `虚拟盘 ${label}：${a.desc}`,
    a.danger
  );
}

/* ---------- 物理磁盘 ---------- */
// 仅未配置（UGood/JBOD）的磁盘允许勾选创建阵列；
// 已加入阵列（Onln 等）或异常状态的磁盘一律禁止勾选，避免误伤现有数据
function pdRaidEligible(d) {
  return d.state === 'UGood' || d.state === 'JBOD';
}

function renderPhysicalDisks(st) {
  const disks = st.physical_disks || [];
  const tb = $('#pd-table tbody');
  const hideSel = !state.isAdmin;
  $$('#pd-table .admin-col').forEach(el => el.classList.toggle('col-hidden', hideSel));
  $('#pd-raid-bar').classList.toggle('hidden', hideSel);

  // 清理勾选集合中已不存在或已变为不可勾选的磁盘
  const eligibleKeys = new Set(
    disks.filter(pdRaidEligible).map(d => d.eid + ':' + d.slot)
  );
  Array.from(state.raidSel).forEach(k => { if (!eligibleKeys.has(k)) state.raidSel.delete(k); });

  if (!disks.length) {
    tb.innerHTML = '<tr><td colspan="13" class="muted">无物理磁盘</td></tr>';
    updateRaidBar();
    return;
  }
  tb.innerHTML = '';
  disks.forEach(d => {
    const tr = document.createElement('tr');
    const alerts = [];
    if (String(d.smart_alert) === 'Yes') alerts.push('<span class="badge crit">SMART</span>');
    if (Number(d.predictive_failure) > 0) alerts.push('<span class="badge warn">PF</span>');
    if (Number(d.shield_counter) > 0) alerts.push('<span class="badge warn">Shield</span>');
    const eligible = pdRaidEligible(d);
    const key = d.eid + ':' + d.slot;
    tr.innerHTML = `
      <td class="admin-col ${hideSel ? 'col-hidden' : ''}"></td>
      <td class="num">${esc(d.label || ('E' + d.eid + ':S' + d.slot))}${d.locate ? '<span class="locate-dot" title="定位灯已开启"></span>' : ''}</td>
      <td>${esc(d.model || '—')}</td>
      <td class="num">${esc(d.sn || '—')}</td>
      <td class="num">${esc(d.fw_rev || '—')}</td>
      <td class="num">${d.dg != null ? esc(d.dg) : '—'}</td>
      <td>${stateBadge(d.state)}</td>
      <td class="num">${fmtTemp(d.temperature)}</td>
      <td class="num">${num0(d.media_error)}/${num0(d.other_error)}/${num0(d.predictive_failure)}</td>
      <td class="num">${num0(d.reallocated)}/${num0(d.pending)}/${num0(d.uncorrectable)}</td>
      <td class="num">${fmtHours(d.power_on_hours)}</td>
      <td>${alerts.join(' ') || '<span class="tiny">—</span>'}</td>
      <td class="ops"></td>`;
    if (state.isAdmin) {
      const selCell = tr.querySelector('.admin-col');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = state.raidSel.has(key);
      cb.disabled = !eligible;
      cb.title = eligible
        ? '勾选后可参与创建阵列'
        : '该磁盘已加入阵列或状态不允许，禁止勾选';
      cb.addEventListener('change', () => {
        if (cb.checked) state.raidSel.add(key); else state.raidSel.delete(key);
        updateRaidBar();
      });
      selCell.appendChild(cb);
    }
    const ops = tr.querySelector('.ops');
    const detail = document.createElement('button');
    detail.className = 'btn sm';
    detail.textContent = '详情';
    detail.addEventListener('click', () => openDrawer(d));
    ops.appendChild(detail);
    if (state.isAdmin) {
      const sel = document.createElement('select');
      sel.className = 'select';
      sel.style.marginLeft = '6px';
      sel.innerHTML = `
        <option value="">操作…</option>
        <option value="online">上线</option>
        <option value="offline">下线</option>
        <option value="good">置为 UGood</option>
        <option value="jbod">置为 JBOD</option>
        <option value="locate_start">定位开</option>
        <option value="locate_stop">定位关</option>`;
      sel.addEventListener('change', () => {
        const action = sel.value;
        sel.value = '';
        if (action) diskAction(d, action);
      });
      ops.appendChild(sel);
    }
    tb.appendChild(tr);
  });
  updateRaidBar();
}
function num0(v) { return v != null && !isNaN(v) ? esc(v) : '0'; }

const DISK_ACTION_TEXT = {
  online: ['上线', '将磁盘上线。'],
  offline: ['下线', '将磁盘下线会使其脱离磁盘组，可能导致虚拟盘降级甚至数据不可用。'],
  good: ['置为 UGood', '将磁盘置为 UGood 未配置状态，磁盘上的阵列配置信息可能被清除。'],
  jbod: ['置为 JBOD', '将磁盘置为 JBOD 直通模式，磁盘上的阵列配置信息可能被清除。'],
  locate_start: ['定位开', '点亮磁盘定位指示灯。'],
  locate_stop: ['定位关', '熄灭磁盘定位指示灯。'],
};
const DANGER_ACTIONS = ['offline', 'good', 'jbod'];

function diskAction(d, action) {
  const [text, desc] = DISK_ACTION_TEXT[action] || [action, ''];
  const label = d.label || ('E' + d.eid + ':S' + d.slot);
  const doIt = async () => {
    const r = await api('/api/disk_action', { method: 'POST', body: { eid: d.eid, slot: d.slot, action } });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(`磁盘 ${label}：${text} 已执行`, 'ok');
    await loadStatus();
  };
  if (DANGER_ACTIONS.includes(action)) {
    confirmModal(`危险操作：${text}`,
      `<p>磁盘 <strong class="mono">${esc(label)}</strong>（${esc(d.model || '')}）</p>
       <p class="warn-text">${esc(desc)} 此操作存在数据丢失风险，请确认后再执行。</p>`,
      '确认执行', true, doIt);
  } else {
    confirmModal(`${text}`,
      `<p>磁盘 <strong class="mono">${esc(label)}</strong>（${esc(d.model || '')}）</p><p>${esc(desc)}</p>`,
      '确认执行', false, doIt);
  }
}

/* ---------- 创建磁盘阵列 ---------- */
// 与后端 RAID_LEVEL_RULES 保持一致，仅用于前端提示，最终以后端校验为准
const RAID_LEVEL_RULES = {
  '0': { min: 1, desc: '至少 1 块盘' },
  '1': { min: 2, exact: 2, desc: '恰好 2 块盘' },
  '5': { min: 3, desc: '至少 3 块盘' },
  '6': { min: 4, desc: '至少 4 块盘' },
  '10': { min: 4, even: true, desc: '至少 4 块且为偶数' },
  '50': { min: 6, mult: 3, desc: '至少 6 块且为 3 的倍数' },
};

function raidLevelError(level, n) {
  const rule = RAID_LEVEL_RULES[level];
  if (!rule) return '请选择 RAID 级别';
  if (n < rule.min) return `RAID${level} ${rule.desc}，当前已选 ${n} 块`;
  if (rule.exact && n !== rule.exact) return `RAID${level} ${rule.desc}，当前已选 ${n} 块`;
  if (rule.even && n % 2 !== 0) return `RAID${level} 需要偶数块盘，当前已选 ${n} 块`;
  if (rule.mult && n % rule.mult !== 0) return `RAID${level} 盘数需为 ${rule.mult} 的倍数，当前已选 ${n} 块`;
  return '';
}

function updateRaidBar() {
  const n = state.raidSel.size;
  const level = $('#raid-level') ? $('#raid-level').value : '0';
  const err = raidLevelError(level, n);
  $('#raid-sel-count').textContent = n ? `已选 ${n} 块盘` : '勾选未配置磁盘可创建阵列';
  const btn = $('#btn-raid-create');
  btn.disabled = !!err;
  btn.title = err || `以 RAID${level} 创建阵列`;
}

function createRaid() {
  const level = $('#raid-level').value;
  const name = ($('#raid-name').value || '').trim();
  const n = state.raidSel.size;
  const err = raidLevelError(level, n);
  if (err) { toast(err, 'error'); return; }
  if (name && !/^[\w .-]{1,32}$/.test(name)) {
    toast('阵列名称仅支持字母数字/空格/._-，最长 32 字符', 'error');
    return;
  }

  // 再次确认所选磁盘均为未配置状态（UGood/JBOD），不在任何阵列中
  const disks = (state.status && state.status.physical_disks) || [];
  const chosen = [];
  for (const key of state.raidSel) {
    const [eid, slot] = key.split(':').map(Number);
    const d = disks.find(x => Number(x.eid) === eid && Number(x.slot) === slot);
    if (!d || !pdRaidEligible(d)) {
      toast(`磁盘 E${eid}:S${slot} 状态已变化，请重新勾选`, 'error');
      return;
    }
    chosen.push(d);
  }
  chosen.sort((a, b) => (a.eid - b.eid) || (a.slot - b.slot));

  const rows = chosen.map(d =>
    `<li><strong class="mono">${esc(d.label || ('E' + d.eid + ':S' + d.slot))}</strong>
      — ${esc(d.model || '未知型号')} · ${esc(d.size || '')} · ${esc(d.state)}</li>`).join('');
  confirmModal('危险操作：创建磁盘阵列',
    `<p>将以 <strong>RAID${esc(level)}</strong>${name ? `（名称：${esc(name)}）` : ''} 创建阵列，包含以下 ${chosen.length} 块磁盘：</p>
     <ul style="margin:8px 0;padding-left:20px;line-height:1.8">${rows}</ul>
     <p class="warn-text">创建阵列会清除上述磁盘上的全部数据！现有磁盘和已有阵列不受影响。请确认无误后再执行。</p>`,
    '确认创建', true, async () => {
      const r = await api('/api/raid/create', {
        method: 'POST',
        body: {
          level,
          name,
          drives: chosen.map(d => ({ eid: d.eid, slot: d.slot })),
        },
      });
      if (r && r.ok === false) throw new Error(r.error || '创建失败');
      toast(`RAID${level} 阵列创建成功`, 'ok');
      state.raidSel.clear();
      $('#raid-name').value = '';
      await loadStatus();
      await loadVdDetail();
    });
}

/* ---------- 系统信息 ---------- */
function renderSystem(st) {
  const s = st.system || {};
  $('#system-kv').innerHTML = `
    <dt>主机名</dt><dd>${esc(st.host || '—')}</dd>
    <dt>系统负载</dt><dd>${esc(s.load || '—')}</dd>
    <dt>内存</dt><dd>${esc(s.memory || '—')}</dd>
    <dt>数据时间</dt><dd>${esc(fmtDateTime(st.timestamp))}</dd>`;
}

/* ---------- 趋势图（温度 / IO / 文件系统） ---------- */
function chartPalette() {
  const dark = document.documentElement.classList.contains('dark');
  return dark
    ? ['#2dccd3', '#f1204a', '#edbbe8', '#fbeb35', '#baf6f0']
    : ['#4285f4', '#ea4335', '#fbbc05', '#0043ad', '#34a853'];
}

const CHART_TYPES = {
  temp: { title: '磁盘温度趋势', unit: '°C' },
  io: { title: '磁盘 IO 吞吐', unit: 'KB/s' },
  fs: { title: '文件系统使用率', unit: '%' },
};

async function fetchChartDatasets(type, palette) {
  if (type === 'temp') {
    const data = await api('/api/history?hours=' + state.hours);
    return (data.series || []).map((s, i) => ({
      label: s.label,
      data: (s.points || []).map(p => ({ x: p[0], y: p[1] })),
      borderColor: palette[i % palette.length],
      backgroundColor: palette[i % palette.length],
    }));
  }
  if (type === 'io') {
    const data = await api('/api/io_history?hours=' + state.hours);
    const out = [];
    (data.series || []).forEach((s, i) => {
      const color = palette[i % palette.length];
      const pts = s.points || [];
      out.push({
        label: s.label + ' 读',
        data: pts.map(p => ({ x: p[0], y: p[1] })),
        borderColor: color, backgroundColor: color,
      });
      out.push({
        label: s.label + ' 写',
        data: pts.map(p => ({ x: p[0], y: p[2] })),
        borderColor: color, backgroundColor: color,
        borderDash: [4, 3],
      });
    });
    return out;
  }
  // fs
  const data = await api('/api/fs_history?hours=' + state.hours);
  return (data.series || []).map((s, i) => ({
    label: s.label,
    data: (s.points || []).map(p => ({ x: p[0], y: p[1] })),
    borderColor: palette[i % palette.length],
    backgroundColor: palette[i % palette.length],
  }));
}

async function loadHistory() {
  const type = state.chartType;
  const meta = CHART_TYPES[type] || CHART_TYPES.temp;
  $('#chart-title').textContent = meta.title;
  let datasets;
  try { datasets = await fetchChartDatasets(type, chartPalette()); }
  catch (e) { datasets = []; }
  const emptyBox = $('#chart-empty');
  const canvas = $('#temp-chart');
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  const hasData = datasets.some(ds => ds.data && ds.data.length > 0);
  if (!hasData || typeof Chart === 'undefined') {
    emptyBox.textContent = typeof Chart === 'undefined' ? '图表组件加载失败' : '暂无数据';
    emptyBox.classList.remove('hidden');
    canvas.style.visibility = 'hidden';
    return;
  }
  emptyBox.classList.add('hidden');
  canvas.style.visibility = 'visible';
  const css = getComputedStyle(document.body);
  const tickColor = css.getPropertyValue('--muted-foreground').trim() || '#7f8d9f';
  const gridColor = css.getPropertyValue('--border').trim() || '#ebebeb';
  datasets.forEach(ds => {
    ds.borderWidth = 1.5;
    ds.pointRadius = 0;
    ds.pointHitRadius = 8;
    ds.tension = 0.25;
  });
  Chart.defaults.font.family = "'JetBrains Mono', monospace";
  Chart.defaults.font.size = 10;
  const fmt = state.hours > 24 ? fmtDayClock : fmtClock;
  const unit = meta.unit;
  state.chart = new Chart(canvas, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: { labels: { color: tickColor, boxWidth: 10, boxHeight: 2, font: { family: "'DM Sans', sans-serif", size: 11 } } },
        tooltip: {
          callbacks: {
            title: (items) => items.length ? fmtDayClock(items[0].parsed.x) : '',
            label: (item) => `${item.dataset.label}: ${item.parsed.y} ${unit}`,
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          ticks: { color: tickColor, maxTicksLimit: 8, callback: (v) => fmt(v) },
          grid: { color: gridColor },
        },
        y: {
          title: { display: true, text: unit, color: tickColor },
          ticks: { color: tickColor },
          grid: { color: gridColor },
        },
      },
    },
  });
}

/* ---------- 事件日志 ---------- */
async function loadEvents() {
  let data;
  try {
    data = await api(`/api/events?level=${state.evLevel}&page=${state.evPage}&page_size=${state.evPageSize}`);
  } catch (e) {
    $('#event-list').innerHTML = `<div class="loading-line">加载失败：${esc(e.message)}</div>`;
    return;
  }
  state.evTotal = data.total || 0;
  const list = $('#event-list');
  const events = data.events || [];
  if (!events.length) {
    list.innerHTML = '<div class="loading-line">暂无事件</div>';
  } else {
    list.innerHTML = events.map(ev => {
      const lv = String(ev.level || 'info').toLowerCase();
      const cls = lv === 'error' ? 'crit' : lv === 'warning' ? 'warn' : 'info';
      const lvText = lv === 'error' ? '错误' : lv === 'warning' ? '警告' : '信息';
      return `<div class="event-item">
        <span class="event-time">${esc(fmtDateTime(ev.timestamp))}</span>
        <span class="badge ${cls}">${lvText}</span>
        <span>${esc(ev.message)}</span>
      </div>`;
    }).join('');
  }
  const pages = Math.max(1, Math.ceil(state.evTotal / state.evPageSize));
  $('#ev-page-info').textContent = `第 ${state.evPage} / ${pages} 页 · 共 ${state.evTotal} 条`;
  $('#ev-prev').disabled = state.evPage <= 1;
  $('#ev-next').disabled = state.evPage >= pages;
}

/* ---------- 邮件报警配置 ---------- */
async function loadAlertConfig() {
  let cfg;
  try { cfg = await api('/api/alert_config'); }
  catch (e) { return; }
  state.alertCfg = cfg;
  if (cfg.config) {
    if (cfg.config.temp_warn != null) state.tempWarn = Number(cfg.config.temp_warn);
    if (cfg.config.temp_crit != null) state.tempCrit = Number(cfg.config.temp_crit);
  }
  const en = $('#alert-enabled-badge');
  en.className = 'badge ' + (cfg.enabled ? 'ok' : '');
  en.textContent = cfg.enabled ? '报警已启用' : '报警已停用';
  const sm = $('#sendmail-badge');
  sm.className = 'badge ' + (cfg.sendmail_available ? 'ok' : 'crit');
  sm.textContent = cfg.sendmail_available ? 'sendmail 可用' : 'sendmail 不可用';

  const map = [
    ['alert_email_to', '#alert-email'],
    ['sendmail_path', '#alert-sendmail'],
    ['temp_warn', '#alert-warn'],
    ['temp_crit', '#alert-crit'],
  ];
  map.forEach(([key, sel]) => {
    const input = $(sel);
    if (cfg.config && cfg.config[key] != null) input.value = cfg.config[key];
    const locked = cfg.locked && cfg.locked[key];
    input.disabled = !!locked || !state.isAdmin;
    const hint = input.closest('.field').querySelector('[data-lock-hint]');
    if (hint) hint.classList.toggle('hidden', !locked);
  });
}

async function saveAlertConfig(ev) {
  ev.preventDefault();
  const btn = $('#btn-alert-save');
  btnLoading(btn, true);
  try {
    const body = {
      alert_email_to: $('#alert-email').value.trim(),
      sendmail_path: $('#alert-sendmail').value.trim(),
      temp_warn: Number($('#alert-warn').value),
      temp_crit: Number($('#alert-crit').value),
    };
    await api('/api/alert_config', { method: 'POST', body });
    state.tempWarn = body.temp_warn;
    state.tempCrit = body.temp_crit;
    toast('报警配置已保存', 'ok');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function testAlert() {
  const btn = $('#btn-alert-test');
  btnLoading(btn, true);
  try {
    const r = await api('/api/alert_test', { method: 'POST' });
    if (r && r.ok === false) throw new Error(r.error || '发送失败');
    toast('测试报警已发送', 'ok');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

/* ---------- 采集控制 ---------- */
async function loadCollectionConfig() {
  try {
    const cfg = await api('/api/collection_config');
    if (cfg && cfg.interval_minutes != null) $('#sel-interval').value = String(cfg.interval_minutes);
  } catch (e) { /* 忽略 */ }
}

async function changeInterval() {
  const sel = $('#sel-interval');
  const v = Number(sel.value);
  sel.disabled = true;
  try {
    await api('/api/collection_config', { method: 'POST', body: { interval_minutes: v } });
    toast(`采集间隔已设为 ${v} 分钟`, 'ok');
  } catch (e) {
    toast(e.message, 'error');
    await loadCollectionConfig();
  } finally {
    sel.disabled = !state.isAdmin;
  }
}

async function collectNow() {
  const btn = $('#btn-collect');
  btnLoading(btn, true);
  try {
    await api('/api/collect_now', { method: 'POST' });
    toast('采集完成', 'ok');
    await loadStatus();
    await loadHistory();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

/* ---------- 磁盘详情抽屉 ---------- */
function openDrawer(d) {
  state.currentDisk = d;
  $('#drawer-title').textContent = '磁盘 ' + (d.label || ('E' + d.eid + ':S' + d.slot));
  const rows = [
    ['槽位', d.label || ('E' + d.eid + ':S' + d.slot)],
    ['EID / Slot / DID', `${d.eid} / ${d.slot} / ${d.did != null ? d.did : '—'}`],
    ['型号', d.model],
    ['序列号', d.sn],
    ['固件版本', d.fw_rev],
    ['磁盘组', d.dg != null ? d.dg : '—'],
    ['状态', d.state],
    ['容量', d.size],
    ['接口 / 介质', `${d.intf || '—'} / ${d.med || '—'}`],
    ['温度', d.temperature != null ? d.temperature + ' °C' : '—'],
    ['通电时长', fmtHours(d.power_on_hours) + (d.power_on_hours != null ? `（${d.power_on_hours} 小时）` : '')],
    ['设备速率', d.dev_speed],
    ['链路速率', d.link_speed],
    ['SMART 摘要', `重映射 ${num0(d.reallocated)} · 待定 ${num0(d.pending)} · 无法纠正 ${num0(d.uncorrectable)}${String(d.smart_alert) === 'Yes' ? ' · 告警!' : ''}`],
    ['错误计数', `ME ${num0(d.media_error)} · OE ${num0(d.other_error)} · PF ${num0(d.predictive_failure)}`],
    ['Shield Counter', d.shield_counter != null ? d.shield_counter : '—'],
  ];
  $('#drawer-kv').innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v == null || v === '' ? '—' : v)}</dd>`).join('');
  state.smartData = null;
  $('#smart-body').innerHTML = '';
  $('#smart-body').classList.add('hidden');
  $('#smart-pre').classList.add('hidden');
  $('#smart-pre').textContent = '';
  const rawBtn = $('#btn-smart-raw');
  rawBtn.classList.add('hidden');
  rawBtn.textContent = '查看原始输出';
  $('#btn-smart-copy').classList.add('hidden');
  $('#drawer').classList.add('open');
  $('#drawer-scrim').classList.remove('hidden');
}
function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#drawer-scrim').classList.add('hidden');
  state.currentDisk = null;
}

async function loadSmart() {
  const d = state.currentDisk;
  if (!d) return;
  const btn = $('#btn-smart');
  btnLoading(btn, true);
  try {
    const r = await api(`/api/disk_smart?eid=${encodeURIComponent(d.eid)}&slot=${encodeURIComponent(d.slot)}`);
    state.smartData = r;
    renderSmart(r);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

const SCSI_KEY_TEXT = {
  temperature: '当前温度', power_on_time: '通电时间', grown_defects: 'Grown 缺陷数',
  start_stop_cycles: '启停次数', load_unload_cycles: '加载/卸载次数',
  read_errors: '读错误', write_errors: '写错误', verify_errors: '校验错误',
  non_medium_errors: '非介质错误', last_test_reason: '最近自检结果',
};

function renderSmart(r) {
  const body = $('#smart-body');
  let html = '';
  if (r.attrs && r.attrs.length) {
    html += '<div class="smart-section-title">SMART 属性</div>';
    html += `<div class="table-wrap"><table class="data smart-attrs"><thead><tr>
      <th>ID</th><th>名称</th><th>VALUE</th><th>WORST</th><th>THRESH</th><th>TYPE</th><th>UPDATED</th><th>RAW</th>
      </tr></thead><tbody>`;
    r.attrs.forEach(a => {
      const bad = Number(a.value) <= Number(a.thresh) && Number(a.thresh) > 0;
      const prefail = /pre.?fail/i.test(String(a.type || ''));
      html += `<tr class="${bad ? 'attr-bad' : prefail ? 'attr-prefail' : ''}">
        <td>${esc(a.id)}</td><td>${esc(a.name)}</td><td>${esc(a.value)}</td><td>${esc(a.worst)}</td>
        <td>${esc(a.thresh)}</td><td>${esc(a.type)}</td><td>${esc(a.updated)}</td><td>${esc(a.raw)}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  }
  const scsi = r.scsi && typeof r.scsi === 'object' ? r.scsi : null;
  const scsiKeys = scsi ? Object.keys(scsi).filter(k => scsi[k] != null && scsi[k] !== '') : [];
  if (scsiKeys.length) {
    html += '<div class="smart-section-title">SCSI 摘要</div><dl class="kv">';
    scsiKeys.forEach(k => {
      html += `<dt>${esc(SCSI_KEY_TEXT[k] || k)}</dt><dd>${esc(scsi[k])}</dd>`;
    });
    html += '</dl>';
  }
  if (!html) html = '<div class="loading-line">无结构化 SMART 数据，可查看原始输出。</div>';
  body.innerHTML = html;
  body.classList.remove('hidden');
  // 原始输出预置，切换展示
  $('#smart-pre').textContent = r.output || '（无输出）';
  $('#smart-pre').classList.add('hidden');
  const rawBtn = $('#btn-smart-raw');
  rawBtn.textContent = '查看原始输出';
  rawBtn.classList.remove('hidden');
  $('#btn-smart-copy').classList.remove('hidden');
}

function toggleSmartRaw() {
  const pre = $('#smart-pre');
  const show = pre.classList.contains('hidden');
  pre.classList.toggle('hidden', !show);
  $('#btn-smart-raw').textContent = show ? '隐藏原始输出' : '查看原始输出';
}

async function copySmart() {
  const text = $('#smart-pre').textContent;
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制 SMART 输出', 'ok');
  } catch (e) {
    toast('复制失败', 'error');
  }
}

/* ---------- 磁盘管理（块设备） ---------- */
async function loadStorage() {
  const tb = $('#storage-table tbody');
  tb.innerHTML = '<tr><td colspan="5" class="muted">加载中…</td></tr>';
  let data;
  try { data = await api('/api/storage/devices'); }
  catch (e) { tb.innerHTML = `<tr><td colspan="5" class="muted">加载失败：${esc(e.message)}</td></tr>`; return; }
  tb.innerHTML = '';
  const devices = data.devices || [];
  if (!devices.length) { tb.innerHTML = '<tr><td colspan="5" class="muted">未发现块设备</td></tr>'; return; }
  devices.forEach(dev => appendDeviceRows(tb, dev, 0));
  state.storageLoaded = true;
}

function appendDeviceRows(tb, dev, depth) {
  const tr = document.createElement('tr');
  if (dev.raid_member) tr.className = 'row-disabled';
  const mounted = (dev.mountpoints || []).length > 0;
  const fsText = [dev.fstype, dev.label].filter(Boolean).join(' · ');
  tr.innerHTML = `
    <td><span class="tree-name" style="padding-left:${depth * 20}px">
      ${depth > 0 ? '└' : ''} ${esc(dev.name || dev.path || '—')}
      ${dev.raid_member ? '<span class="raid-tag">RAID 成员</span>' : ''}
    </span></td>
    <td class="num">${esc(dev.size || '—')}</td>
    <td class="num">${esc(fsText || '—')}</td>
    <td class="num">${esc((dev.mountpoints || []).join(', ') || '—')}</td>
    <td class="ops"></td>`;
  const ops = tr.querySelector('.ops');
  if (dev.raid_member) {
    ops.innerHTML = '<span class="tiny">禁止操作</span>';
  } else if (state.isAdmin) {
    if (mounted) {
      const b = document.createElement('button');
      b.className = 'btn sm';
      b.textContent = '卸载';
      b.addEventListener('click', () => umountDevice(dev));
      ops.appendChild(b);
    } else {
      const b = document.createElement('button');
      b.className = 'btn sm';
      b.textContent = '挂载';
      b.addEventListener('click', () => mountDevice(dev));
      ops.appendChild(b);
    }
    const fmt = document.createElement('button');
    fmt.className = 'btn sm danger';
    fmt.style.marginLeft = '6px';
    fmt.textContent = '格式化';
    fmt.addEventListener('click', () => formatDevice(dev));
    ops.appendChild(fmt);
    // 整盘初始化：仅顶层磁盘、未挂载（含子设备）、非 RAID 成员
    if (depth === 0 && !subtreeMounted(dev)) {
      const init = document.createElement('button');
      init.className = 'btn sm';
      init.style.marginLeft = '6px';
      init.textContent = '初始化';
      init.addEventListener('click', () => initDiskDialog(dev));
      ops.appendChild(init);
    }
  } else {
    ops.innerHTML = '<span class="tiny">—</span>';
  }
  tb.appendChild(tr);
  (dev.children || []).forEach(ch => appendDeviceRows(tb, ch, depth + 1));
}

function mountDevice(dev) {
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <p>将设备 <strong class="mono">${esc(dev.path || dev.name)}</strong> 挂载到指定目录。</p>
    <div class="field"><label>挂载点</label><input class="input mono" id="mnt-point" placeholder="/mnt/data" /></div>`;
  showModal({
    title: '挂载设备', body: wrap,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '挂载', cls: 'primary',
        handler: async (btn) => {
          const mp = wrap.querySelector('#mnt-point').value.trim();
          if (!mp) { toast('请输入挂载点', 'error'); return; }
          btnLoading(btn, true);
          try {
            await api('/api/storage/mount', { method: 'POST', body: { device: dev.path || dev.name, mountpoint: mp } });
            toast('挂载成功', 'ok');
            closeModal();
            await loadStorage();
          } catch (e) { toast(e.message, 'error'); }
          finally { btnLoading(btn, false); }
        }
      }
    ]
  });
}

function umountDevice(dev) {
  confirmModal('卸载设备',
    `<p>确认卸载 <strong class="mono">${esc(dev.path || dev.name)}</strong>（${esc((dev.mountpoints || []).join(', '))}）？</p>`,
    '卸载', true, async () => {
      await api('/api/storage/umount', { method: 'POST', body: { device: dev.path || dev.name } });
      toast('已卸载', 'ok');
      await loadStorage();
    });
}

function formatDevice(dev) {
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <p>将设备 <strong class="mono">${esc(dev.path || dev.name)}</strong> 格式化为：</p>
    <div class="field"><label>文件系统</label>
      <select class="select" id="fmt-fs" style="width:100%">
        <option value="ext4">ext4</option>
        <option value="xfs">xfs</option>
      </select></div>
    <p class="warn-text">警告：格式化将清除该设备上的全部数据，且不可恢复。请确认设备选择无误。</p>`;
  showModal({
    title: '格式化设备', body: wrap,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '确认格式化', cls: 'danger',
        handler: async (btn) => {
          const fs = wrap.querySelector('#fmt-fs').value;
          btnLoading(btn, true);
          try {
            await api('/api/storage/format', { method: 'POST', body: { device: dev.path || dev.name, fs_type: fs } });
            toast('格式化完成', 'ok');
            closeModal();
            await loadStorage();
          } catch (e) { toast(e.message, 'error'); }
          finally { btnLoading(btn, false); }
        }
      }
    ]
  });
}

/* ---------- 用户管理 ---------- */
async function loadUsers() {
  $('#users-warning').classList.toggle('hidden', state.authRequired);
  const tb = $('#users-table tbody');
  tb.innerHTML = '<tr><td colspan="3" class="muted">加载中…</td></tr>';
  let data;
  try { data = await api('/api/users'); }
  catch (e) { tb.innerHTML = `<tr><td colspan="3" class="muted">加载失败：${esc(e.message)}</td></tr>`; return; }
  tb.innerHTML = '';
  const users = data.users || [];
  if (!users.length) { tb.innerHTML = '<tr><td colspan="3" class="muted">暂无用户</td></tr>'; return; }
  users.forEach(u => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${esc(u.username)}</td>
      <td><span class="badge ${u.role === 'admin' ? 'info' : ''}">${u.role === 'admin' ? '管理员' : '只读用户'}</span></td>
      <td class="ops"></td>`;
    const ops = tr.querySelector('.ops');
    const rp = document.createElement('button');
    rp.className = 'btn sm';
    rp.textContent = '重置口令';
    rp.addEventListener('click', () => resetPassword(u));
    ops.appendChild(rp);
    const del = document.createElement('button');
    del.className = 'btn sm danger';
    del.style.marginLeft = '6px';
    del.textContent = '删除';
    del.addEventListener('click', () => deleteUser(u));
    ops.appendChild(del);
    tb.appendChild(tr);
  });
  state.usersLoaded = true;
}

function resetPassword(u) {
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <p>为用户 <strong>${esc(u.username)}</strong> 设置新口令。</p>
    <div class="field"><label>新口令</label><input class="input" id="rp-password" type="password" /></div>`;
  showModal({
    title: '重置口令', body: wrap,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '重置', cls: 'primary',
        handler: async (btn) => {
          const pw = wrap.querySelector('#rp-password').value;
          if (!pw) { toast('请输入新口令', 'error'); return; }
          btnLoading(btn, true);
          try {
            await api('/api/users/' + encodeURIComponent(u.username) + '/password', { method: 'POST', body: { password: pw } });
            toast('口令已重置', 'ok');
            closeModal();
          } catch (e) { toast(e.message, 'error'); }
          finally { btnLoading(btn, false); }
        }
      }
    ]
  });
}

function deleteUser(u) {
  confirmModal('删除用户',
    `<p>确认删除用户 <strong>${esc(u.username)}</strong>？该操作不可恢复。</p>`,
    '删除', true, async () => {
      await api('/api/users/' + encodeURIComponent(u.username), { method: 'DELETE' });
      toast('用户已删除', 'ok');
      await loadUsers();
    });
}

async function createUser(ev) {
  ev.preventDefault();
  const btn = $('#btn-user-create');
  btnLoading(btn, true);
  try {
    await api('/api/users', {
      method: 'POST',
      body: {
        username: $('#nu-username').value.trim(),
        password: $('#nu-password').value,
        role: $('#nu-role').value,
      },
    });
    toast('用户已创建', 'ok');
    $('#user-form').reset();
    await loadUsers();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

/* ---------- 视图切换 ---------- */
function switchView(v) {
  state.view = v;
  $$('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  ['overview', 'storage', 'logs', 'users'].forEach(name => {
    $('#view-' + name).classList.toggle('hidden', name !== v);
  });
  if (v === 'storage' && !state.storageLoaded) loadStorage();
  if (v === 'storage' && !state.fsUsage) loadFsUsage();
  if (v === 'storage' && !state.nfsLoaded) loadNfs();
  if (v === 'users' && !state.usersLoaded) loadUsers();
  $('#sidebar').classList.remove('open');
  $('#sidebar-scrim').classList.remove('show');
}

/* ---------- UI 绑定 ---------- */
function bindUI() {
  // 侧边栏导航
  $$('.nav-btn').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
  $('#btn-sidebar').addEventListener('click', () => {
    const sb = $('#sidebar');
    if (window.innerWidth <= 860) {
      sb.classList.toggle('open');
      $('#sidebar-scrim').classList.toggle('show', sb.classList.contains('open'));
    } else {
      $('#app').classList.toggle('sb-collapsed');
    }
  });
  $('#sidebar-scrim').addEventListener('click', () => {
    $('#sidebar').classList.remove('open');
    $('#sidebar-scrim').classList.remove('show');
  });

  // 主题切换
  $('#btn-theme').addEventListener('click', () => {
    const dark = !document.documentElement.classList.contains('dark');
    applyTheme(dark ? 'dark' : 'light', true);
  });

  // 顶栏操作
  $('#btn-export').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = '/api/export.csv';
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  });
  $('#btn-collect').addEventListener('click', collectNow);
  $('#sel-interval').addEventListener('change', changeInterval);

  // 创建磁盘阵列
  $('#btn-raid-create').addEventListener('click', createRaid);
  $('#raid-level').addEventListener('change', updateRaidBar);
  $('#btn-logout').addEventListener('click', async () => {
    try { await api('/api/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ }
    location.reload();
  });

  // 登录
  $('#login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = $('#login-btn');
    $('#login-error').textContent = '';
    btnLoading(btn, true);
    try {
      const r = await api('/api/login', {
        method: 'POST',
        body: { username: $('#login-username').value.trim(), password: $('#login-password').value },
      });
      state.me = { auth_required: true, logged_in: true, username: r.username, role: r.role };
      await afterLogin();
    } catch (e) {
      $('#login-error').textContent = e.message || '登录失败';
    } finally {
      btnLoading(btn, false);
    }
  });

  // 报警配置
  $('#alert-form').addEventListener('submit', saveAlertConfig);
  $('#btn-alert-test').addEventListener('click', testAlert);

  // 图表类型与时间范围
  $$('#chart-type .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#chart-type .chip').forEach(c => c.classList.toggle('active', c === ch));
    state.chartType = ch.dataset.type;
    loadHistory();
  }));
  $$('#chart-range .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#chart-range .chip').forEach(c => c.classList.toggle('active', c === ch));
    state.hours = Number(ch.dataset.hours);
    loadHistory();
  }));

  // 控制器事件
  $$('#ctl-lines .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#ctl-lines .chip').forEach(c => c.classList.toggle('active', c === ch));
    state.ctlLines = Number(ch.dataset.lines);
    loadCtlEvents();
  }));
  $('#btn-ctl-refresh').addEventListener('click', loadCtlEvents);
  $('#btn-ctl-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#ctl-pre').textContent);
      toast('已复制控制器事件', 'ok');
    } catch (e) { toast('复制失败', 'error'); }
  });

  // 事件筛选与分页
  $$('#event-filter .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#event-filter .chip').forEach(c => c.classList.toggle('active', c === ch));
    state.evLevel = ch.dataset.level;
    state.evPage = 1;
    loadEvents();
  }));
  $('#ev-prev').addEventListener('click', () => { if (state.evPage > 1) { state.evPage--; loadEvents(); } });
  $('#ev-next').addEventListener('click', () => { state.evPage++; loadEvents(); });

  // 抽屉
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-scrim').addEventListener('click', closeDrawer);
  $('#btn-smart').addEventListener('click', loadSmart);
  $('#btn-smart-raw').addEventListener('click', toggleSmartRaw);
  $('#btn-smart-copy').addEventListener('click', copySmart);

  // 对话框
  $('#modal-scrim').addEventListener('click', (ev) => { if (ev.target === ev.currentTarget) closeModal(); });

  // 存储 / 用户
  $('#btn-storage-refresh').addEventListener('click', () => { loadStorage(); loadFsUsage(); });
  $('#btn-fs-refresh').addEventListener('click', loadFsUsage);
  $('#btn-nfs-refresh').addEventListener('click', loadNfs);
  $('#nfs-form').addEventListener('submit', (ev) => { ev.preventDefault(); addNfs(); });
  $('#user-form').addEventListener('submit', createUser);

  // 页面重新可见时立即刷新实时数据
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.me) loadRealtime().catch(() => {});
  });
}

/* ---------- 系统资源（实时，5 秒轮询） ---------- */
function fmtUptime(sec) {
  if (sec == null || isNaN(sec)) return '—';
  const s = Number(sec);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分`;
  return `${m} 分`;
}
function fmtRate(bps) {
  if (bps == null || isNaN(bps)) return '—';
  const v = Number(bps);
  if (v >= 1073741824) return (v / 1073741824).toFixed(1) + ' GB/s';
  if (v >= 1048576) return (v / 1048576).toFixed(1) + ' MB/s';
  if (v >= 1024) return (v / 1024).toFixed(1) + ' KB/s';
  return v.toFixed(0) + ' B/s';
}
function fmtBytes(n) {
  if (n == null || isNaN(n)) return '—';
  const v = Number(n);
  if (v >= 1099511627776) return (v / 1099511627776).toFixed(1) + 'T';
  if (v >= 1073741824) return (v / 1073741824).toFixed(1) + 'G';
  if (v >= 1048576) return (v / 1048576).toFixed(1) + 'M';
  if (v >= 1024) return (v / 1024).toFixed(1) + 'K';
  return v + 'B';
}
function meterHtml(label, pct, valText, cls) {
  const p = Math.max(0, Math.min(100, pct == null || isNaN(pct) ? 0 : pct));
  return `<div class="meter-row">
    <span class="m-label" title="${esc(label)}">${esc(label)}</span>
    <span class="meter"><i class="${cls || ''}" style="width:${p}%"></i></span>
    <span class="meter-val">${esc(valText)}</span>
  </div>`;
}

async function loadRealtime() {
  const r = await api('/api/system/realtime');
  renderRealtime(r);
}

function renderRealtime(r) {
  const body = $('#realtime-body');
  const cpu = r.cpu_percent != null ? Number(r.cpu_percent) : null;
  const memPct = (r.mem_total_kb && r.mem_avail_kb != null)
    ? (1 - r.mem_avail_kb / r.mem_total_kb) * 100 : null;
  const memText = memPct != null
    ? `${memPct.toFixed(1)}% · ${fmtBytes((r.mem_total_kb - r.mem_avail_kb) * 1024)}/${fmtBytes(r.mem_total_kb * 1024)}`
    : '—';
  const load = Array.isArray(r.load) ? r.load.map(v => Number(v).toFixed(2)).join(' ') : '—';
  const ioRows = (r.io || []).map(d => `
    <tr>
      <td>${esc(d.name)}</td>
      <td>${esc(fmtRate(d.read_bps))}</td>
      <td>${esc(fmtRate(d.write_bps))}</td>
      <td>${d.iops != null ? esc(d.iops) : '—'}</td>
      <td>${d.ios_in_progress != null ? esc(d.ios_in_progress) : '—'}</td>
    </tr>`).join('');
  body.innerHTML = `
    <div class="meter-rows">
      ${meterHtml('CPU', cpu, cpu != null ? cpu.toFixed(1) + '%' : '—')}
      ${meterHtml('内存', memPct, memText)}
    </div>
    <div class="rt-kv">
      <div class="item"><div class="v">${esc(load)}</div><div class="k">负载 1/5/15 分钟</div></div>
      <div class="item"><div class="v">${esc(fmtUptime(r.uptime_seconds))}</div><div class="k">运行时间</div></div>
    </div>
    ${(r.io || []).length ? `<table class="io-table">
      <thead><tr><th>设备</th><th>读</th><th>写</th><th>IOPS</th><th>队列</th></tr></thead>
      <tbody>${ioRows}</tbody></table>` : ''}`;
  const now = new Date();
  $('#rt-updated').textContent = `实时 · ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

/* ---------- 文件系统使用率 ---------- */
function fsUseClass(pct) {
  if (pct >= 90) return 'lv-crit';
  if (pct >= 75) return 'lv-warn';
  return 'lv-ok';
}

async function loadFsUsage() {
  let data;
  try { data = await api('/api/storage/usage'); }
  catch (e) {
    $('#fs-usage-body').innerHTML = `<div class="loading-line">加载失败：${esc(e.message)}</div>`;
    return;
  }
  state.fsUsage = data.filesystems || [];
  renderFsCard();
  renderFsTable();
}

function renderFsCard() {
  const list = state.fsUsage || [];
  const body = $('#fs-usage-body');
  if (!list.length) { body.innerHTML = '<div class="loading-line">暂无文件系统数据</div>'; return; }
  body.innerHTML = '<div class="meter-rows">' + list.map(fs => {
    const pct = Number(fs.use_percent) || 0;
    const label = `${fs.mountpoint || fs.device}`;
    const val = `${pct}% · 可用 ${fmtBytes(fs.avail)}`;
    return `<div title="${esc(fs.device)} · ${esc(fs.fstype)} · 共 ${fmtBytes(fs.size)}">
      ${meterHtml(label, pct, val, fsUseClass(pct))}</div>`;
  }).join('') + '</div>';
}

function renderFsTable() {
  const tb = $('#fs-table tbody');
  const list = state.fsUsage;
  if (!list) return;
  if (!list.length) { tb.innerHTML = '<tr><td colspan="7" class="muted">暂无文件系统数据</td></tr>'; return; }
  tb.innerHTML = '';
  list.forEach(fs => {
    const pct = Number(fs.use_percent) || 0;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="num">${esc(fs.device || '—')}</td>
      <td class="num">${esc(fs.mountpoint || '—')}</td>
      <td>${esc(fs.fstype || '—')}</td>
      <td class="num">${fmtBytes(fs.size)}</td>
      <td class="num">${fmtBytes(fs.used)}</td>
      <td><span class="use-cell">
        <span class="meter"><i class="${fsUseClass(pct)}" style="width:${Math.min(100, pct)}%"></i></span>
        <span class="meter-val">${pct}%</span>
      </span></td>
      <td class="ops"></td>`;
    const ops = tr.querySelector('.ops');
    if (state.isAdmin) {
      const add = document.createElement('button');
      add.className = 'btn sm';
      add.textContent = '写入 fstab';
      add.addEventListener('click', () => fstabAction('add', fs));
      ops.appendChild(add);
      const rm = document.createElement('button');
      rm.className = 'btn sm danger';
      rm.style.marginLeft = '6px';
      rm.textContent = '从 fstab 移除';
      rm.addEventListener('click', () => fstabAction('remove', fs));
      ops.appendChild(rm);
    } else {
      ops.innerHTML = '<span class="tiny">—</span>';
    }
    tb.appendChild(tr);
  });
}

function fstabAction(action, fs) {
  const isAdd = action === 'add';
  const body = isAdd
    ? { action: 'add', device: fs.device, mountpoint: fs.mountpoint, fstype: fs.fstype }
    : { action: 'remove', mountpoint: fs.mountpoint };
  const desc = isAdd
    ? `将 ${fs.device}（${fs.fstype}）的挂载点 ${fs.mountpoint} 写入 /etc/fstab，系统重启后将自动挂载。`
    : `从 /etc/fstab 移除挂载点 ${fs.mountpoint} 的条目，重启后将不再自动挂载（不影响当前已挂载状态）。`;
  confirmModal(isAdd ? '写入 fstab' : '从 fstab 移除', `<p>${esc(desc)}</p>`,
    isAdd ? '写入' : '移除', !isAdd, async () => {
      const r = await api('/api/storage/fstab', { method: 'POST', body });
      if (r && r.ok === false) throw new Error(r.error || '操作失败');
      toast(isAdd ? '已写入 /etc/fstab' : '已从 /etc/fstab 移除', 'ok');
    });
}

/* ---------- NFS 共享管理 ---------- */
async function loadNfs() {
  const tb = $('#nfs-table tbody');
  tb.innerHTML = '<tr><td colspan="4" class="muted">加载中…</td></tr>';
  let data;
  try { data = await api('/api/nfs/exports'); }
  catch (e) { tb.innerHTML = `<tr><td colspan="4" class="muted">加载失败：${esc(e.message)}</td></tr>`; return; }
  state.nfsLoaded = true;
  $('#nfs-unavailable').classList.toggle('hidden', !!data.available);
  $('#nfs-form').classList.toggle('hidden', !state.isAdmin || !data.available);
  $$('#nfs-table .admin-col').forEach(el => el.classList.toggle('col-hidden', !state.isAdmin));

  const rows = [];
  (data.exports || []).forEach(ex => {
    (ex.clients || []).forEach(c => rows.push({ path: ex.path, host: c.host, options: c.options }));
  });
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="4" class="muted">暂无 NFS 共享</td></tr>'; return; }
  tb.innerHTML = '';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="num">${esc(r.path)}</td>
      <td class="num">${esc(r.host)}</td>
      <td class="num">${esc(r.options || '—')}</td>
      <td class="ops admin-col ${state.isAdmin ? '' : 'col-hidden'}"></td>`;
    if (state.isAdmin) {
      const ops = tr.querySelector('.ops');
      const del = document.createElement('button');
      del.className = 'btn sm danger';
      del.textContent = '删除';
      del.addEventListener('click', () => removeNfs(r));
      ops.appendChild(del);
    }
    tb.appendChild(tr);
  });
}

function removeNfs(r) {
  confirmModal('删除 NFS 共享',
    `<p>将从 /etc/exports 移除以下共享并立即生效：</p>
     <p><strong class="mono">${esc(r.path)}</strong> → <strong class="mono">${esc(r.host)}</strong>（${esc(r.options || '默认')}）</p>
     <p class="warn-text">正在使用该共享的客户端将立即无法访问，请确认后再执行。</p>`,
    '确认删除', true, async () => {
      const resp = await api('/api/nfs/exports/delete', { method: 'POST', body: { path: r.path, host: r.host } });
      if (resp && resp.ok === false) throw new Error(resp.error || '删除失败');
      toast('NFS 共享已删除', 'ok');
      await loadNfs();
    });
}

function addNfs() {
  const path = $('#nfs-path').value.trim();
  const host = $('#nfs-host').value.trim() || '*';
  const options = [$('#nfs-perm').value];
  if ($('#nfs-opt-async').checked) options.push('async');
  if ($('#nfs-opt-nrs').checked) options.push('no_root_squash');
  if ($('#nfs-opt-allsquash').checked) options.push('all_squash');
  if (!path) { toast('请填写共享路径', 'error'); return; }
  confirmModal('添加 NFS 共享',
    `<p>将写入 /etc/exports 并立即生效：</p>
     <p><strong class="mono">${esc(path)}</strong> → <strong class="mono">${esc(host)}</strong>（${esc(options.join(','))}）</p>
     <p class="warn-text">客户端将能够以${$('#nfs-perm').value === 'rw' ? '读写' : '只读'}方式访问该目录，请确认路径和客户端范围正确。</p>`,
    '确认添加', false, async () => {
      const resp = await api('/api/nfs/exports', { method: 'POST', body: { path, host, options } });
      if (resp && resp.ok === false) throw new Error(resp.error || '添加失败');
      toast('NFS 共享已添加', 'ok');
      $('#nfs-path').value = '';
      await loadNfs();
    });
}

/* ---------- 控制器事件 ---------- */
async function loadCtlEvents() {
  const pre = $('#ctl-pre');
  try {
    const d = await api('/api/controller_events?lines=' + state.ctlLines);
    pre.textContent = d.output || '（无输出）';
    $('#ctl-total').textContent = d.total_lines != null ? `共 ${d.total_lines} 行` : '';
  } catch (e) {
    pre.textContent = '加载失败：' + e.message;
  }
}

/* ---------- 整盘初始化 ---------- */
function subtreeMounted(dev) {
  if ((dev.mountpoints || []).length) return true;
  return (dev.children || []).some(subtreeMounted);
}

function initDiskDialog(dev) {
  const name = dev.name || '';
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <p>对 <strong class="mono">${esc(dev.path || name)}</strong> 执行整盘初始化：创建 GPT 分区表、单个主分区并格式化为所选文件系统。</p>
    <div class="field"><label>文件系统</label>
      <select class="select" id="init-fs" style="width:100%">
        <option value="ext4">ext4</option>
        <option value="xfs">xfs</option>
      </select></div>
    <div class="field"><label>挂载点（可选，留空则不挂载）</label>
      <input class="input mono" id="init-mp" placeholder="/mnt/data" /></div>
    <label class="persist-row"><input type="checkbox" id="init-persist" /> 写入 /etc/fstab 持久挂载（需填写挂载点）</label>
    <p class="warn-text">警告：整盘数据将被清除，且不可恢复！</p>
    <div class="field"><label class="confirm-input-note">请输入设备名 <strong class="mono">${esc(name)}</strong> 以确认操作</label>
      <input class="input mono" id="init-confirm" placeholder="${esc(name)}" autocomplete="off" /></div>`;
  showModal({
    title: '整盘初始化', body: wrap,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '确认初始化', cls: 'danger',
        handler: async (btn) => {
          const confirmName = wrap.querySelector('#init-confirm').value.trim();
          if (confirmName !== name) { toast('请输入正确的设备名以确认', 'error'); return; }
          const mp = wrap.querySelector('#init-mp').value.trim();
          const persist = wrap.querySelector('#init-persist').checked;
          if (persist && !mp) { toast('勾选持久挂载时必须填写挂载点', 'error'); return; }
          const body = { device: dev.path || ('/dev/' + name), fs_type: wrap.querySelector('#init-fs').value };
          if (mp) body.mountpoint = mp;
          if (persist) body.persist = true;
          btnLoading(btn, true);
          try {
            const r = await api('/api/storage/init_disk', { method: 'POST', body });
            closeModal();
            showInitResult(r, dev);
            if (r && r.ok) {
              toast(`磁盘 ${name} 初始化完成`, 'ok');
              await loadStorage();
              await loadFsUsage();
            }
          } catch (e) {
            toast(e.message, 'error');
          } finally {
            btnLoading(btn, false);
          }
        }
      }
    ]
  });
}

function showInitResult(r, dev) {
  const steps = (r && r.steps) || [];
  const ok = r && r.ok;
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <p>${ok
      ? `设备 <strong class="mono">${esc(dev.path || dev.name)}</strong> 初始化完成${r.partition ? '，分区：<strong class="mono">' + esc(r.partition) + '</strong>' : ''}。`
      : `<span class="warn-text">初始化失败：${esc((r && r.error) || '未知错误')}</span>`}</p>
    ${steps.length ? `<ul class="steps-list">${steps.map(s => {
      const isOk = /^ok\b/i.test(String(s).trim());
      const text = String(s).replace(/^ok\s*/i, '');
      return `<li class="${isOk ? 'ok' : 'fail'}">${esc(text)}</li>`;
    }).join('')}</ul>` : ''}`;
  showModal({
    title: '初始化执行结果', body: wrap,
    actions: [{ label: '关闭', cls: 'primary', handler: closeModal }]
  });
}

boot();
