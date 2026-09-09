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

function cssToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* 数字滚动（自然缓动） */
function countUp(el, target, duration) {
  if (!el || target == null || isNaN(target)) return;
  duration = duration || 850;
  if (el.__countRaf) cancelAnimationFrame(el.__countRaf);
  const from = parseFloat(el.textContent) || 0;
  const start = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = String(Math.round(from + (target - from) * eased));
    if (p < 1) el.__countRaf = requestAnimationFrame(step);
    else el.__countRaf = null;
  };
  el.__countRaf = requestAnimationFrame(step);
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
  vdExpanded: new Set(), // 已展开成员磁盘的 VD（键为 vd 编号字符串）
  fsUsage: null,
  showHiddenFs: false,
  ctlLines: 100,
  ctlQuery: '',
  storageLoaded: false,
  storageExpanded: new Set(),
  bcacheLoaded: false,
  opsLoaded: false,
  bayCapacity: 'auto',
  bayLayout: {},
  usersLoaded: false,
  nfsLoaded: false,
  refreshTimer: null,
  realtimeTimer: null,
  raidSel: new Set(), // 勾选用于创建阵列的磁盘，键为 "eid:slot"
};

const HEALTH_TEXT = { ok: '正常', warn: '警告', crit: '严重', unknown: '未知' };
const BADGE_OK_STATES = ['onln', 'optl', 'optimal', 'ok', 'online', 'good', 'ugood', 'jbod', 'ghs', 'dhs'];
const BADGE_CRIT_STATES = ['ubad', 'failed', 'fail', 'degraded', 'dead', 'offline', 'offlin', 'offln', 'missing'];

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
const PREDICT_TEXT = { ok: '正常', info: '关注', warn: '警告', crit: '高危' };
function predictReasons(p) {
  const reasons = (p && Array.isArray(p.reasons)) ? p.reasons : [];
  return reasons.length ? reasons.map(r => r.text) : ['各项关键指标正常，未发现故障前兆'];
}
function predictBadge(p) {
  const lv = (p && p.level) || 'ok';
  return `<span class="badge ${lv}" title="${esc(predictReasons(p).join('\n'))}">${PREDICT_TEXT[lv] || esc(lv)}</span>`;
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
  const tb = $('#btn-theme');
  tb.textContent = t === 'dark' ? '☀' : '◐';
  tb.setAttribute('aria-label', t === 'dark' ? '切换到浅色主题' : '切换到深色主题');
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
    if (me.auth_required && !me.logged_in) {
      $('#login-sub').textContent = me.auth_mode === 'pam'
        ? '使用服务器系统账号登录'
        : '登录以查看控制器、磁盘与存储状态';
      showLogin();
      return;
    }
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
  $('#nav-users').classList.toggle('hidden', !(me.manage_users && state.isAdmin));
  // 未创建管理员账号时的安全提示横幅
  $('#security-banner').classList.toggle('hidden', state.authRequired);
  $('#btn-collect').classList.toggle('hidden', !state.isAdmin);
  $('#btn-vd-import').classList.toggle('hidden', !state.isAdmin); // 是否有外来配置由 loadStatus 动态控制
  $('#btn-logout').classList.toggle('hidden', !state.authRequired);
  $('#sel-interval').disabled = !state.isAdmin;
  $('#btn-alert-save').disabled = !state.isAdmin;
  $('#btn-alert-test').disabled = !state.isAdmin;
  $('#btn-webhook-test').disabled = !state.isAdmin;
  $('#btn-hotspare-save').classList.toggle('hidden', !state.isAdmin);
  const name = me.username || 'admin';
  $('#user-name').textContent = name + (state.authRequired ? '' : '（未认证）');
  $('#user-avatar').textContent = (name[0] || '?');
  $('#app-version').textContent = me.version ? 'v' + me.version : '';
  let savedView = 'overview';
  try {
    const sv = localStorage.getItem('lsi-view');
    if (['overview', 'storage', 'logs', 'ops', 'users'].includes(sv)) savedView = sv;
  } catch (e) { /* 忽略 */ }
  if (savedView === 'users' && !(me.manage_users && state.isAdmin)) savedView = 'overview';
  switchView(savedView);
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
    loadAlarm(), loadJbod(),
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
  renderNvmeDisks(st);
  renderSystem(st);
  // 控制器检测到外来配置时显示「载入外部配置」按钮
  const hasForeign = !!(st.controller && st.controller.foreign_present);
  const btnImport = $('#btn-vd-import');
  btnImport.textContent = hasForeign
    ? `载入外部配置${st.controller.foreign_count > 1 ? `（${st.controller.foreign_count}）` : ''}`
    : '载入外部配置';
  btnImport.classList.toggle('hidden', !state.isAdmin || !hasForeign);
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
  const toneVar = total >= 80 ? '--ok' : total >= 60 ? '--warn' : '--crit';
  ring.style.stroke = cssToken(toneVar) || '#4f8cff';
  requestAnimationFrame(() => {
    ring.style.strokeDashoffset = String(C * (1 - total / 100));
  });
  countUp($('#health-score'), total);
  const hb = $('#health-badge');
  hb.className = 'badge ' + (total >= 80 ? 'ok' : total >= 60 ? 'warn' : 'crit');
  hb.textContent = total >= 80 ? '健康' : total >= 60 ? '需要关注' : '存在风险';
  $('#sub-scores').innerHTML = subs.map((s, i) => `
    <div class="sub-score" style="--d:${i}">
      <span>${esc(s.name)}</span>
      <span class="bar"><i style="width:${s.score}%;background:${scoreColor(s.score)}"></i></span>
      <span class="pct">${s.score}</span>
    </div>`).join('');
}

/* ---------- 阵列卡报警（蜂鸣器）开关 ---------- */
const ALARM_STATE_MAP = [
  ['SILENC', ['临时关闭', 'warn']],
  ['OFF', ['永久关闭', 'crit']],
  ['ON', ['打开', 'ok']],
];

async function loadAlarm() {
  const badge = $('#alarm-badge');
  let data;
  try { data = await api('/api/controller_alarm'); }
  catch (e) { badge.textContent = '—'; return; }
  const raw = String(data.alarm || '').toUpperCase();
  const hit = ALARM_STATE_MAP.find(([k]) => raw.includes(k));
  badge.className = 'badge ' + (hit ? hit[1][1] : '');
  badge.textContent = hit ? hit[1][0] : (raw || '未知');
  $('#alarm-actions').classList.toggle('hidden', !state.isAdmin);
}

function alarmAction(mode) {
  const desc = {
    on: '打开阵列卡蜂鸣器报警，出现异常事件时会鸣叫。',
    silence: '临时关闭当前报警鸣叫；出现新的异常事件时会再次鸣叫（重启后也会恢复）。',
    off: '永久关闭阵列卡蜂鸣器报警，出现异常事件也不再鸣叫，直到重新打开。',
  }[mode];
  const label = { on: '打开', silence: '临时关闭', off: '永久关闭' }[mode];
  confirmModal(`阵列卡报警${label}`, `<p>${esc(desc)}</p>`, label, mode === 'off', async () => {
    const r = await api('/api/controller_alarm', { method: 'POST', body: { mode } });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(`阵列卡报警已${label}`, 'ok');
    await loadAlarm();
  });
}

/* ---------- JBOD 模式开关 ---------- */
async function loadJbod() {
  const badge = $('#jbod-badge');
  let data;
  try { data = await api('/api/controller_jbod'); }
  catch (e) { badge.textContent = '—'; return; }
  const raw = String(data.jbod || '').toUpperCase();
  const on = raw === 'ON';
  badge.className = 'badge ' + (raw ? (on ? 'ok' : '') : '');
  badge.textContent = raw ? (on ? '已打开' : '已关闭') : '未知';
  $('#jbod-actions').classList.toggle('hidden', !state.isAdmin);
}

function jbodAction(mode) {
  const on = mode === 'on';
  const desc = on
    ? '打开 JBOD 模式后，新插入的未配置磁盘将直接作为 JBOD 盘暴露给操作系统，可被系统直接识别使用。'
    : '关闭 JBOD 模式后，新插入的未配置磁盘将保持 UGood 状态，不再自动暴露给操作系统，需配置 RAID 后才能使用。已在线的 JBOD 盘不受此开关影响。';
  const label = on ? '打开' : '关闭';
  confirmModal(`JBOD 模式${label}`, `<p>${esc(desc)}</p>`, label, !on, async () => {
    const r = await api('/api/controller_jbod', { method: 'POST', body: { mode } });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(`JBOD 模式已${label}`, 'ok');
    await loadJbod();
  });
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
      label: '阵列卡温度',
      value: c.roc_temp != null ? `${Number(c.roc_temp)}°C` : '—',
      sub: avgT != null ? `磁盘均温 ${avgT.toFixed(1)}°C · 最高 ${maxT}°C（${esc(maxDisk)}）` : '无磁盘温度数据',
    },
    {
      label: 'BBU 状态',
      value: `<span style="font-size:15px">${c.bbu_state ? stateBadge(c.bbu_state) : '<span class="muted">—</span>'}</span>`,
      sub: `${esc(c.bbu_model || '无 BBU')}${c.bbu_temperature != null ? ' · ' + esc(c.bbu_temperature) + '°C' : ''}`,
    },
  ];
  $('#stat-cards').innerHTML = cards.map((k, i) => `
    <div class="card" style="--d:${i}">
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
  disks.forEach((d, idx) => {
    const tone = stateTone(d.state);
    const tTone = tempTone(Number(d.temperature));
    const cls = tone === 'crit' ? 'st-crit' : (tTone || tone) === 'warn' ? 'st-warn' : tone === 'ok' ? 'st-ok' : 'st-unknown';
    const cell = document.createElement('button');
    cell.className = 'topo-cell ' + cls;
    cell.style.setProperty('--d', idx);
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

function opLabel(op) {
  const map = {
    'Migrate': '迁移/扩容',
    'Reconstruction': '重建',
    'Consistency Check': '一致性检查',
    'Initialization': '初始化',
    'Copyback': '回拷',
    'Erase': '安全擦除',
  };
  return map[op] || op || '—';
}

function currentOpDisplay(v) {
  const mg = v.migrate_progress != null && v.migrate_progress > 0;
  if (mg) {
    const name = opLabel(v.migrate_operation || 'Migrate');
    return `<span class="op-cell">
      <span class="op-name">${esc(name)}</span>
      <span class="migrate-bar"><i style="width:${v.migrate_progress}%"></i></span>
      <span class="migrate-pct">${v.migrate_progress}%</span>
      ${v.migrate_eta ? `<span class="migrate-eta">${esc(v.migrate_eta)}</span>` : ''}
    </span>`;
  }
  const raw = String(v.current_operation || 'None');
  if (!raw || raw === 'None') return '<span class="muted">—</span>';
  const m = raw.match(/^([^(]+?)\s*(?:\((\d+)%\))?/);
  const name = opLabel((m && m[1] || raw).trim());
  const pct = m && m[2] ? m[2] : null;
  return `<span class="op-cell">
    <span class="op-name">${esc(name)}</span>
    ${pct != null ? `<span class="migrate-pct">${pct}%</span>` : ''}
  </span>`;
}

function renderVirtualDisks() {
  const vds = state.vds || [];
  const tb = $('#vd-table tbody');
  const hideOps = !state.isAdmin;
  $$('#vd-table .admin-col').forEach(el => el.classList.toggle('col-hidden', hideOps));
  if (!vds.length) { tb.innerHTML = '<tr><td colspan="9" class="muted">无虚拟磁盘</td></tr>'; return; }
  tb.innerHTML = '';
  vds.forEach(v => {
    const key = String(v.vd);
    const expanded = state.vdExpanded.has(key);
    const disks = Array.isArray(v.disks) ? v.disks : [];
    const tr = document.createElement('tr');
    tr.className = 'vd-row';
    tr.title = expanded ? '点击收起成员磁盘' : '点击展开成员磁盘';
    const opHtml = currentOpDisplay(v);
    tr.innerHTML = `
      <td class="num"><span class="vd-caret">${expanded ? '▾' : '▸'}</span>${esc(v.dg_vd || '—')} <span class="tiny">(${disks.length} 盘)</span></td>
      <td>${esc(v.name || '—')}</td>
      <td>${esc(v.type || '—')}</td>
      <td class="num">${esc(v.size || '—')}</td>
      <td>${stateBadge(v.state)}</td>
      <td class="num">${esc(v.os_device || '—')}</td>
      <td class="num" title="当前 Cache: ${esc(v.write_cache_raw || '—')} · 初始设置: ${esc(v.write_cache_initial || '—')}">${esc(v.write_cache || '—')}</td>
      <td>${opHtml}</td>
      <td class="ops admin-col ${hideOps ? 'col-hidden' : ''}"></td>`;
    tr.addEventListener('click', () => {
      if (state.vdExpanded.has(key)) state.vdExpanded.delete(key);
      else state.vdExpanded.add(key);
      renderVirtualDisks();
    });
    if (state.isAdmin) {
      const ops = tr.querySelector('.ops');
      ops.addEventListener('click', ev => ev.stopPropagation());
      const sel = document.createElement('select');
      sel.className = 'select';
      sel.innerHTML = `
        <option value="">操作…</option>
        <option value="init_start">初始化开始</option>
        <option value="init_stop">初始化停止</option>
        <option value="cc_start">CC 开始</option>
        <option value="cc_stop">CC 停止</option>
        <option value="expand">容量扩容（加盘）</option>
        <option value="vd_delete">删除</option>`;
      sel.addEventListener('change', () => {
        const action = sel.value;
        sel.value = '';
        if (action === 'expand') expandVd(v);
        else if (action) vdAction(v, action);
      });
      ops.appendChild(sel);
    }
    tb.appendChild(tr);
    if (expanded) {
      const dr = document.createElement('tr');
      dr.className = 'vd-disks-row';
      const rowsHtml = disks.map(p => `
        <tr>
          <td class="num">${esc(p.slot || '—')}</td>
          <td class="num">${esc(p.did !== '' && p.did != null ? String(p.did) : '—')}</td>
          <td>${stateBadge(p.state)}</td>
          <td class="num">${esc(p.size || '—')}</td>
          <td class="num">${esc(p.intf || '—')} / ${esc(p.med || '—')}</td>
          <td>${esc(p.model || '—')}</td>
        </tr>`).join('');
      dr.innerHTML = `<td colspan="9"><div class="vd-sub">
        <div class="tiny" style="margin-bottom:4px">成员磁盘（${disks.length}）</div>
        <table class="data">
          <thead><tr><th>槽位</th><th>DID</th><th>状态</th><th>容量</th><th>接口/介质</th><th>型号</th></tr></thead>
          <tbody>${rowsHtml || '<tr><td colspan="6" class="muted">无成员磁盘数据</td></tr>'}</tbody>
        </table></div></td>`;
      tb.appendChild(dr);
    }
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
  vd_delete: { target: 'vd_delete', action: 'delete', text: '删除', danger: true,
    desc: '删除该虚拟盘会销毁阵列配置并导致数据丢失，且不可恢复。' },
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

function expandVd(v) {
  const raid = RAID_TYPE_MAP[String(v.type || '').toUpperCase()] || 'r5';
  const eligible = (state.status && state.status.physical_disks || [])
    .filter(d => d.state === 'UGood');
  if (!eligible.length) {
    toast('没有可加入阵列的 UGood 盘', 'error');
    return;
  }
  const opts = eligible.map(d =>
    `<option value="${esc(d.eid)}:${esc(d.slot)}">${esc(d.label)} · ${esc(d.model || '')}</option>`
  ).join('');
  showModal({
    title: `VD ${v.vd} 动态扩容`,
    body: `<p>目标阵列：<strong class="mono">${esc(v.dg_vd || ('VD ' + v.vd))}</strong> · ${esc(v.type || '')}</p>
      <p>向该阵列在线加入一块 UGood 盘（同级别容量扩容）：</p>
      <div class="field"><label for="expand-drive">选择磁盘</label>
        <select class="select" id="expand-drive">${opts}</select></div>
      <p class="warn-text">扩容会执行在线迁移，耗时较长；期间请勿断电、拔盘或重启。</p>`,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '开始扩容',
        cls: 'primary',
        handler: async (btn) => {
          const [eid, slot] = $('#expand-drive').value.split(':').map(Number);
          btnLoading(btn, true);
          try {
            const r = await api('/api/raid_expand', {
              method: 'POST',
              body: { vd: v.vd, raid, drives: [{ eid, slot }] },
            });
            if (r && r.ok === false) throw new Error(r.error || '扩容失败');
            toast('动态扩容已启动', 'ok');
            closeModal();
            await loadVdDetail();
            await loadStatus();
          } catch (e) {
            toast(e.message, 'error');
          } finally {
            btnLoading(btn, false);
          }
        },
      },
    ],
  });
}

/* ---------- 物理磁盘 ---------- */
// 仅未配置（UGood/JBOD）的磁盘允许勾选创建阵列；
// 已加入阵列（Onln 等）或异常状态的磁盘一律禁止勾选，避免误伤现有数据
function pdRaidEligible(d) {
  return d.state === 'UGood' || d.state === 'JBOD';
}

function bgProgressCell(d, key) {
  const p = Number(d[key + '_progress']);
  if (d[key + '_progress'] == null || d[key + '_progress'] === '' || isNaN(p)) return '';
  const pct = Math.max(0, Math.min(100, p));
  const eta = d[key + '_eta'];
  const titles = { rebuild: 'Rebuild 重建中', copyback: 'Copyback 回拷中', erase: 'Erase 安全擦除中' };
  const title = titles[key] || key;
  const cls = 'rebuild-progress' + (key !== 'rebuild' ? ' ' + key : '');
  return `<div class="${cls}" title="${title}">
    <div class="rb-bar"><i style="width:${pct}%"></i></div>
    <span class="rb-pct">${pct}%</span>${eta ? `<span class="rb-eta">${esc(eta)}</span>` : ''}
  </div>`;
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
    tb.innerHTML = '<tr><td colspan="14" class="muted">无物理磁盘</td></tr>';
    updateRaidBar();
    return;
  }
  const foreignBadge = ' <span class="badge warn" title="外来阵列配置：该盘带有其它系统的阵列元数据，可在顶部通过「载入外部配置」导回原阵列">F</span>';
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
      <td class="num">${d.dg != null ? esc(d.dg) : '—'}${String(d.dg).toUpperCase() === 'F' ? foreignBadge : ''}</td>
      <td>${stateBadge(d.state)}${bgProgressCell(d, 'rebuild')}${bgProgressCell(d, 'copyback')}${bgProgressCell(d, 'erase')}</td>
      <td class="num">${fmtTemp(d.temperature)}</td>
      <td class="num">${num0(d.media_error)}/${num0(d.other_error)}/${num0(d.predictive_failure)}</td>
      <td class="num">${num0(d.reallocated)}/${num0(d.pending)}/${num0(d.uncorrectable)}</td>
      <td class="num">${fmtHours(d.power_on_hours)}</td>
      <td>${alerts.join(' ') || '<span class="tiny">—</span>'}</td>
      <td>${predictBadge(d.prediction)}</td>
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
        <option value="locate_stop">定位关</option>
        <option value="hotspare_global">设为全局热备</option>
        <option value="hotspare_dedicated">设为专用热备</option>
        <option value="hotspare_delete">删除热备</option>
        <option value="copyback_start">启动 Copyback</option>
        <option value="copyback_stop">停止 Copyback</option>
        <option value="erase">安全擦除 (Erase)</option>
        <option value="erase_stop">停止 Erase</option>`;
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

/* ---------- NVMe 磁盘 ---------- */
function renderNvmeDisks(st) {
  const disks = st.nvme_disks || [];
  const tb = $('#nvme-table tbody');
  if (!disks.length) {
    tb.innerHTML = '<tr><td colspan="12" class="muted">无 NVMe 磁盘</td></tr>';
    return;
  }
  tb.innerHTML = '';
  disks.forEach(d => {
    const cw = String(d.critical_warning || '');
    const cwWarn = cw && cw !== '0x00' && cw !== '0x0';
    const alerts = [];
    if (cwWarn) alerts.push(`<span class="badge crit" title="Critical Warning">SMART</span>`);
    if (Number(d.media_errors) > 0) alerts.push('<span class="badge crit">介质错误</span>');
    if (d.available_spare != null && Number(d.available_spare) <= 10) alerts.push('<span class="badge warn">备用空间低</span>');
    if (d.percentage_used != null && Number(d.percentage_used) >= 90) alerts.push('<span class="badge warn">寿命将尽</span>');
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="num">${esc(d.device || '—')}</td>
      <td>${esc(d.model || '—')}</td>
      <td class="num">${esc(d.serial || '—')}</td>
      <td class="num">${esc(d.firmware || '—')}</td>
      <td class="num">${esc(d.size || '—')}</td>
      <td class="num">${esc(d.used || '—')}</td>
      <td class="num">${fmtTemp(d.temperature)}</td>
      <td class="num">${d.available_spare != null ? esc(d.available_spare) + '%' : '—'}</td>
      <td class="num">${d.percentage_used != null ? esc(d.percentage_used) + '%' : '—'}</td>
      <td class="num">${fmtHours(d.power_on_hours)}</td>
      <td class="num">${num0(d.media_errors)}</td>
      <td>${alerts.join(' ') || '<span class="tiny">—</span>'}</td>`;
    tb.appendChild(tr);
  });
}
function num0(v) { return v != null && !isNaN(v) ? esc(v) : '0'; }

const DISK_ACTION_TEXT = {
  online: ['上线', '将磁盘上线。'],
  offline: ['下线', '将磁盘下线会使其脱离磁盘组，可能导致虚拟盘降级甚至数据不可用。'],
  good: ['置为 UGood', '将磁盘置为 UGood 未配置状态，磁盘上的阵列配置信息可能被清除。'],
  jbod: ['置为 JBOD', '将磁盘置为 JBOD 直通模式，磁盘上的阵列配置信息可能被清除。'],
  locate_start: ['定位开', '点亮磁盘定位指示灯。'],
  locate_stop: ['定位关', '熄灭磁盘定位指示灯。'],
  hotspare_global: ['设为全局热备', '将该磁盘设为全局热备盘，可接管任意磁盘组的故障盘。'],
  hotspare_dedicated: ['设为专用热备', '将该磁盘设为指定磁盘组的专用热备盘，仅接管该磁盘组的故障盘。'],
  hotspare_delete: ['删除热备', '移除该磁盘的热备盘属性。'],
  copyback_start: ['启动 Copyback', '将当前盘的数据回拷到指定目标盘（替换盘）。请确认目标盘已插入且状态正确。'],
  copyback_stop: ['停止 Copyback', '停止当前盘正在进行的 Copyback 回拷操作。'],
  erase: ['安全擦除 (Erase)', '通过控制器对该盘执行安全擦除，盘上所有数据将被彻底销毁且不可恢复。'],
  erase_stop: ['停止 Erase', '停止当前盘正在进行的 Erase 安全擦除操作。'],
};
const DANGER_ACTIONS = ['offline', 'good', 'jbod'];

function diskAction(d, action) {
  const [text, desc] = DISK_ACTION_TEXT[action] || [action, ''];
  const label = d.label || ('E' + d.eid + ':S' + d.slot);
  const doIt = async (extraBody) => {
    const r = await api('/api/disk_action', { method: 'POST', body: { eid: d.eid, slot: d.slot, action, ...(extraBody || {}) } });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(`磁盘 ${label}：${text} 已执行`, 'ok');
    await loadStatus();
  };
  if (action === 'hotspare_dedicated') {
    confirmModal(`${text}`,
      `<p>磁盘 <strong class="mono">${esc(label)}</strong>（${esc(d.model || '')}）</p>
       <p>${esc(desc)}</p>
       <div class="field"><label for="hs-dg">目标磁盘组 (DG) 编号</label>
       <input class="input" id="hs-dg" type="number" min="0" value="0" /></div>`,
      '确认执行', false, async () => {
        const v = ($('#hs-dg').value || '').trim();
        if (!/^\d+$/.test(v)) throw new Error('请输入有效的 DG 编号');
        await doIt({ dg: Number(v) });
      });
    return;
  }
  if (action === 'copyback_start') {
    confirmModal(`${text}`,
      `<p>源盘 <strong class="mono">${esc(label)}</strong>（${esc(d.model || '')}）</p>
       <p>${esc(desc)}</p>
       <div class="field"><label for="cb-target">目标盘 (e:s)</label>
       <input class="input" id="cb-target" type="text" placeholder="例如 134:2" /></div>`,
      '确认执行', false, async () => {
        const v = ($('#cb-target').value || '').trim();
        const m = v.match(/^(\d+):(\d+)$/);
        if (!m) throw new Error('请输入有效的目标盘 e:s（如 134:2）');
        await doIt({ target_eid: Number(m[1]), target_slot: Number(m[2]) });
      });
    return;
  }
  if (action === 'erase') {
    confirmModal(`危险操作：${text}`,
      `<p>磁盘 <strong class="mono">${esc(label)}</strong>（${esc(d.model || '')}）</p>
       <p class="warn-text">${esc(desc)} 请再次核对盘位，该操作不可撤销。</p>
       <div class="field"><label for="erase-pattern">擦除模式</label>
       <select class="select" id="erase-pattern">
         <option value="simple">simple — 单遍快速覆写</option>
         <option value="normal">normal — 三遍覆写</option>
         <option value="threepass">threepass — 三遍（随机→清零→校验）</option>
         <option value="thorough">thorough — 九遍深度覆写</option>
         <option value="crypto">crypto — 加密擦除（仅 SED/ISE 盘）</option>
       </select></div>
       <div class="field"><label for="erase-confirm">输入盘位标识确认（如 ${esc(label)}）</label>
       <input class="input mono" id="erase-confirm" type="text" placeholder="${esc(label)}" autocomplete="off" /></div>
       <label class="check-row"><input type="checkbox" id="erase-ack" />
       <span>我已知晓该盘上的所有数据将被彻底销毁且无法恢复</span></label>`,
      '开始擦除', true, async () => {
        const pattern = $('#erase-pattern').value;
        const typed = ($('#erase-confirm').value || '').trim();
        if (typed !== label) throw new Error(`请输入正确的盘位标识 "${label}"`);
        if (!$('#erase-ack').checked) throw new Error('请先勾选风险确认');
        const r = await api('/api/disk_erase', { method: 'POST', body: { eid: d.eid, slot: d.slot, pattern, confirm: label, acknowledge: true } });
        if (r && r.ok === false) throw new Error(r.error || '启动擦除失败');
        toast(`磁盘 ${label}：安全擦除已启动`, 'ok');
        await loadStatus();
      });
    return;
  }
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
const RAID_TYPE_MAP = {
  RAID0: 'r0', RAID1: 'r1', RAID5: 'r5', RAID6: 'r6',
  RAID10: 'r10', RAID50: 'r50', RAID60: 'r60',
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
  if (name && !/^[\w .-]{1,15}$/.test(name)) {
    toast('阵列名称仅支持字母数字/空格/._-，最长 15 字符', 'error');
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
    ? ['#5f8cff', '#ff8f8f', '#f0b45c', '#58cfd6', '#b39dff', '#58d1a0']
    : ['#2767e8', '#d95f5f', '#d99a2b', '#2a9aa6', '#7a5af8', '#2f9d72'];
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
  const tickColor = css.getPropertyValue('--muted-foreground').trim() || '#7f8b9a';
  const gridColor = css.getPropertyValue('--border').trim() || '#e2e7ee';
  const tooltipBg = css.getPropertyValue('--popover').trim() || '#ffffff';
  const tooltipInk = css.getPropertyValue('--popover-foreground').trim() || '#26303d';
  const tooltipLine = css.getPropertyValue('--border').trim() || '#e2e7ee';
  datasets.forEach(ds => {
    ds.borderWidth = 1.8;
    ds.pointRadius = 0;
    ds.pointHitRadius = 8;
    ds.tension = 0.34;
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
      animation: { duration: 1100, easing: 'easeOutQuart' },
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: {
          labels: {
            color: tickColor,
            boxWidth: 12,
            boxHeight: 2,
            usePointStyle: true,
            pointStyle: 'line',
            font: { family: "'DM Sans', 'PingFang SC', sans-serif", size: 11, weight: '500' },
            padding: 16,
          },
        },
        tooltip: {
          backgroundColor: tooltipBg,
          titleColor: tooltipInk,
          bodyColor: tooltipInk,
          borderColor: tooltipLine,
          borderWidth: 1,
          padding: 11,
          cornerRadius: 10,
          displayColors: true,
          titleFont: { family: "'JetBrains Mono', monospace", size: 11, weight: '600' },
          bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
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
          grid: { color: gridColor, drawTicks: false },
          border: { display: false },
        },
        y: {
          title: { display: true, text: unit, color: tickColor },
          ticks: { color: tickColor },
          grid: { color: gridColor, drawTicks: false },
          border: { display: false },
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
    list.innerHTML = events.map((ev, i) => {
      const lv = String(ev.level || 'info').toLowerCase();
      const cls = lv === 'error' ? 'crit' : lv === 'warning' ? 'warn' : 'info';
      const lvText = lv === 'error' ? '错误' : lv === 'warning' ? '警告' : '信息';
      return `<div class="event-item" style="--d:${i}">
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
  const wh = $('#webhook-badge');
  wh.className = 'badge ' + (cfg.webhook_configured ? 'ok' : '');
  wh.textContent = cfg.webhook_configured ? 'Webhook 已配置' : 'Webhook 未配置';

  const map = [
    ['alert_email_to', '#alert-email'],
    ['sendmail_path', '#alert-sendmail'],
    ['webhook_url', '#alert-webhook'],
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

  const policies = (cfg.config && cfg.config.policies) || {};
  document.querySelectorAll('#alert-policies [data-policy]').forEach(cb => {
    cb.checked = policies[cb.dataset.policy] !== false;
    cb.disabled = !state.isAdmin;
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
      webhook_url: $('#alert-webhook').value.trim(),
      temp_warn: Number($('#alert-warn').value),
      temp_crit: Number($('#alert-crit').value),
      policies: Object.fromEntries(
        Array.from(document.querySelectorAll('#alert-policies [data-policy]'))
          .map(cb => [cb.dataset.policy, cb.checked])
      ),
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

async function testWebhook() {
  const btn = $('#btn-webhook-test');
  btnLoading(btn, true);
  try {
    const r = await api('/api/webhook_test', { method: 'POST' });
    if (r && r.ok === false) throw new Error(r.error || '发送失败');
    toast('测试 Webhook 已发送', 'ok');
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
    ['故障预测', `${PREDICT_TEXT[(d.prediction && d.prediction.level) || 'ok']} — ${predictReasons(d.prediction).join('；')}`],
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
  devices.forEach(dev => {
    const key = dev.path || dev.name;
    const top = createStorageRow(dev, 0);
    tb.appendChild(top);
    const parts = dev.children || [];
    if (parts.length) {
      const subRows = parts.map(ch => createStorageRow(ch, 1));
      subRows.forEach(r => tb.appendChild(r));
      const show = state.storageExpanded.has(key);
      subRows.forEach(r => r.classList.toggle('storage-collapsed', !show));
      top.style.cursor = 'pointer';
      top.addEventListener('click', (ev) => {
        if (ev.target.closest('.ops')) return;
        const isOpen = state.storageExpanded.has(key);
        if (isOpen) state.storageExpanded.delete(key);
        else state.storageExpanded.add(key);
        subRows.forEach(r => {
          r.classList.toggle('storage-collapsed', isOpen);
          r.classList.remove('storage-row-anim');
          void r.offsetWidth;
          r.classList.add('storage-row-anim');
        });
        const caret = top.querySelector('.tree-caret');
        if (caret) caret.textContent = isOpen ? '▸' : '▾';
      });
    }
  });
  state.storageLoaded = true;
}

function createStorageRow(dev, depth) {
  const tr = document.createElement('tr');
  const key = dev.path || dev.name || ('dev-' + depth);
  const hasChildren = (dev.children || []).length > 0;
  const expanded = state.storageExpanded.has(key);
  if (dev.raid_member) tr.className = 'row-disabled';
  const mounted = (dev.mountpoints || []).length > 0;
  const fsText = [dev.fstype, dev.label].filter(Boolean).join(' · ');
  tr.innerHTML = `
    <td><span class="tree-name" style="padding-left:${depth * 20}px">
      ${hasChildren ? `<span class="tree-caret">${expanded ? '▾' : '▸'}</span>` : `<span class="tree-caret" style="visibility:hidden">▸</span>`}
      ${depth > 0 ? '' : ''}${esc(dev.name || dev.path || '—')}
      ${dev.raid_member ? '<span class="raid-tag">RAID 成员</span>' : ''}
    </span></td>
    <td class="num">${fmtGB(dev.size)}</td>
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
  tr.dataset.storageKey = key;
  return tr;
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
  const name = dev.name || '';
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <p>将设备 <strong class="mono">${esc(dev.path || dev.name)}</strong> 格式化为：</p>
    <div class="field"><label>文件系统</label>
      <select class="select" id="fmt-fs" style="width:100%">
        <option value="ext4">ext4</option>
        <option value="xfs">xfs</option>
      </select></div>
    <p class="warn-text">警告：格式化将清除该设备上的全部数据，且不可恢复。请确认设备选择无误。</p>
    <div class="field"><label class="confirm-input-note">请输入设备名 <strong class="mono">${esc(name)}</strong> 以确认操作</label>
      <input class="input mono" id="fmt-confirm" placeholder="${esc(name)}" autocomplete="off" /></div>`;
  showModal({
    title: '格式化设备', body: wrap,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '确认格式化', cls: 'danger',
        handler: async (btn) => {
          const confirmName = wrap.querySelector('#fmt-confirm').value.trim();
          if (confirmName !== name) { toast('请输入正确的设备名以确认', 'error'); return; }
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
    // 创建第一个管理员后认证即时启用：刷新状态并隐藏提示横幅，未登录则弹出登录框
    const me = await api('/api/me');
    state.me = me;
    state.authRequired = !!me.auth_required;
    $('#security-banner').classList.toggle('hidden', state.authRequired);
    if (me.auth_required && !me.logged_in) { showLogin(); }
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

/* ---------- 运维中心 ---------- */

function levelBadge(level) {
  const map = { ok: ['', '正常'], info: ['info', '关注'], warn: ['warn', '预警'], crit: ['crit', '高危'] };
  const [cls, text] = map[level] || ['', level || '正常'];
  return `<span class="badge ${cls}">${text}</span>`;
}

async function loadOps() {
  state.opsLoaded = true;
  await Promise.allSettled([
    renderEnclosures(),
    loadLedger(),
    loadReport(),
    loadReplacements(),
    loadHotspare(),
    loadLife(),
  ]);
}

function diskVisualTone(d) {
  let tone = stateTone(d.state);
  const pv = (d.prediction && d.prediction.level) || 'ok';
  if (pv === 'crit') tone = 'crit';
  else if (pv === 'warn' && tone !== 'crit') tone = 'warn';
  const tt = tempTone(Number(d.temperature));
  if (tt === 'crit') tone = 'crit';
  else if (tt === 'warn' && tone !== 'crit') tone = 'warn';
  return tone === 'ok' || tone === 'warn' || tone === 'crit' ? tone : 'unknown';
}

function isHotSpare(d) {
  return /(^|[^a-z])h?s$/i.test(String(d.state || '')) && !/online/i.test(String(d.state || ''));
}

function bayProgress(d) {
  const kinds = [
    ['rebuild', 'var(--warn)'],
    ['copyback', 'var(--primary)'],
    ['erase', 'var(--crit)'],
  ];
  for (const [k, color] of kinds) {
    const p = Number(d[k + '_progress']);
    if (p > 0) return { pct: Math.min(100, p), color };
  }
  return null;
}

const BAY_SPECS = [
  { bays: 8, u: '2U', uHeight: 220 },
  { bays: 12, u: '2U', uHeight: 220 },
  { bays: 16, u: '4U', uHeight: 440 },
  { bays: 20, u: '5U', uHeight: 550 },
  { bays: 24, u: '4U', uHeight: 440 },
];

function baySpecFor(bays) {
  return BAY_SPECS.find(s => s.bays === Number(bays)) || null;
}

function autoBaySpec(count) {
  const target = Math.max(8, count);
  return BAY_SPECS.find(s => s.bays >= target) || BAY_SPECS[BAY_SPECS.length - 1];
}

function bayKey(d) {
  return d.eid + ':' + d.slot;
}

function loadBayLayout() {
  try {
    state.bayLayout = JSON.parse(localStorage.getItem('lsi-bay-layout') || '{}');
  } catch (e) {
    state.bayLayout = {};
  }
}

function saveBayLayout() {
  try {
    localStorage.setItem('lsi-bay-layout', JSON.stringify(state.bayLayout));
  } catch (e) { /* 忽略 */ }
}

function arrangeDisks(list, capacity, eid) {
  const key = String(eid);
  const slots = new Array(capacity).fill(null);
  const byKey = {};
  list.forEach(d => { byKey[bayKey(d)] = d; });
  const saved = state.bayLayout[key];
  if (Array.isArray(saved)) {
    const placed = new Set();
    saved.forEach((k, i) => {
      if (i < capacity && byKey[k]) {
        slots[i] = byKey[k];
        placed.add(k);
      }
    });
    list.forEach(d => {
      if (!placed.has(bayKey(d))) {
        const idx = slots.indexOf(null);
        if (idx >= 0) slots[idx] = d;
      }
    });
    return slots;
  }
  list.forEach((d, i) => { if (i < capacity) slots[i] = d; });
  return slots;
}

let _dragState = null;
let _bcacheChart = null;

function onTrayDragStart(ev) {
  _dragState = { eid: ev.currentTarget.dataset.eid, idx: Number(ev.currentTarget.dataset.idx) };
  ev.dataTransfer.effectAllowed = 'move';
  try { ev.dataTransfer.setData('text/plain', ''); } catch (e) { /* 忽略 */ }
  ev.currentTarget.classList.add('dragging');
}

function onTrayDragOver(ev) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
  ev.currentTarget.classList.add('drop-target');
}

function onTrayDragLeave(ev) {
  ev.currentTarget.classList.remove('drop-target');
}

function onTrayDrop(ev) {
  ev.preventDefault();
  ev.currentTarget.classList.remove('drop-target');
  if (!_dragState) return;
  const srcEid = _dragState.eid;
  const srcIdx = _dragState.idx;
  const dstEid = ev.currentTarget.dataset.eid;
  const dstIdx = Number(ev.currentTarget.dataset.idx);
  if (srcEid !== dstEid || srcIdx === dstIdx) return;
  moveBay(String(srcEid), srcIdx, dstIdx);
}

function onTrayDragEnd(ev) {
  ev.currentTarget.classList.remove('dragging');
  _dragState = null;
  $$('.drop-target').forEach(el => el.classList.remove('drop-target'));
}

function moveBay(eid, from, to) {
  const st = state.status;
  const disks = (st.physical_disks || [])
    .filter(d => String(d.eid) === String(eid))
    .sort((a, b) => Number(a.slot) - Number(b.slot));
  const spec = state.bayCapacity && state.bayCapacity !== 'auto'
    ? baySpecFor(Number(state.bayCapacity)) || autoBaySpec(disks.length)
    : autoBaySpec(disks.length);
  const capacity = spec.bays;
  const slots = arrangeDisks(disks, capacity, eid);
  if (from < 0 || from >= slots.length || to < 0 || to >= slots.length || !slots[from]) return;
  const moved = slots[from];
  slots[from] = slots[to];
  slots[to] = moved;
  state.bayLayout[String(eid)] = slots.map(d => (d ? bayKey(d) : null));
  saveBayLayout();
  renderEnclosures();
}

async function renderEnclosures() {
  const wrap = $('#enclosure-view');
  wrap.innerHTML = '<div class="loading-line">加载中…</div>';
  let st = state.status;
  if (!st) {
    try {
      st = await api('/api/status');
      state.status = st;
    } catch (e) {
      wrap.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
      return;
    }
  }
  const disks = st.physical_disks || [];
  const ctrl = st.controller || {};
  loadBayLayout();
  wrap.innerHTML = '';
  if (!disks.length) {
    wrap.innerHTML = '<div class="muted">暂无磁盘数据</div>';
    return;
  }

  const tools = document.createElement('div');
  tools.className = 'bay-tools';
  tools.innerHTML = '<span class="tiny">拖动托盘可重新排布盘位；定位灯亮起时对应 LED 会闪烁</span><button class="btn sm" id="btn-bay-reset" type="button">重置排布</button>';
  wrap.appendChild(tools);
  $('#btn-bay-reset').addEventListener('click', () => {
    state.bayLayout = {};
    saveBayLayout();
    renderEnclosures();
  });

  const groups = {};
  disks.forEach(d => {
    const eid = String(d.eid);
    (groups[eid] || (groups[eid] = [])).push(d);
  });

  Object.keys(groups).sort((a, b) => Number(a) - Number(b)).forEach(eid => {
    const list = groups[eid].slice().sort((a, b) => Number(a.slot) - Number(b.slot));
    const spec = state.bayCapacity && state.bayCapacity !== 'auto'
      ? baySpecFor(Number(state.bayCapacity)) || autoBaySpec(list.length)
      : autoBaySpec(list.length);
    const capacity = spec.bays;
    const uLabel = spec.u;
    const rows = capacity / 4;
    const gap = 14;
    const trayH = Math.max(64, Math.floor((spec.uHeight - (rows - 1) * gap) / rows));
    const placed = arrangeDisks(list, capacity, eid);
    const ctrlTone = stateTone(ctrl.health) === 'crit' ? 'crit' : stateTone(ctrl.health) === 'warn' ? 'warn' : 'ok';
    const bbuTone = stateTone(ctrl.bbu_state) === 'crit' ? 'crit' : stateTone(ctrl.bbu_state) === 'warn' ? 'warn' : 'ok';
    const healthText = HEALTH_TEXT[st.health] || st.health || '未知';
    const rocText = ctrl.roc_temp != null ? ctrl.roc_temp + '°C' : '—';
    const view = document.createElement('div');
    view.className = 'chassis-view';
    view.innerHTML = `
      <div class="chassis">
        <span class="rack-ear rack-ear-left" aria-hidden="true"></span>
        <span class="rack-ear rack-ear-right" aria-hidden="true"></span>
        <span class="screw screw-tl" aria-hidden="true"></span>
        <span class="screw screw-tr" aria-hidden="true"></span>
        <span class="screw screw-bl" aria-hidden="true"></span>
        <span class="screw screw-br" aria-hidden="true"></span>
        <div class="chassis-top">
          <div class="chassis-brand">
            <span class="chassis-name">MegaRAID Storage</span>
            <span class="chassis-model">${esc(ctrl.model || 'Controller')} · FW ${esc(ctrl.fw || '—')}</span>
            <span class="chassis-asset">${esc(st.host || '')} · Enclosure ${esc(eid)}</span>
          </div>
          <div class="chassis-status">
            <span class="status-cell"><span class="panel-led led-power"></span>PWR</span>
            <span class="status-cell"><span class="panel-led led-fan"></span>FAN</span>
            <span class="status-cell"><span class="panel-led led-${bbuTone}"></span>BBU</span>
            <span class="status-cell"><span class="panel-led led-${ctrlTone}"></span>CTRL</span>
          </div>
        </div>
        <div class="chassis-screen screen-${esc(st.health || 'unknown')}" aria-hidden="true">${esc(healthText)} · ROC ${esc(rocText)}</div>
        <div class="bay-rack"></div>
        <div class="chassis-foot">
          <span class="panel-label">Enclosure ${esc(eid)} · ${uLabel} · ${capacity} 盘位 · 已装 ${list.length}</span>
          <span class="chassis-vents"></span>
        </div>
      </div>`;
    const rack = view.querySelector('.bay-rack');
    rack.style.setProperty('--tray-h', trayH + 'px');
    for (let i = 0; i < capacity; i++) {
      const d = placed[i];
      const tray = document.createElement('button');
      tray.type = 'button';
      tray.dataset.eid = eid;
      tray.dataset.idx = String(i);
      if (!d) {
        tray.className = 'tray tray-empty';
        tray.disabled = true;
        tray.innerHTML = `<span class="tray-slot">空槽</span>`;
      } else {
        const tone = diskVisualTone(d);
        const hs = isHotSpare(d);
        const prog = bayProgress(d);
        tray.className = 'tray tray-' + tone + (hs ? ' tray-hs' : '') + (d.locate ? ' tray-locate' : '');
        const stateText = (hs ? '热备 · ' : '') + (d.state || '—');
        const tempText = d.temperature != null ? d.temperature + '°C' : '—';
        tray.innerHTML = `
          <span class="tray-handle"></span>
          <span class="tray-led"></span>
          <span class="tray-meta">
            <span class="tray-slot">${esc(d.label)}</span>
            <span class="tray-model">${esc(d.model || '—')}</span>
          </span>
          <span class="tray-temp">${tempText}</span>
          <span class="tray-state">${esc(stateText)}</span>
          ${prog ? `<span class="tray-progress"><i style="width:${prog.pct}%;background:${prog.color}"></i></span>` : ''}`;
        tray.title = `${d.model || '—'} · ${d.sn || '—'} · ${d.state || '—'} · ${tempText}`;
        tray.draggable = true;
        tray.addEventListener('dragstart', onTrayDragStart);
        tray.addEventListener('dragend', onTrayDragEnd);
        tray.addEventListener('click', () => openDrawer(d));
      }
      tray.addEventListener('dragover', onTrayDragOver);
      tray.addEventListener('dragleave', onTrayDragLeave);
      tray.addEventListener('drop', onTrayDrop);
      rack.appendChild(tray);
    }
    wrap.appendChild(view);
  });
}

async function loadLedger() {
  const tb = $('#ledger-table tbody');
  tb.innerHTML = '<tr><td colspan="12" class="muted">加载中…</td></tr>';
  let data;
  try {
    data = await api('/api/disk_ledger');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="12" class="muted">加载失败：${esc(e.message)}</td></tr>`;
    return;
  }
  const rows = data.disks || [];
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="12" class="muted">暂无磁盘数据</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => {
    const adviceCls = r.replace_advice === '立即更换' ? 'crit' : r.replace_advice === '建议安排更换' ? 'warn' : r.replace_advice === '关注' ? 'info' : '';
    return `<tr>
      <td class="num">${esc(r.label)}</td>
      <td>${esc(r.model || '—')}</td>
      <td class="num">${esc(r.sn || '—')}</td>
      <td>${stateBadge(r.state)}</td>
      <td class="num">${r.temperature != null ? esc(r.temperature) + '°C' : '—'}</td>
      <td class="num">${esc(fmtHours(r.power_on_hours))}</td>
      <td class="num">${esc(r.reallocated != null ? r.reallocated : '—')}</td>
      <td class="num">${esc(r.pending != null ? r.pending : '—')}</td>
      <td class="num">${esc(r.uncorrectable != null ? r.uncorrectable : '—')}</td>
      <td>${levelBadge(r.predict_level)}</td>
      <td><span class="badge ${adviceCls}">${esc(r.replace_advice)}</span></td>
      <td class="num">${esc(r.last_seen || '—')}</td>
    </tr>`;
  }).join('');
}

async function loadReport() {
  const wear = $('#wear-top');
  const life = $('#lifetime-top');
  let data;
  try {
    data = await api('/api/report/summary');
  } catch (e) {
    wear.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
    life.innerHTML = '<div class="muted">加载失败</div>';
    return;
  }
  const wearRows = data.wear_top || [];
  wear.innerHTML = wearRows.length
    ? wearRows.map(x => `<div class="rank-row">
        <span class="rank-label">${esc(x.label)}</span>
        <span class="rank-sub">${esc(x.model || '—')}</span>
        <span class="rank-metrics">重映射 ${esc(x.reallocated)} · 待定 ${esc(x.pending)} · 无法纠正 ${esc(x.uncorrectable)}</span>
        ${levelBadge(x.predict_level)}
      </div>`).join('')
    : '<div class="muted">暂无磨损数据</div>';

  const lifeRows = data.lifetime_top || [];
  life.innerHTML = lifeRows.length
    ? lifeRows.map(x => `<div class="rank-row">
        <span class="rank-label">${esc(x.label)}</span>
        <span class="rank-sub">${esc(x.model || '—')}</span>
        <span class="rank-metrics">${esc(x.power_on_hours)} 小时 · ${esc(x.power_on_days)} 天</span>
      </div>`).join('')
    : '<div class="muted">暂无通电时长数据</div>';
}

async function loadReplacements() {
  const el = $('#replace-list');
  el.innerHTML = '<div class="loading-line">加载中…</div>';
  let data;
  try {
    data = await api('/api/disk_ledger');
  } catch (e) {
    el.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
    return;
  }
  const candidates = (data.disks || []).filter(d => d.predict_level === 'warn' || d.predict_level === 'crit');
  if (!candidates.length) {
    el.innerHTML = '<div class="muted">当前没有需要更换的磁盘</div>';
    return;
  }
  el.innerHTML = candidates.map(d => {
    const cls = d.replace_advice === '立即更换' ? 'crit' : 'warn';
    const stepsHtml = state.isAdmin
      ? `<div class="replace-steps">
          <span class="step">1. 定位灯</span><button class="btn sm" data-step="locate">开启</button>
          <span class="step">2. 下线旧盘</span><button class="btn sm danger" data-step="offline">下线</button>
          <span class="step">3. 移除旧盘</span><span class="tiny">物理操作</span>
          <span class="step">4. 插入新盘</span><span class="tiny">物理操作</span>
          <span class="step">5. 设为全局热备</span><button class="btn sm" data-step="hotspare">执行</button>
        </div>`
      : '<div class="tiny">仅管理员可执行换盘操作</div>';
    return `<div class="replace-item" data-eid="${esc(d.eid)}" data-slot="${esc(d.slot)}">
      <div class="replace-head">
        <span class="replace-label mono">${esc(d.label)}</span>
        <span class="badge ${cls}">${esc(d.replace_advice)}</span>
        <span class="tiny">${esc(d.model || '—')}</span>
      </div>
      ${stepsHtml}
    </div>`;
  }).join('');
  if (state.isAdmin) {
    el.querySelectorAll('button[data-step]').forEach(btn => {
      btn.addEventListener('click', () => {
        const item = btn.closest('.replace-item');
        const eid = Number(item.dataset.eid);
        const slot = Number(item.dataset.slot);
        const d = (state.status && state.status.physical_disks || []).find(x => Number(x.eid) === eid && Number(x.slot) === slot);
        if (!d) { toast('未找到该磁盘', 'error'); return; }
        const step = btn.dataset.step;
        if (step === 'locate') diskAction(d, d.locate ? 'locate_stop' : 'locate_start');
        else if (step === 'offline') diskAction(d, 'offline');
        else if (step === 'hotspare') diskAction(d, 'hotspare_global');
      });
    });
  }
}

async function loadHotspare() {
  const body = $('#hotspare-body');
  body.innerHTML = '<div class="loading-line">加载中…</div>';
  let data;
  try {
    data = await api('/api/hotspare_policy');
  } catch (e) {
    body.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
    return;
  }
  const cfg = data.config || {};
  const opts = [0, 1, 2, 3, 4].map(n => `<option value="${n}"${Number(cfg.desired_global) === n ? ' selected' : ''}>${n} 块</option>`).join('');
  const eligible = data.eligible || [];
  const eligibleHtml = eligible.length
    ? eligible.map(d => `<div class="hs-row">
        <span class="mono">${esc(d.label)}</span>
        <span class="tiny">${esc(d.model || '')}</span>
        ${state.isAdmin ? `<button class="btn sm" data-hs="${esc(d.eid)}:${esc(d.slot)}">设为全局热备</button>` : '<span class="tiny">可配置</span>'}
      </div>`).join('')
    : '<div class="tiny" style="margin-top:10px">当前没有 UGood/JBOD 盘可设为热备</div>';

  if (state.isAdmin) {
    body.innerHTML = `
      <div class="field">
        <label for="hs-desired">期望全局热备盘数量</label>
        <select class="select" id="hs-desired">${opts}</select>
      </div>
      <label class="persist-row" style="margin-top:10px">
        <input type="checkbox" id="hs-auto" ${cfg.auto_promote ? 'checked' : ''} />
        <span>自动补位（策略标记，当前为人工确认）</span>
      </label>
      <div class="rt-kv" style="margin-top:14px">
        <div class="item"><div class="v">${data.global_count}</div><div class="k">全局热备</div></div>
        <div class="item"><div class="v">${data.dedicated_count}</div><div class="k">专用热备</div></div>
        <div class="item"><div class="v">${eligible.length}</div><div class="k">可设为热备</div></div>
      </div>
      ${eligible.length ? '<div class="tiny" style="margin-top:10px">可设为全局热备：</div><div class="hs-list">' + eligibleHtml + '</div>' : ''}`;
    body.querySelectorAll('button[data-hs]').forEach(btn => {
      btn.addEventListener('click', () => {
        const [eid, slot] = btn.dataset.hs.split(':').map(Number);
        const d = (state.status && state.status.physical_disks || []).find(x => Number(x.eid) === eid && Number(x.slot) === slot);
        if (d) diskAction(d, 'hotspare_global');
      });
    });
  } else {
    body.innerHTML = `
      <div class="rt-kv">
        <div class="item"><div class="v">${data.global_count}</div><div class="k">全局热备</div></div>
        <div class="item"><div class="v">${data.dedicated_count}</div><div class="k">专用热备</div></div>
        <div class="item"><div class="v">${eligible.length}</div><div class="k">可设为热备</div></div>
      </div>
      <div class="tiny" style="margin-top:10px">期望全局热备 ${Number(cfg.desired_global) || 0} 块 · ${cfg.auto_promote ? '已开启' : '未开启'}自动补位</div>
      ${eligible.length ? '<div class="tiny" style="margin-top:10px">可设为热备的盘：</div><div class="hs-list">' + eligibleHtml + '</div>' : ''}
      <div class="tiny" style="margin-top:12px">仅管理员可配置热备策略</div>`;
  }
}

async function saveHotsparePolicy() {
  const btn = $('#btn-hotspare-save');
  btnLoading(btn, true);
  try {
    await api('/api/hotspare_policy', {
      method: 'POST',
      body: {
        desired_global: Number($('#hs-desired').value),
        auto_promote: $('#hs-auto').checked,
      },
    });
    toast('热备策略已保存', 'ok');
    await loadHotspare();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function loadLife() {
  const body = $('#life-body');
  body.innerHTML = '<div class="loading-line">加载中…</div>';
  let data;
  try {
    data = await api('/api/disk_ledger');
  } catch (e) {
    body.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
    return;
  }
  const rows = data.disks || [];
  if (!rows.length) {
    body.innerHTML = '<div class="muted">暂无磁盘寿命数据</div>';
    return;
  }
  const stageText = { normal: '正常', watch: '关注', aged: '老化', critical: '临界' };
  body.innerHTML = rows.map(r => {
    const life = r.life || {};
    const cls = life.stage === 'critical' ? 'crit' : life.stage === 'aged' ? 'warn' : life.stage === 'watch' ? 'info' : '';
    return `<div class="life-row">
      <span class="life-label mono">${esc(r.label)}</span>
      <span class="life-sub">${esc(r.model || '—')}</span>
      <span class="life-meter"><i style="width:${esc(life.used_percent || 0)}%"></i></span>
      <span class="life-pct mono">${esc(life.remaining_percent != null ? life.remaining_percent : '—')}%</span>
      <span class="badge ${cls}">${stageText[life.stage] || '正常'}</span>
    </div>`;
  }).join('');
}

/* ---------- 视图切换 ---------- */
function switchView(v) {
  state.view = v;
  try {
    if (['overview', 'storage', 'logs', 'ops', 'users'].includes(v)) {
      localStorage.setItem('lsi-view', v);
    }
  } catch (e) { /* 忽略 */ }
  $$('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  ['overview', 'storage', 'logs', 'ops', 'users'].forEach(name => {
    $('#view-' + name).classList.toggle('hidden', name !== v);
  });
  if (v === 'storage' && !state.storageLoaded) loadStorage();
  if (v === 'storage' && !state.fsUsage) loadFsUsage();
  if (v === 'storage' && !state.nfsLoaded) loadNfs();
  if (v === 'storage' && !state.bcacheLoaded) loadBcache();
  if (v === 'ops' && !state.opsLoaded) loadOps();
  if (v === 'users' && !state.usersLoaded) loadUsers();
}

/* ---------- UI 绑定 ---------- */
function bindUI() {
  // 侧边栏导航
  $$('.nav-btn').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));

  // 未启用认证安全提示：点击跳转到用户管理页创建管理员
  const gotoCreateUser = () => {
    switchView('users');
    const u = $('#nu-username');
    if (u) setTimeout(() => u.focus(), 50);
  };
  $('#security-banner').addEventListener('click', gotoCreateUser);
  $('#security-banner').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); gotoCreateUser(); }
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
  // 导入外来（Foreign）虚拟盘配置：仅当控制器检测到外来配置时显示
  $('#btn-vd-import').addEventListener('click', () => {
    const c = (state.status && state.status.controller) || {};
    const count = Number(c.foreign_count) || 1;
    const desc = c.foreign_desc
      ? `检测到外来配置：${esc(c.foreign_desc)}。导入后控制器将恢复原阵列（DG/VD），原有数据可继续访问。`
      : `确认导入外来 (Foreign) 配置？该操作会把外部虚拟盘配置导入当前控制器，使其恢复可见与可访问。检测到 ${count} 组外来配置。`;
    confirmModal('载入外部配置', `<p>${desc}</p><p class="tiny">导入完成后将自动刷新状态，原 UGood+F 标记的磁盘会回到原阵列。</p>`, '确认导入', true, async () => {
      const r = await api('/api/foreign_import', { method: 'POST', body: { acknowledge: true } });
      if (r && r.ok === false) throw new Error(r.error || '导入失败');
      toast('外来配置已导入', 'ok');
      await loadStatus();
      await loadVdDetail();
    });
  });
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
      state.me = {
        auth_required: true,
        logged_in: true,
        username: r.username,
        role: r.role,
        version: r.version,
        auth_mode: r.auth_mode || 'local',
        manage_users: r.manage_users !== false,
      };
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
  $('#btn-webhook-test').addEventListener('click', testWebhook);

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
  let ctlSearchTimer = null;
  $('#ctl-search').addEventListener('input', () => {
    clearTimeout(ctlSearchTimer);
    ctlSearchTimer = setTimeout(() => {
      state.ctlQuery = $('#ctl-search').value.trim();
      loadCtlEvents();
    }, 300);
  });
  $('#btn-log-download').addEventListener('click', downloadLogs);
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
  $('#btn-ledger-refresh').addEventListener('click', () => {
    renderEnclosures();
    loadLedger();
    loadReport();
    loadReplacements();
    loadHotspare();
    loadLife();
  });
  $('#bay-capacity').addEventListener('change', (ev) => {
    state.bayCapacity = ev.target.value;
    renderEnclosures();
  });
  $('#btn-hotspare-save').addEventListener('click', saveHotsparePolicy);
  $$('#alarm-actions [data-alarm]').forEach(b =>
    b.addEventListener('click', () => alarmAction(b.dataset.alarm)));
  $$('#jbod-actions [data-jbod]').forEach(b =>
    b.addEventListener('click', () => jbodAction(b.dataset.jbod)));
  $('#fs-show-hidden').addEventListener('change', (ev) => {
    state.showHiddenFs = ev.target.checked;
    renderFsTable();
  });
  $('#btn-nfs-refresh').addEventListener('click', loadNfs);
  $('#btn-nfs-install').addEventListener('click', installNfs);
  $('#btn-bcache-prepare').addEventListener('click', prepareBcache);
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
// 块设备容量统一按 GB（10^9）展示
function fmtGB(b) {
  const n = Number(b);
  if (b == null || isNaN(n) || n <= 0) return '—';
  return (n / 1e9).toFixed(1) + ' GB';
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
  const list = (state.fsUsage || []).filter(fs => !fs.hidden);
  const body = $('#fs-usage-body');
  if (!list.length) { body.innerHTML = '<div class="loading-line">暂无文件系统数据</div>'; return; }
  const wrap = document.createElement('div');
  wrap.className = 'meter-rows';
  list.forEach(fs => {
    const pct = Number(fs.use_percent) || 0;
    const label = `${fs.mountpoint || fs.device}`;
    const val = `${pct}% · 可用 ${fmtBytes(fs.avail)}`;
    const line = document.createElement('div');
    line.className = 'fs-meter-line';
    line.innerHTML = `<div title="${esc(fs.device)} · ${esc(fs.fstype)} · 共 ${fmtBytes(fs.size)}" style="flex:1;min-width:0">
      ${meterHtml(label, pct, val, fsUseClass(pct))}</div>`;
    if (state.isAdmin) {
      const btn = document.createElement('button');
      btn.className = 'btn sm';
      btn.textContent = '隐藏';
      btn.title = '在展示中隐藏该分区（可在存储页“显示已隐藏”中恢复）';
      btn.addEventListener('click', () => fsVisibilityAction(fs));
      line.appendChild(btn);
    }
    wrap.appendChild(line);
  });
  body.innerHTML = '';
  body.appendChild(wrap);
}

function renderFsTable() {
  const tb = $('#fs-table tbody');
  const all = state.fsUsage;
  if (!all) return;
  const list = all.filter(fs => state.showHiddenFs || !fs.hidden);
  if (!list.length) { tb.innerHTML = '<tr><td colspan="8" class="muted">暂无文件系统数据</td></tr>'; return; }
  tb.innerHTML = '';
  list.forEach(fs => {
    const pct = Number(fs.use_percent) || 0;
    const ipct = Number(fs.inode_use_percent) || 0;
    const tr = document.createElement('tr');
    if (fs.hidden) tr.style.opacity = '0.45';
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
      <td><span class="use-cell" title="Inode 已用 ${num0(fs.inode_used)} / 共 ${num0(fs.inode_total)}">
        <span class="meter"><i class="${fsUseClass(ipct)}" style="width:${Math.min(100, ipct)}%"></i></span>
        <span class="meter-val">${ipct}%</span>
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
      const vis = document.createElement('button');
      vis.className = 'btn sm';
      vis.style.marginLeft = '6px';
      vis.textContent = fs.hidden ? '恢复显示' : '隐藏';
      vis.addEventListener('click', () => fsVisibilityAction(fs));
      ops.appendChild(vis);
    } else {
      ops.innerHTML = '<span class="tiny">—</span>';
    }
    tb.appendChild(tr);
  });
}

async function fsVisibilityAction(fs) {
  const hide = !fs.hidden;
  try {
    const r = await api('/api/storage/visibility', {
      method: 'POST',
      body: { mountpoint: fs.mountpoint, hidden: hide },
    });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(hide ? `已隐藏 ${fs.mountpoint}` : `已恢复显示 ${fs.mountpoint}`, 'ok');
    await loadFsUsage();
    if (state.chartType === 'fs') loadHistory();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function fstabAction(action, fs) {
  const isAdd = action === 'add';
  if (isAdd) {
    confirmModal('写入 fstab',
      `<p>将 ${esc(fs.device)}（${esc(fs.fstype)}）的挂载点 ${esc(fs.mountpoint)} 写入 /etc/fstab，系统重启后将自动挂载。</p>`,
      '写入', false, async () => {
        const r = await api('/api/storage/fstab', { method: 'POST', body: { action: 'add', device: fs.device, mountpoint: fs.mountpoint, fstype: fs.fstype } });
        if (r && r.ok === false) throw new Error(r.error || '操作失败');
        toast('已写入 /etc/fstab', 'ok');
      });
    return;
  }
  // 移除：输入挂载点文字二次确认
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <p>从 /etc/fstab 移除挂载点 <strong class="mono">${esc(fs.mountpoint)}</strong> 的条目，重启后将不再自动挂载（不影响当前已挂载状态）。</p>
    <div class="field"><label class="confirm-input-note">请输入挂载点 <strong class="mono">${esc(fs.mountpoint)}</strong> 以确认操作</label>
      <input class="input mono" id="fstab-confirm" placeholder="${esc(fs.mountpoint)}" autocomplete="off" /></div>`;
  showModal({
    title: '从 fstab 移除', body: wrap,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '移除', cls: 'danger',
        handler: async (btn) => {
          const v = wrap.querySelector('#fstab-confirm').value.trim();
          if (v !== fs.mountpoint) { toast('请输入正确的挂载点以确认', 'error'); return; }
          btnLoading(btn, true);
          try {
            const r = await api('/api/storage/fstab', { method: 'POST', body: { action: 'remove', mountpoint: fs.mountpoint } });
            if (r && r.ok === false) throw new Error(r.error || '操作失败');
            toast('已从 /etc/fstab 移除', 'ok');
            closeModal();
          } catch (e) { toast(e.message, 'error'); }
          finally { btnLoading(btn, false); }
        }
      }
    ]
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
  $('#btn-nfs-install').classList.toggle('hidden', !state.isAdmin);
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

async function installNfs() {
  const btn = $('#btn-nfs-install');
  btnLoading(btn, true);
  try {
    const r = await api('/api/nfs/install', { method: 'POST' });
    if (r && r.ok === false) throw new Error(r.error || '安装失败');
    toast(r.message || 'NFS 服务已安装', 'ok');
    await loadNfs();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function loadBcache() {
  const body = $('#bcache-body');
  body.innerHTML = '<div class="loading-line">加载中…</div>';
  let data;
  let devData;
  try {
    const r = await Promise.all([
      api('/api/bcache/status'),
      api('/api/bcache/devices'),
    ]);
    data = r[0];
    devData = r[1];
  } catch (e) {
    body.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
    return;
  }
  state.bcacheLoaded = true;
  const btn = $('#btn-bcache-prepare');
  const ready = !!(data.available && data.module_loaded);
  btn.classList.toggle('hidden', !state.isAdmin || ready);
  const csets = data.cache_sets || [];
  const devs = data.devices || [];
  const okBadge = '<span class="badge ok">就绪</span>';
  const noBadge = '<span class="badge">未就绪</span>';
  const cacheOffline = csets.some(c => c.cache_offline);
  body.innerHTML = `
    <div class="rt-kv" style="margin-top:0">
      <div class="item"><div class="v">${data.available ? '有' : '无'}</div><div class="k">bcache-tools</div></div>
      <div class="item"><div class="v">${data.module_loaded ? '已加载' : '未加载'}</div><div class="k">内核模块</div></div>
      <div class="item"><div class="v">${csets.length}</div><div class="k">缓存集</div></div>
      <div class="item"><div class="v">${devs.length}</div><div class="k">bcache 设备</div></div>
    </div>
    <div class="banner" style="margin-top:14px;${ready ? 'border-color:rgba(53,196,139,.3);background:var(--ok-bg);' : ''}">
      ${ready ? okBadge + ' <span>bcache 环境已就绪。</span>' : noBadge + ' <span style="flex:1">尚未安装 bcache-tools 或未加载内核模块，管理员可点击右上角“准备环境”。</span>'}
    </div>
    ${cacheOffline ? `<div class="banner" style="margin-top:10px;border-color:color-mix(in srgb, var(--crit) 45%, transparent);background:var(--crit-bg)">
      <span class="badge crit">缓存盘离线</span><span>检测到缓存盘已不在系统中（bcache 已降级为 no cache）。请检查缓存盘连接，恢复后可重新注册并绑定。</span>
    </div>` : ''}
    ${csets.length ? `<div class="tiny" style="margin-top:14px">缓存集统计：</div>
      ${csets.map(c => {
        const offline = !!c.cache_offline;
        const hit = offline ? null : (Number(c.cache_hit_ratio) || 0);
        const avail = offline ? null : Number(c.cache_available_percent);
        const usage = avail != null && avail >= 0 ? (100 - avail) : null;
        const ci = c.cache_info || {};
        const cards = [
          { k: '命中率', v: offline ? '—' : `${hit}%`, s: offline ? '缓存盘不在线' : `Hits ${esc(c.cache_hits)} · Miss ${esc(c.cache_misses)}` },
          { k: '缓存可用', v: offline ? '—' : `${esc(avail)}%`, s: offline ? '缓存盘不在线' : '缓存可用比例' },
          { k: '缓存占用', v: offline ? '—' : `${esc(usage)}%`, s: offline ? '缓存盘不在线' : '已占用缓存' },
          { k: '直通命中/未命中', v: offline ? '—' : `${esc(c.cache_bypass_hits)} / ${esc(c.cache_bypass_misses)}`, s: offline ? '缓存盘不在线' : '绕过缓存的 IO' },
          { k: '总桶', v: offline ? '—' : `${esc(ci.nbuckets || '—')}`, s: offline ? '缓存盘不在线' : `桶大小 ${esc(ci.bucket_size || '—')}` },
          { k: '桶状态', v: offline ? '—' : `C ${esc(ci.bucket_clean || 0)}% · D ${esc(ci.bucket_dirty || 0)}%`, s: offline ? '缓存盘不在线' : `Unused ${esc(ci.bucket_unused || 0)}% · Meta ${esc(ci.bucket_metadata || 0)}%` },
        ];
        return `<div class="stat-mini-grid">${cards.map(x => `<div class="stat-mini">
          <span class="stat-mini-k">${esc(x.k)}</span>
          <span class="stat-mini-v">${x.v}</span>
          <span class="stat-mini-s">${x.s}</span>
        </div>`).join('')}</div>`;
      }).join('')}` : ''}`;
  appendBcacheControls(body, data, devData);
  if (devs.length) renderBcacheTrend(devs[0].name);
  if (csets.length) renderBcacheTopology(csets, devs);
  loadBcacheAlertSettings();
  if (state.isAdmin) {
    devs.forEach(x => loadBcacheTunables(x.name));
  }
}

function appendBcacheControls(body, data, devData) {
  const admin = !!state.isAdmin;
  const devs = data.devices || [];
  const blockDevs = (devData && devData.devices) || [];
  const isBlank = d => !d.mounted && !d.in_bcache && String(d.fstype || '').toLowerCase() !== 'bcache';
  const freeCache = blockDevs.filter(d => isBlank(d) && !d.has_partitions && (Number(d.rota) === 0 || /nvme/i.test(d.device)));
  const freeBacking = blockDevs.filter(isBlank);
  const ready = !!(data.available && data.module_loaded);
  const fmtSize = (b) => {
    if (b == null || !Number(b)) return '';
    const g = Number(b) / 1073741824;
    return g >= 1 ? g.toFixed(1) + 'G' : Math.round(Number(b) / 1048576) + 'M';
  };
  const curMode = (raw) => {
    const m = String(raw || '').match(/\[(\w+)\]/);
    return m ? m[1] : 'writethrough';
  };
  const modeOpts = ['writethrough', 'writeback', 'writearound', 'none']
    .map(m => `<option value="${m}">${m}</option>`).join('');

  let html = '';

  if (admin && ready) {
    if (freeCache.length && freeBacking.length) {
      const cacheOpts = freeCache.map(d =>
        `<option value="${esc(d.path)}">${esc(d.device)} · ${fmtSize(d.size)} · ${esc(d.model || d.tran || 'SSD')}</option>`).join('');
      const backOpts = freeBacking.map(d =>
        `<option value="${esc(d.path)}">${esc(d.device)} · ${fmtSize(d.size)}${d.has_partitions ? ' · 含分区(将被清空)' : ''}</option>`).join('');
      html += `
        <div class="field" style="margin-top:14px">
          <label>新建 bcache（缓存盘 + 被加速盘）</label>
          <div class="replace-steps" style="flex-wrap:wrap">
            <select class="select" id="bcache-cache">${cacheOpts}</select>
            <select class="select" id="bcache-backing">${backOpts || '<option value="">无可用盘</option>'}</select>
            <button class="btn sm danger" id="btn-bcache-create">创建缓存</button>
          </div>
          <label class="check-row" style="margin-top:8px"><input type="checkbox" id="bcache-ack" />
            <span>确认两块设备上的现有数据都可以被清空</span></label>
        </div>`;
    } else {
      const csetList = data.cache_sets || [];
      const degraded = csetList.some(c => c.cache_offline) || devs.some(x => /no cache/i.test(String(x.state || '')));
      const bcCands = blockDevs.filter(d => !d.in_bcache && String(d.fstype || '').toLowerCase() === 'bcache');
      const cacheCands = bcCands.filter(d => d.bcache_role === 'cache');
      const backingCands = bcCands.filter(d => d.bcache_role !== 'cache');
      let showReattach = false;
      try { showReattach = localStorage.getItem('lsi-bcache-stopped') === '1'; } catch (e) { /* 忽略 */ }
      if (showReattach && csetList.length && backingCands.length) {
        html += '<div class="tiny" style="margin-top:12px">检测到可重新绑定的 backing 设备：</div><div class="hs-list">' +
          backingCands.map(d => `<div class="hs-row">
            <span class="mono">${esc(d.path)}</span>
            <span class="tiny">${esc(d.model || '')}</span>
            <button class="btn sm" data-reattach="${esc(d.path)}" data-cset="${esc(csetList[0].uuid)}">重新注册并绑定</button>
          </div>`).join('') + '</div>';
      } else {
        if (degraded && csetList.length && cacheCands.length) {
          html += '<div class="tiny" style="margin-top:12px">检测到缓存盘已恢复，可重新接入：</div><div class="bcache-ops"><button class="btn sm primary" data-bcache-recover>恢复缓存</button></div>';
        } else {
          html += '<div class="tiny" style="margin-top:12px">暂无可用空白缓存盘/被加速盘（已被 bcache 使用或已挂载的设备不会出现在这里）。</div>';
        }
      }
    }
  }

  if (devs.length) {
    html += '<div class="tiny" style="margin-top:14px">bcache 设备管理：</div><div class="hs-list">' +
      devs.map(x => {
        const n = x.name;
        const mc = curMode(x.cache_mode);
        const stCls = x.state === 'clean' ? 'ok' : (/no cache|no_cache/i.test(String(x.state || '')) ? 'warn' : '');
        const modeSel = `<select class="select" data-mode-for="${esc(n)}" data-current="${esc(mc)}">${modeOpts.replace(`value="${mc}"`, `value="${mc}" selected`)}</select>`;
        const ops = admin
          ? `<div class="bcache-ops">
              ${modeSel}
              <button class="btn sm" data-mode-save="${esc(n)}">切换模式</button>
              <button class="btn sm" data-writeback="${esc(n)}">手动回写</button>
              ${x.mounted
                ? `<button class="btn sm" data-umount="${esc(n)}">卸载</button>`
                : `<input class="input" data-mount-input="${esc(n)}" value="/mnt/${esc(n)}" style="width:150px" />
                   <button class="btn sm" data-mount="${esc(n)}">挂载</button>
                   <button class="btn sm" data-detach="${esc(n)}">解绑缓存</button>
                   <button class="btn sm danger" data-erase="${esc(n)}">擦除超级块</button>
                   <button class="btn sm danger" data-stop="${esc(n)}">停止</button>`}
            </div>`
          : '<div class="tiny">仅管理员可管理</div>';
        return `<div class="bcache-mgr" data-name="${esc(n)}">
          <div class="hs-row">
            <span class="mono">${esc(x.device)}</span>
            <span class="tiny">${esc(x.label || '')}</span>
            <span class="badge ${stCls}">${esc(x.state || '—')}</span>
            ${x.mounted ? `<span class="badge ok">已挂载 ${esc(x.mounted)}</span>` : '<span class="badge">未挂载</span>'}
            ${x.dirty_data ? `<span class="tiny">脏数据 ${esc(x.dirty_data)}</span>` : ''}
          </div>
          ${ops}
          ${admin ? `<div class="bcache-tune" data-tune-for="${esc(n)}"></div>` : ''}
        </div>`;
      }).join('') + '</div>';
  }

  const csets = data.cache_sets || [];
  if (admin && csets.length) {
    html += csets.map(c => {
      const uuid = c.uuid || '';
      return `<div class="bcache-mgr" data-gc-uuid="${esc(uuid)}">
        <div class="tiny">缓存集 ${esc(uuid.slice(0, 8))}… · GC / 缓存盘下线</div>
        <div class="bcache-ops">
          <button class="btn sm" data-gc-view="${esc(uuid)}">查看 GC 状态</button>
          <button class="btn sm" data-gc-run="${esc(uuid)}">触发 GC</button>
          <button class="btn sm danger" data-cache-off="${esc(uuid)}">注销缓存集</button>
        </div>
        <pre class="smart-pre hidden" data-gc-pre="${esc(uuid)}"></pre>
      </div>`;
    }).join('');
  }

  if (!admin) {
    html += '<div class="tiny" style="margin-top:12px;color:var(--ink-faint)">仅管理员可创建/切换/解绑 bcache。</div>';
  }
  body.insertAdjacentHTML('beforeend', html);

  const createBtn = $('#btn-bcache-create');
  if (createBtn) {
    createBtn.addEventListener('click', () => {
      if (!$('#bcache-ack').checked) { toast('请先勾选确认清空设备', 'error'); return; }
      const cachePath = $('#bcache-cache').value;
      const backingPath = $('#bcache-backing').value;
      if (!cachePath || !backingPath) { toast('请选择缓存盘和被加速盘', 'error'); return; }
      confirmModal('创建 bcache 缓存',
        `<p>缓存盘：<strong class="mono">${esc(cachePath)}</strong><br/>被加速盘：<strong class="mono">${esc(backingPath)}</strong></p>
         <p class="warn-text">两块设备上的分区和数据都会被清空，且不可恢复。请再次确认盘位无误。</p>`,
        '确认创建', true, async () => {
          createBtn.disabled = true;
          try {
            const r = await api('/api/bcache/create', { method: 'POST', body: { cache_path: cachePath, backing_path: backingPath, acknowledge: true } });
            toast(r.message || 'bcache 已创建', 'ok');
            await loadBcache();
          } catch (e) { toast(e.message, 'error'); }
          finally { createBtn.disabled = false; }
        });
    });
  }

  body.querySelectorAll('[data-reattach]').forEach(b => b.addEventListener('click', async () => {
    const path = b.dataset.reattach;
    const cset = b.dataset.cset;
    try {
      const r = await api('/api/bcache/reattach', { method: 'POST', body: { backing_path: path, cset_uuid: cset } });
      toast(r.message || '已重新绑定', 'ok');
      try { localStorage.setItem('lsi-bcache-stopped', '0'); } catch (e) { /* 忽略 */ }
      await loadBcache();
    } catch (e) { toast(e.message, 'error'); }
  }));
  const recoverBtn = $('#bcache-body').querySelector('[data-bcache-recover]');
  if (recoverBtn) {
    recoverBtn.addEventListener('click', async () => {
      try {
        const r = await api('/api/bcache/recover', { method: 'POST' });
        toast(r.message || '缓存已恢复', 'ok');
        await loadBcache();
      } catch (e) { toast(e.message, 'error'); }
    });
  }

  body.querySelectorAll('[data-mode-save]').forEach(b => {
    b.addEventListener('click', () => {
      const name = b.dataset.modeSave;
      const sel = body.querySelector(`[data-mode-for="${name}"]`);
      const mode = sel.value;
      const current = sel.dataset.current || '';
      let warn = '';
      if (current === 'writeback' && mode !== 'writeback') {
        warn = '<p class="warn-text">当前是 writeback（回写）模式，切换前会先触发脏数据回写，期间请勿断电。</p>';
      } else if (mode === 'writeback') {
        warn = '<p class="warn-text">writeback 回写模式下异常断电可能丢数据。</p>';
      }
      confirmModal(`切换 ${name} 缓存模式`,
        `<p>将 ${name} 从 <strong>${esc(current)}</strong> 切换到 <strong>${esc(mode)}</strong>。${warn}</p>
         <p class="warn-text">确认切换吗？</p>`,
        '确认切换', true, async () => {
          const r = await api('/api/bcache/mode', { method: 'POST', body: { name, mode } });
          toast(r.message || '已切换', 'ok');
          await loadBcache();
        });
    });
  });
  body.querySelectorAll('[data-writeback]').forEach(b => b.addEventListener('click', async () => {
    const name = b.dataset.writeback;
    try {
      const r = await api('/api/bcache/writeback', { method: 'POST', body: { name } });
      toast(r.message || '已触发回写', 'ok');
      await loadBcache();
    } catch (e) { toast(e.message, 'error'); }
  }));
  body.querySelectorAll('[data-erase]').forEach(b => b.addEventListener('click', () => {
    const name = b.dataset.erase;
    confirmModal(`擦除 ${name} 超级块`,
      `<p class="warn-text">此操作会 wipefs 对应设备的 bcache 超级块，通常用于彻底移除旧缓存/backing 配置。</p>
       <div class="field"><label for="erase-dev">要擦除的设备路径</label>
       <input class="input mono" id="erase-dev" placeholder="/dev/sdX" autocomplete="off" /></div>
       <label class="check-row"><input type="checkbox" id="erase-ack2" />
       <span>我已知晓该设备上的 bcache 配置将被擦除且不可恢复</span></label>`,
      '执行擦除', true, async () => {
        const devPath = ($('#erase-dev').value || '').trim();
        if (!/^\/dev\/(sd[a-z]+|nvme\d+n\d+)$/.test(devPath)) throw new Error('请输入有效的整块设备路径，如 /dev/sdc');
        if (!$('#erase-ack2').checked) throw new Error('请勾选风险确认');
        const r = await api('/api/bcache/erase', { method: 'POST', body: { device_path: devPath, confirm: devPath, acknowledge: true } });
        toast(r.message || '已擦除', 'ok');
        await loadBcache();
      });
  }));
  body.querySelectorAll('[data-mount]').forEach(b => b.addEventListener('click', async () => {
    const name = b.dataset.mount;
    const input = body.querySelector(`[data-mount-input="${name}"]`);
    const mountpoint = input ? input.value.trim() : '';
    try {
      const r = await api('/api/bcache/mount', { method: 'POST', body: { name, mountpoint } });
      toast(r.message || '已挂载', 'ok');
      await loadBcache();
    } catch (e) { toast(e.message, 'error'); }
  }));
  body.querySelectorAll('[data-umount]').forEach(b => b.addEventListener('click', async () => {
    try {
      const r = await api('/api/bcache/umount', { method: 'POST', body: { name: b.dataset.umount } });
      toast(r.message || '已卸载', 'ok');
      await loadBcache();
    } catch (e) { toast(e.message, 'error'); }
  }));
  body.querySelectorAll('[data-detach]').forEach(b => b.addEventListener('click', () => {
    const name = b.dataset.detach;
    confirmModal(`解绑 ${name}`, '<p>将缓存集与 backing 解绑，解绑后不再加速，可重新 attach。</p>', '解绑', false, async () => {
      const r = await api('/api/bcache/detach', { method: 'POST', body: { name } });
      toast(r.message || '已解绑', 'ok');
      await loadBcache();
    });
  }));
  body.querySelectorAll('[data-stop]').forEach(b => b.addEventListener('click', () => {
    const name = b.dataset.stop;
    confirmModal(`停止 ${name}`, `<p class="warn-text">停止 ${name} 会使其从系统移除；再次使用需要重新注册/attach。</p>`, '停止', true, async () => {
      const r = await api('/api/bcache/stop', { method: 'POST', body: { name } });
      toast(r.message || '已停止', 'ok');
      try { localStorage.setItem('lsi-bcache-stopped', '1'); } catch (e) { /* 忽略 */ }
      await loadBcache();
    });
  }));
  body.querySelectorAll('[data-gc-view]').forEach(b => b.addEventListener('click', async () => {
    const uuid = b.dataset.gcView;
    const pre = body.querySelector(`[data-gc-pre="${uuid}"]`);
    try {
      const r = await api('/api/bcache/gc?uuid=' + encodeURIComponent(uuid));
      pre.classList.remove('hidden');
      pre.textContent = JSON.stringify(r, null, 2);
    } catch (e) {
      toast(e.message, 'error');
    }
  }));
  body.querySelectorAll('[data-gc-run]').forEach(b => b.addEventListener('click', async () => {
    try {
      const r = await api('/api/bcache/gc/trigger', { method: 'POST', body: { uuid: b.dataset.gcRun } });
      toast(r.message || '已触发 GC', 'ok');
    } catch (e) { toast(e.message, 'error'); }
  }));
  body.querySelectorAll('[data-cache-off]').forEach(b => b.addEventListener('click', () => {
    const uuid = b.dataset.cacheOff;
    confirmModal('注销 bcache 缓存集',
      `<p>将注销缓存集并下线缓存盘（相当于删除该缓存的 bcache 归属）。<br/>
       <span class="mono">${esc(uuid)}</span></p>
       <div class="field"><label>输入缓存集 UUID 确认</label>
       <input class="input mono" id="cache-off-confirm" placeholder="${esc(uuid)}" autocomplete="off" /></div>
       <label class="check-row"><input type="checkbox" id="cache-off-ack" />
       <span>我已知晓注销后该缓存盘不再参与加速</span></label>`,
      '注销', true, async () => {
        const val = ($('#cache-off-confirm').value || '').trim();
        if (val !== uuid) throw new Error('UUID 不一致，请重新输入');
        if (!$('#cache-off-ack').checked) throw new Error('请勾选风险确认');
        const r = await api('/api/bcache/cache_unregister', { method: 'POST', body: { uuid, confirm: val, acknowledge: true } });
        toast(r.message || '已注销', 'ok');
        await loadBcache();
      });
  }));
}

async function prepareBcache() {
  const btn = $('#btn-bcache-prepare');
  btnLoading(btn, true);
  try {
    const r = await api('/api/bcache/prepare', { method: 'POST' });
    if (r && r.ok === false) throw new Error(r.error || '准备失败');
    toast(r.message || 'bcache 环境已就绪', 'ok');
    await loadBcache();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function renderBcacheTrend(devName) {
  if (typeof Chart === 'undefined' || !devName) return;
  let s;
  try {
    s = await api('/api/bcache/stats?device=' + encodeURIComponent(devName));
  } catch (e) {
    return;
  }
  const body = $('#bcache-body');
  if (!body) return;
  if (_bcacheChart) { _bcacheChart.destroy(); _bcacheChart = null; }
  const old = document.getElementById('bcache-trend-wrap');
  if (old) old.remove();
  const wrap = document.createElement('div');
  wrap.id = 'bcache-trend-wrap';
  const order = ['five_minute', 'hour', 'day', 'total'];
  const labels = ['最近5分钟', '最近1小时', '最近1天', '累计'];
  const stats = s.stats || {};
  const pick = (p, k) => {
    const v = Number((stats[p] && stats[p][k]));
    return isFinite(v) ? v : 0;
  };
  const hit = order.map(p => pick(p, 'cache_hit_ratio'));
  const hits = order.map(p => pick(p, 'cache_hits'));
  const misses = order.map(p => pick(p, 'cache_misses'));
  const ioCount = order.map((p, i) => hits[i] + misses[i]
    + pick(p, 'cache_bypass_hits') + pick(p, 'cache_bypass_misses'));
  wrap.innerHTML = `
    <div class="tiny" style="margin-top:14px">命中率 / IO 趋势（${esc(s.device)}）</div>
    ${s.cache_info && s.cache_info.device ? `<div class="tiny" style="margin:2px 0 6px;color:var(--ink-faint)">缓存盘 ${esc(s.cache_info.device)} · discard=${esc(s.cache_info.discard !== null ? s.cache_info.discard : 'N/A')} · metadata_written=${esc(s.cache_info.metadata_written || 'N/A')}</div>` : ''}
    <div style="position:relative;height:190px"><canvas id="bcache-trend-canvas"></canvas></div>`;
  body.appendChild(wrap);
  const ctx = document.getElementById('bcache-trend-canvas');
  _bcacheChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: '命中率 (%)',
          data: hit,
          borderColor: '#35c48b',
          backgroundColor: '#35c48b',
          yAxisID: 'y0',
          tension: 0.3,
          borderWidth: 2,
          pointRadius: 3,
        },
        {
          label: '命中+未命中+直通 (IO)',
          data: ioCount,
          borderColor: '#4f8cff',
          backgroundColor: '#4f8cff',
          yAxisID: 'y1',
          tension: 0.3,
          borderWidth: 2,
          pointRadius: 3,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600 },
      scales: {
        y0: {
          position: 'left',
          min: 0,
          max: 100,
          title: { display: true, text: '命中率 %' },
          ticks: { color: '#7f8b9a' },
        },
        y1: {
          position: 'right',
          min: 0,
          title: { display: true, text: 'IO 次数' },
          grid: { drawOnChartArea: false },
          ticks: { color: '#7f8b9a' },
        },
      },
      plugins: {
        legend: { labels: { color: '#9aa5b2' } },
      },
    },
  });
}

async function renderBcacheTopology(csets, devs) {
  const body = $('#bcache-body');
  if (!body || !csets.length || !devs.length) return;
  const old = document.getElementById('bcache-topology-wrap');
  if (old) old.remove();
  const cset = csets[0];
  let stats = {};
  try {
    stats = await api('/api/bcache/stats?device=' + encodeURIComponent(devs[0].name));
  } catch (e) { /* 忽略 */ }
  const cacheInfo = cset.cache_info || stats.cache_info || {};
  const cacheDev = cacheInfo.device || '—';
  const backing = stats.backing_device ? '/dev/' + stats.backing_device : '—';
  const d = devs[0];
  const avail = Number(cset.cache_available_percent);
  const used = avail >= 0 ? (100 - avail) : null;
  const uuid = cset.uuid || '';
  const wrap = document.createElement('div');
  wrap.id = 'bcache-topology-wrap';
  wrap.innerHTML = `
    <div class="tiny" style="margin-top:14px">bcache 拓扑</div>
    <div class="bc-topo-line">
      <div class="bc-node"><span class="bc-node-k">缓存盘</span><span class="bc-node-v mono">${esc(cacheDev)}</span><span class="bc-node-s">discard ${esc(cacheInfo.discard || '—')}</span></div>
      <span class="bc-link">→</span>
      <div class="bc-node bc-node-set"><span class="bc-node-k">缓存集</span><span class="bc-node-v mono">${esc((uuid || '').slice(0, 8))}…</span><span class="bc-node-s">可用 ${esc(avail)}%${used != null ? ` · 占用 ${used}%` : ''}</span></div>
      <span class="bc-link">→</span>
      <div class="bc-node"><span class="bc-node-k">backing</span><span class="bc-node-v mono">${esc(backing)}</span></div>
      <span class="bc-link">→</span>
      <div class="bc-node bc-node-dev"><span class="bc-node-k">加速设备</span><span class="bc-node-v mono">${esc(d.device)}</span><span class="bc-node-s">${esc(d.mounted ? '已挂载' : '未挂载')} · dirty ${esc(d.dirty_data || '0')}</span></div>
    </div>`;
  body.appendChild(wrap);
}

async function loadBcacheAlertSettings() {
  const body = $('#bcache-body');
  if (!body) return;
  let cfg;
  try {
    cfg = await api('/api/bcache/alerts');
  } catch (e) {
    return;
  }
  const old = document.getElementById('bcache-alert-settings');
  if (old) old.remove();
  const c = cfg.config || {};
  const el = document.createElement('div');
  el.id = 'bcache-alert-settings';
  const admin = !!state.isAdmin;
  el.innerHTML = `
    <div class="tiny" style="margin-top:14px">bcache 告警设置：</div>
    ${admin ? `<div class="bcache-ops" style="flex-wrap:wrap">
      <label class="check-row"><input type="checkbox" id="bc-alert-enabled" ${c.enabled ? 'checked' : ''} /> 启用巡检告警</label>
      <span class="tiny">可用率低于</span><input class="input" id="bc-avail" type="number" min="1" max="100" value="${esc(c.cache_available_warn)}" style="width:72px"><span class="tiny">%</span>
      <span class="tiny">脏数据滞留</span><input class="input" id="bc-stuck" type="number" min="1" value="${esc(c.dirty_stuck_minutes)}" style="width:72px"><span class="tiny">分钟</span>
      <span class="tiny">命中率骤降</span><input class="input" id="bc-drop" type="number" min="1" max="100" value="${esc(c.hit_drop_points)}" style="width:72px"><span class="tiny">点</span>
      <span class="tiny">积压阈值(MB)</span><input class="input" id="bc-backlog" type="number" min="1" value="${Math.round(Number(c.backlog_bytes) / 1048576)}" style="width:84px">
      <button class="btn sm primary" id="btn-bc-alert-save">保存</button>
    </div>` : `<div class="tiny">巡检间隔 60 秒，告警会走系统邮件/Webhook。当前阈值：可用率 < ${esc(c.cache_available_warn)}% · 脏数据滞留 ${esc(c.dirty_stuck_minutes)} 分钟 · 命中率骤降 ≥ ${esc(c.hit_drop_points)} 点 · 积压 > ${(Number(c.backlog_bytes) / 1048576).toFixed(0)}MB</div>`}`;
  body.appendChild(el);
  if (!admin) return;
  $('#btn-bc-alert-save').addEventListener('click', async () => {
    try {
      await api('/api/bcache/alerts', {
        method: 'POST',
        body: {
          enabled: $('#bc-alert-enabled').checked,
          cache_available_warn: Number($('#bc-avail').value),
          dirty_stuck_minutes: Number($('#bc-stuck').value),
          hit_drop_points: Number($('#bc-drop').value),
          backlog_bytes: Number($('#bc-backlog').value) * 1048576,
        },
      });
      toast('bcache 告警设置已保存', 'ok');
      await loadBcacheAlertSettings();
    } catch (e) { toast(e.message, 'error'); }
  });
}

function _sizeToMb(text) {
  const t = String(text || '').trim();
  if (t === '0' || t === '') return 0;
  const m = t.match(/^(\d+(?:\.\d+)?)\s*([kKmMgG])$/);
  if (!m) return null;
  const mult = { k: 1 / 1024, m: 1, g: 1024 }[m[2].toLowerCase()];
  return Math.round(Number(m[1]) * mult);
}

async function loadBcacheTunables(name) {
  const el = $('#bcache-body') && $('#bcache-body').querySelector(`[data-tune-for="${name}"]`);
  if (!el) return;
  let t;
  try {
    t = await api('/api/bcache/tunables?device=' + encodeURIComponent(name));
  } catch (e) {
    return;
  }
  const pct = Number(t.writeback_percent);
  const rate = _sizeToMb(t.writeback_rate);
  const seq = _sizeToMb(t.sequential_cutoff);
  const delay = Number(t.writeback_delay);
  const meta = t.writeback_metadata === '1';
  const polCur = (String(t.readahead_cache_policy || 'all').match(/\[([^\]]+)\]/) || [])[1] || 'all';
  el.innerHTML = `
    <div class="tiny" style="margin:10px 0 4px">缓存比例调节</div>
    <div class="bcache-ops" style="flex-wrap:wrap">
      <span class="tiny">脏数据驻留</span><input class="input" id="bc-wp" type="number" min="0" max="100" value="${esc(isFinite(pct) ? pct : 10)}" style="width:62px"><span class="tiny">%</span>
      <span class="tiny">回写速率</span><input class="input" id="bc-rate" type="number" min="0" value="${esc(rate == null ? 0 : rate)}" style="width:84px"><span class="tiny">MB/s (0=不限)</span>
      <span class="tiny">回写延迟</span><input class="input" id="bc-delay" type="number" min="0" value="${esc(isFinite(delay) ? delay : 0)}" style="width:62px"><span class="tiny">秒</span>
      <label class="check-row"><input type="checkbox" id="bc-meta" ${meta ? 'checked' : ''} /> metadata 回写</label>
      <span class="tiny">顺序缓存阈值</span><input class="input" id="bc-seq" type="number" min="0" value="${esc(seq == null ? 0 : seq)}" style="width:84px"><span class="tiny">MB (0=关闭)</span>
      <span class="tiny">readahead</span><select class="select" id="bc-policy">
        <option value="all"${polCur === 'all' ? ' selected' : ''}>all</option>
        <option value="meta-only"${polCur === 'meta-only' ? ' selected' : ''}>meta-only</option>
      </select>
      <button class="btn sm primary" id="btn-bc-tune-save">保存</button>
    </div>`;
  el.querySelector('#btn-bc-tune-save').addEventListener('click', async () => {
    try {
      const rateMb = Number(el.querySelector('#bc-rate').value) || 0;
      const seqMb = Number(el.querySelector('#bc-seq').value) || 0;
      await api('/api/bcache/tune', {
        method: 'POST',
        body: {
          name,
          settings: {
            writeback_percent: Number(el.querySelector('#bc-wp').value),
            writeback_rate: rateMb * 1048576,
            writeback_delay: Number(el.querySelector('#bc-delay').value),
            writeback_metadata: el.querySelector('#bc-meta').checked ? 1 : 0,
            sequential_cutoff: seqMb * 1048576,
            readahead_cache_policy: el.querySelector('#bc-policy').value,
          },
        },
      });
      toast('缓存比例已保存', 'ok');
      await loadBcacheTunables(name);
    } catch (e) { toast(e.message, 'error'); }
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

/* ---------- 日志打包下载 ---------- */
async function downloadLogs() {
  const btn = $('#btn-log-download');
  btnLoading(btn, true);
  try {
    const res = await fetch('/api/logs/download', { credentials: 'same-origin' });
    if (!res.ok) {
      let msg = '下载失败 (' + res.status + ')';
      try { const d = await res.json(); if (d && d.error) msg = d.error; } catch (e) { /* 非 JSON */ }
      throw new Error(msg);
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="?([^";]+)"?/);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : 'lsi-logs.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast('日志包已下载', 'ok');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

/* ---------- 控制器事件 ---------- */
async function loadCtlEvents() {
  const pre = $('#ctl-pre');
  try {
    const q = encodeURIComponent(state.ctlQuery || '');
    const d = await api('/api/controller_events?lines=' + state.ctlLines + (q ? '&q=' + q : ''));
    pre.textContent = d.output || '（无输出）';
    $('#ctl-total').textContent = d.total_lines != null
      ? (state.ctlQuery ? `匹配 ${d.total_lines} 行` : `共 ${d.total_lines} 行`) : '';
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
