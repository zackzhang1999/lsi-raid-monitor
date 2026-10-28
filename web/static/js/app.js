/* DataMax-LSI — Amax 存储管理系统（原生 JS SPA） */
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

/* ---------- 统一状态块 ----------
   产品里最常见的三个混淆：没有数据 / 加载失败 / 没权限。用一个组件把
   它们分开，而不是都渲染成一行灰字。 */
const STATE_ICONS = {
  empty: '<svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M3.5 10h17M8 5v14"/></svg>',
  error: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 8v5M12 16.2v.1"/></svg>',
  denied: '<svg viewBox="0 0 24 24"><rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7"/></svg>',
  loading: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 8.5 8.5"/></svg>',
};

function stateBlock(kind, title, desc, actionHtml, compact) {
  const cls = 'state ' + (kind || 'empty') + (compact ? ' compact' : '');
  return `<div class="${cls}">${STATE_ICONS[kind] || STATE_ICONS.empty}`
    + `<div class="t">${esc(title || '')}</div>`
    + (desc ? `<div class="d">${esc(desc)}</div>` : '')
    + (actionHtml ? `<div class="a">${actionHtml}</div>` : '')
    + '</div>';
}

function stateFromError(e) {
  const msg = String((e && e.message) || e || '未知错误');
  if (/未登录|会话|权限|401|403/.test(msg)) {
    return stateBlock('denied', '没有访问权限', msg);
  }
  return stateBlock('error', '加载失败', msg);
}

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
  } else if (
    typeof o.body === 'string' &&
    /^\s*[[{]/.test(o.body) &&
    !(o.headers && o.headers['Content-Type'])
  ) {
    // 已经有调用点直接传 JSON 字符串。不补这个头的话，后端 get_json()
    // 拿到 None，接口照样返回 200 但什么都没生效——静默失败最难查，
    // 所以在公共入口上兜住，而不是指望每个调用点都记得。
    o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
  }
  const res = await fetch(path, o);
  if (res.status === 401) {
    if (state.authRequired) showLogin();
    throw new Error('未登录或会话已过期');
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  // 402：功能被授权门禁挡住。统一给出说明与升级入口，
  // 而不是让各个调用点各自处理一句看不出所以然的报错。
  if (res.status === 402) {
    showUpgradePrompt(data || {});
    throw new Error((data && data.error) || '需要更高版本授权');
  }
  if (!res.ok) {
    const msg = data && (data.error || data.message) ? (data.error || data.message) : ('请求失败 (' + res.status + ')');
    if (res.status === 403) toast(msg, 'error');
    throw new Error(msg);
  }
  return data;
}

let _upgradeShown = 0;

function showUpgradePrompt(info) {
  // 同一功能的重复失败不反复弹窗（例如页面轮询）
  const key = info.feature || info.error || 'unknown';
  const now = Date.now();
  if (state.upgradePromptKey === key && now - _upgradeShown < 30000) return;
  state.upgradePromptKey = key;
  _upgradeShown = now;
  const editionName = { pro: '专业版', enterprise: '企业版' }[info.requires] || '更高版本';
  const cur = state.license || {};
  showModal({
    title: '需要更高版本授权',
    body: `<p>${esc(info.error || '当前版本不包含该功能')}</p>
      <dl class="kv" style="margin-top:12px">
        <dt>当前版本</dt><dd>${esc(cur.edition_name || '未知')}</dd>
        <dt>所需版本</dt><dd>${esc(editionName)}</dd>
      </dl>
      <p class="tiny" style="margin-top:10px">在「监控概览 → 授权」中可查看授权状态并安装离线授权文件。</p>`,
    actions: [
      { label: '关闭', handler: closeModal },
      {
        label: '查看授权',
        cls: 'primary',
        handler: async () => {
          closeModal();
          switchView('overview');
          const c = $('#card-license');
          if (c && c.scrollIntoView) c.scrollIntoView({ behavior: 'smooth', block: 'center' });
        },
      },
    ],
  });
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
  perfHours: 24,
  perfMetric: 'iops',
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
  bcacheTrendDevice: null,
  bcacheTrendTimer: null,
  raidSel: new Set(), // 勾选用于创建阵列的磁盘，键为 "eid:slot"
};

const HEALTH_TEXT = { ok: '正常', warn: '警告', crit: '严重', unknown: '未知' };
/* 控制器健康状态来自 storcli（Optimal/Dgrd/…），与上面聚合健康
   的中文映射是两套词表，不能混用。 */
const CTRL_HEALTH_TEXT = {
  Optimal: '正常',
  Opt: '正常',
  OK: '正常',
  Degraded: '降级',
  Dgrd: '降级',
  Failed: '故障',
  Offline: '离线',
  Offln: '离线',
  'Need Attention': '需关注',
  NdAtn: '需关注',
  Missing: '缺失',
  Msng: '缺失',
  Rebuild: '重建中',
  'N/A': '不可用',
};

function ctrlHealthText(h) {
  const s = String(h || '').trim();
  if (!s) return '未知';
  return CTRL_HEALTH_TEXT[s] || s;
}

/* 控制器列表：优先用后端返回的 controllers[]，旧接口退化到单控制器 */
function ctrlListOf(st) {
  const list = (st && Array.isArray(st.controllers)) ? st.controllers.slice() : [];
  if (list.length) return list.sort((a, b) => Number(a.ctrl) - Number(b.ctrl));
  const c = (st && st.controller) || {};
  return [{
    ctrl: 0,
    model: c.model || '',
    fw: c.fw || '',
    health: c.health || '',
    roc_temp: c.roc_temp,
    bbu_state: c.bbu_state || '',
  }];
}

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
  bindConsoleUI();
  bindLicenseUI();
  bindHostsUI();
  bindBackupUI();
  bindConfigUI();
  bindRecoveryUI();
  bindFleetAlertUI();
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
  // 权限按矩阵下发，前端不再自己推断"是不是管理员"。
  // 后端每一处都会独立校验，前端只负责别把用不了的按钮摆出来。
  state.perms = Array.isArray(me.perms) ? me.perms : ['view'];
  state.canConfigure = state.perms.includes('configure');
  state.canOperate = state.perms.includes('operate');
  state.canAudit = state.perms.includes('audit');
  state.roleLabel = me.role_label || '';
  state.isAdmin = state.canConfigure; // 既有代码里的 isAdmin 语义就是"可变更配置"
  buildRoleInfo(me.rbac);
  hideLogin();
  // 角色相关可见性
  $('#nav-users').classList.toggle('hidden', !(me.manage_users && state.isAdmin));
  // 未创建管理员账号时的安全提示横幅
  $('#security-banner').classList.toggle('hidden', state.authRequired);
  $('#btn-collect').classList.toggle('hidden', !state.isAdmin);
  $('#btn-host-add').classList.toggle('hidden', !state.isAdmin);
  $('#btn-token-rotate').classList.toggle('hidden', !state.isAdmin);
  $('#btn-vd-import').classList.toggle('hidden', !state.isAdmin); // 是否有外来配置由 loadStatus 动态控制
  $('#btn-logout').classList.toggle('hidden', !state.authRequired);
  $('#sel-interval').disabled = !state.isAdmin;
  $('#btn-alert-save').disabled = !state.isAdmin;
  $('#btn-alert-test').disabled = !state.isAdmin;
  $('#btn-webhook-test').disabled = !state.isAdmin;
  $('#btn-hotspare-save').classList.toggle('hidden', !state.isAdmin);
  const name = me.username || 'admin';
  // 用户名旁直接标出角色：一个页面同时开着两种权限的会话时，
  // "我现在能用哪些按钮"不该靠记忆。
  $('#user-name').textContent =
    name + (state.roleLabel ? `（${state.roleLabel}）` : '') + (state.authRequired ? '' : ' 未认证');
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
    loadAlarm(), loadJbod(), loadAlertCenter(), loadDiskHealth(),
    loadLicense(),
  ]);
  // 顶栏计数依赖事件流，放在状态之后刷新，避免与 loadStatus 抢 state
  refreshConsoleBadges().catch(() => {});
}

/* ---------- 状态数据 ---------- */

/* ---------- 告警中心 ---------- */
let _alertStatus = 'open';
let _alertData = { alerts: [], counts: {}, silences: [] };

async function loadAlertCenter() {
  const box = $('#alert-list');
  if (!box) return;
  try {
    _alertData = await api(`/api/alerts?status=${encodeURIComponent(_alertStatus)}`);
  } catch (e) {
    box.innerHTML = stateFromError(e);
    return;
  }
  renderSilenceBanner(_alertData.silences || []);

  const counts = _alertData.counts || {};
  const badge = $('#alert-count-badge');
  if (badge) {
    const n = Number(counts.active || 0);
    badge.textContent = n > 99 ? '99+' : String(n);
    badge.className = 'badge ' + (n > 0 ? 'crit' : 'hidden');
  }

  const rows = _alertData.alerts || [];
  if (!rows.length) {
    box.innerHTML = '<div class="muted">当前没有需要处理的告警</div>';
    return;
  }
  box.innerHTML = rows
    .map(a => {
      const lv = String(a.level || 'warning');
      const tone = lv === 'error' ? 'crit' : lv === 'warning' ? 'warn' : 'info';
      const text = { crit: '错误', warn: '警告', info: '信息' }[tone] || lv;
      const acked = a.status === 'acknowledged';
      const resolved = a.status === 'resolved';
      return `<div class="alert-row ${resolved ? 'resolved' : acked ? 'acked' : ''}">
        <span class="badge ${resolved ? 'ok' : tone}">${resolved ? '已恢复' : text}</span>
        <span class="alert-main">
          <span class="alert-title">${esc(a.title || a.key)}${Number(a.count) > 1 ? ` <span class="tiny">×${esc(a.count)}</span>` : ''}</span>
          <span class="alert-detail">${esc(a.detail || '')}</span>
        </span>
        <span class="alert-time mono" title="首次 ${esc(a.first_seen || '')} · 最近 ${esc(a.last_seen || '')}">${esc((a.last_seen || '').slice(5))}</span>
        <span class="alert-ops">
          ${resolved ? '' : acked
            ? `<span class="tiny" title="${esc(a.acknowledged_by || '')} 于 ${esc(a.acknowledged_at || '')} 确认">已确认</span>`
            : state.canOperate
              ? `<button class="btn sm" data-ack="${esc(a.id)}">确认</button>`
              : ''}
          ${state.canOperate ? `<button class="btn sm ghost" data-silence="${esc(a.key)}" title="为该项开启维护窗口">静默</button>` : ''}
        </span>
      </div>`;
    })
    .join('');

  $$('#alert-list [data-ack]').forEach(b =>
    b.addEventListener('click', async () => {
      btnLoading(b, true);
      try {
        await api(`/api/alerts/${b.dataset.ack}/ack`, { method: 'POST' });
        toast('告警已确认', 'ok');
        await loadAlertCenter();
      } catch (e) {
        toast(e.message, 'error');
        btnLoading(b, false);
      }
    })
  );
  $$('#alert-list [data-silence]').forEach(b =>
    b.addEventListener('click', () => openSilenceModal(b.dataset.silence))
  );
}

function renderSilenceBanner(silences) {
  const box = $('#silence-banner');
  if (!box) return;
  if (!silences.length) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = silences
    .map(
      s => `<div class="silence-item">
        <span class="silence-scope mono">${esc(s.scope)}</span>
        <span class="tiny">维护窗口至 ${esc(s.ends_at)}</span>
        ${s.reason ? `<span class="tiny">· ${esc(s.reason)}</span>` : ''}
        ${state.isAdmin ? `<button class="btn sm ghost" data-unsilence="${esc(s.id)}">结束</button>` : ''}
      </div>`
    )
    .join('');
  $$('#silence-banner [data-unsilence]').forEach(b =>
    b.addEventListener('click', async () => {
      try {
        await api(`/api/alerts/silence/${b.dataset.unsilence}/remove`, { method: 'POST' });
        toast('维护窗口已结束', 'ok');
        await loadAlertCenter();
      } catch (e) {
        toast(e.message, 'error');
      }
    })
  );
}

function openSilenceModal(scope) {
  const target = scope || 'all';
  showModal({
    title: '开启维护窗口',
    body: `<p>静默期间相关告警<strong>照常记录</strong>，但不发送邮件与 Webhook。</p>
      <div class="field"><label for="sil-scope">作用范围</label>
        <input class="input mono" id="sil-scope" value="${esc(target)}" />
        <span class="hint" style="color:var(--ink-faint)">all = 全部；disk:C1:E134:S0 = 单盘；controller:C1 = 整张卡（含其磁盘）</span></div>
      <div class="field"><label for="sil-minutes">时长（分钟）</label>
        <input class="input" id="sil-minutes" type="number" min="1" max="43200" value="60" /></div>
      <div class="field"><label for="sil-reason">原因（选填）</label>
        <input class="input" id="sil-reason" placeholder="如：更换背板" /></div>`,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '开启静默',
        cls: 'primary',
        handler: async btn => {
          btnLoading(btn, true);
          try {
            await api('/api/alerts/silence', {
              method: 'POST',
              body: {
                scope: $('#sil-scope').value.trim() || 'all',
                minutes: Number($('#sil-minutes').value) || 60,
                reason: $('#sil-reason').value.trim(),
              },
            });
            toast('维护窗口已开启', 'ok');
            closeModal();
            await loadAlertCenter();
          } catch (e) {
            toast(e.message, 'error');
            btnLoading(btn, false);
          }
        },
      },
    ],
  });
}

/* 跨控制器一致性：固件/型号不一致属于"整机级"问题，
   不在单块盘的告警范围内，但会直接影响可维护性与厂商支持。 */
function renderConsistency(st) {
  const box = $('#alert-consistency');
  if (!box) return;
  const issues = (st && st.consistency && st.consistency.issues) || [];
  if (!issues.length) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = issues
    .map(
      i => `<div class="consistency-item ${esc(i.level || 'info')}">
        <span class="consistency-title">${esc(i.title)}</span>
        <span class="consistency-detail mono">${esc(i.detail)}</span>
        <span class="consistency-hint">${esc(i.hint)}</span>
      </div>`
    )
    .join('');
}

/* ---------- 磁盘健康 TOP ---------- */
let _healthMetric = 'wear';

async function loadDiskHealth() {
  const box = $('#health-top');
  if (!box) return;
  let d;
  try {
    d = await api('/api/report/summary');
  } catch (e) {
    box.innerHTML = stateFromError(e);
    return;
  }
  const rows = _healthMetric === 'wear' ? d.wear_top || [] : d.lifetime_top || [];
  if (!rows.length) {
    box.innerHTML = stateBlock('empty', '没有物理磁盘', '控制器没有报告任何物理盘；刚插入的盘通常要等下一轮采集。');
    return;
  }
  const maxWear = Math.max(1, ...(d.wear_top || []).map(r => Number(r.wear_score) || 0));
  box.innerHTML = rows
    .map(r => {
      const label = r.full_label || r.label || '—';
      if (_healthMetric === 'wear') {
        const score = Number(r.wear_score) || 0;
        const pct = Math.max(2, Math.round((score / maxWear) * 100));
        const lv = r.predict_level || 'ok';
        const tone = lv === 'crit' ? 'crit' : lv === 'warn' ? 'warn' : 'ok';
        return `<div class="health-row">
          <span class="health-label mono">${esc(label)}</span>
          <span class="health-bar"><i class="${tone}" style="width:${pct}%"></i></span>
          <span class="health-meta mono" title="重映射 / 待定 / 无法纠正">${num0(r.reallocated)}/${num0(r.pending)}/${num0(r.uncorrectable)}</span>
        </div>`;
      }
      const life = r.life || {};
      const pct = Number(life.used_percent) || 0;
      const tone = pct >= 90 ? 'crit' : pct >= 70 ? 'warn' : 'ok';
      return `<div class="health-row">
        <span class="health-label mono">${esc(label)}</span>
        <span class="health-bar"><i class="${tone}" style="width:${Math.max(2, pct)}%"></i></span>
        <span class="health-meta mono">${esc(r.power_on_days != null ? r.power_on_days + ' 天' : '—')} · ${pct}%</span>
      </div>`;
    })
    .join('');
}

async function loadStatus() {
  const st = await api('/api/status');
  state.status = st;
  renderTopbar(st);
  renderHealth(st);
  renderStatCards(st);
  // 一致性结论来自 /api/status，必须在这里渲染：
  // loadAlertCenter 与 loadStatus 是并发发起的，它读 state.status 时
  // 很可能还是 null，导致面板空白。
  renderConsistency(st);
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
  renderTopbarControllers(st);
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

/* 控制器健康色：Optimal/Opt 视为正常，其余按 stateTone 判定 */
function ctrlHealthTone(health) {
  const t = stateTone(String(health || ''));
  if (t === 'crit') return 'crit';
  if (t === 'warn') return 'warn';
  const s = String(health || '').trim();
  if (!s || s === 'N/A') return 'unknown';
  return 'ok';
}

/* 多控制器并列展示：每张卡一个状态条，不必进二级页面就能看到
   第二张卡是否健康。只有一张卡时不显示，避免占位噪音。 */
function renderTopbarControllers(st) {
  const box = $('#tb-ctrls');
  if (!box) return;
  const ctrls = Array.isArray(st.controllers) ? st.controllers : [];
  if (ctrls.length < 2) {
    box.innerHTML = '';
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = ctrls.map(c => {
    const tone = ctrlHealthTone(c.health);
    const state = ctrlHealthText(c.health);
    const tip = [
      c.model || 'Controller',
      c.fw ? 'FW ' + c.fw : '',
      c.roc_temp != null ? 'ROC ' + c.roc_temp + '°C' : '',
      (c.num_disks || 0) + ' 盘 / ' + (c.num_vds || 0) + ' 阵列',
    ].filter(Boolean).join(' · ');
    return `<span class="ctrl-chip ${tone}" title="${esc(tip)}">
      <span class="ctrl-chip-id">C${esc(c.ctrl)}</span>
      <span class="ctrl-chip-state">${esc(state)}</span>
      ${c.roc_temp != null ? `<span class="ctrl-chip-temp">${esc(c.roc_temp)}°</span>` : ''}
      <span class="ctrl-chip-count">${esc(c.num_disks || 0)}盘</span>
    </span>`;
  }).join('');
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

/* 控制器级设置（蜂鸣器 / JBOD）的目标选择。
   多卡机器上这些设置是每张卡各自的，必须让用户明确选一张卡，
   否则改动会落到"默认那张"上而用户不知道操作了哪张。 */
function ctrlSelectValue(selId) {
  const sel = $(selId);
  if (!sel || sel.classList.contains('hidden')) return null;
  const v = Number(sel.value);
  return Number.isFinite(v) ? v : null;
}

function fillCtrlSelect(selId, ctrls) {
  const sel = $(selId);
  if (!sel) return;
  if (!ctrls || ctrls.length < 2) {
    sel.classList.add('hidden');
    sel.innerHTML = '';
    return;
  }
  const prev = sel.value;
  sel.classList.remove('hidden');
  sel.innerHTML = ctrls
    .map(c => `<option value="${esc(c.ctrl)}">C${esc(c.ctrl)}</option>`)
    .join('');
  if (prev && ctrls.some(c => String(c.ctrl) === String(prev))) sel.value = prev;
}

async function loadAlarm() {
  const badge = $('#alarm-badge');
  let data;
  const ctrls = state.status ? ctrlListOf(state.status) : [];
  fillCtrlSelect('#alarm-ctrl', ctrls);
  const sel = ctrlSelectValue('#alarm-ctrl');
  try { data = await api('/api/controller_alarm' + (sel != null ? `?ctrl=${sel}` : '')); }
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
    const sel = ctrlSelectValue('#alarm-ctrl');
    const body = { mode };
    if (sel != null) body.ctrl = sel;
    const r = await api('/api/controller_alarm', { method: 'POST', body });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(`阵列卡报警已${label}${sel != null ? `（C${sel}）` : ''}`, 'ok');
    await loadAlarm();
  });
}

/* ---------- JBOD 模式开关 ---------- */
async function loadJbod() {
  const badge = $('#jbod-badge');
  let data;
  const ctrls = state.status ? ctrlListOf(state.status) : [];
  fillCtrlSelect('#jbod-ctrl', ctrls);
  const sel = ctrlSelectValue('#jbod-ctrl');
  try { data = await api('/api/controller_jbod' + (sel != null ? `?ctrl=${sel}` : '')); }
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
    const sel = ctrlSelectValue('#jbod-ctrl');
    const body = { mode };
    if (sel != null) body.ctrl = sel;
    const r = await api('/api/controller_jbod', { method: 'POST', body });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    toast(`JBOD 模式已${label}${sel != null ? `（C${sel}）` : ''}`, 'ok');
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
  const wrap = $('#maint-cards');

  // 巡读与一致性检查是每张控制器各自的设置：多卡机器上必须让用户
  // 先明确针对哪张卡，不能拿"默认那张"的读数代表全部。
  const controllers = ctrlListOf(st);
  const primary = Number(
    (st.controller && st.controller.ctrl != null) ? st.controller.ctrl : controllers[0].ctrl
  );
  const cur = (state.maintCtrl != null && controllers.some(c => Number(c.ctrl) === Number(state.maintCtrl)))
    ? Number(state.maintCtrl)
    : primary;
  state.maintCtrl = cur;
  const cobj = controllers.find(c => Number(c.ctrl) === cur) || controllers[0];
  const m = (cobj && cobj.maintenance) || st.maintenance || {};

  const ctrlPicker = controllers.length > 1
    ? `<select class="select sm" id="sel-maint-ctrl">${controllers.map(c =>
        `<option value="${esc(c.ctrl)}"${Number(c.ctrl) === cur ? ' selected' : ''}>控制器 C${esc(c.ctrl)}</option>`
      ).join('')}</select>`
    : '';

  const prOps = state.isAdmin ? `
    <div class="maint-ops">
      ${ctrlPicker}
      <button class="btn sm" data-pr="start">启动</button>
      <button class="btn sm" data-pr="pause">暂停</button>
      <button class="btn sm" data-pr="resume">恢复</button>
      <button class="btn sm danger" data-pr="stop">停止</button>
    </div>` : '';

  const vdsOfCur = (state.vds || []).filter(v => Number(v.ctrl != null ? v.ctrl : 0) === cur);
  const vdOpts = vdsOfCur.map(v =>
    `<option value="${esc(v.ctrl != null ? v.ctrl : 0)}:${esc(v.vd)}">C${esc(v.ctrl != null ? v.ctrl : 0)} · VD ${esc(v.vd)}（${esc(v.dg_vd || '')} ${esc(v.name || '')}）</option>`).join('');
  const ccOps = state.isAdmin ? `
    <div class="maint-ops">
      ${controllers.length > 1 ? '' : ''}
      <select class="select" id="sel-cc-vd">${vdOpts || '<option value="">无虚拟盘</option>'}</select>
      <button class="btn sm" id="btn-cc-start" ${vdsOfCur.length ? '' : 'disabled'}>启动</button>
      <button class="btn sm danger" id="btn-cc-stop" ${vdsOfCur.length ? '' : 'disabled'}>停止</button>
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
    const maintCtrlSel = $('#sel-maint-ctrl');
    if (maintCtrlSel) {
      maintCtrlSel.addEventListener('change', () => {
        state.maintCtrl = Number(maintCtrlSel.value);
        renderMaintenance(st);
      });
    }
    $$('[data-pr]', wrap).forEach(b => b.addEventListener('click', () => {
      const action = b.dataset.pr;
      const text = { start: '启动', pause: '暂停', resume: '恢复', stop: '停止' }[action];
      raidAction({ target: 'patrolread', action, ctrl: cur }, `巡读：${text}`,
        `确认对控制器 C${cur} ${text}巡读 (Patrol Read)？`, action === 'stop');
    }));
    const ccVd = () => {
      const v = $('#sel-cc-vd').value;
      if (v === '') return null;
      const [c, vd] = String(v).split(':').map(Number);
      return { ctrl: c, vd };
    };
    $('#btn-cc-start') && $('#btn-cc-start').addEventListener('click', () => {
      const t = ccVd();
      if (!t) { toast('请选择虚拟盘', 'error'); return; }
      raidAction({ target: 'cc', action: 'start', vd: t.vd, ctrl: t.ctrl }, `一致性检查：启动`,
        `确认对控制器 C${t.ctrl} 的 VD ${t.vd} 启动一致性检查？该操作会占用一定的 IO 资源。`, false);
    });
    $('#btn-cc-stop') && $('#btn-cc-stop').addEventListener('click', () => {
      const t = ccVd();
      if (!t) { toast('请选择虚拟盘', 'error'); return; }
      raidAction({ target: 'cc', action: 'stop', vd: t.vd, ctrl: t.ctrl }, `一致性检查：停止`,
        `确认停止控制器 C${t.ctrl} 的 VD ${t.vd} 上正在运行的一致性检查？`, true);
    });
  }
}

/* ---------- RAID 操作（巡读 / CC / VD 初始化） ---------- */
function raidAction(body, title, desc, danger) {
  const payload = Object.assign({}, body);
  // 未显式指定控制器时：VD 操作跟随该 VD 所属控制器，
  // 其余控制器级操作跟随当前状态里的主控制器。
  if (payload.ctrl == null && payload.vd != null) {
    const v = (state.vds || []).find(x => Number(x.vd) === Number(payload.vd));
    if (v && v.ctrl != null) payload.ctrl = Number(v.ctrl);
  }
  if (payload.ctrl == null && state.status) {
    const c = state.status.controller || {};
    payload.ctrl = Number(c.ctrl != null ? c.ctrl : 0);
  }
  confirmModal(title, `<p>${esc(desc)}</p>`, '确认执行', danger, async () => {
    const r = await api('/api/raid_action', { method: 'POST', body: payload });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    if (r && r.task) {
      toast(`${title} 已提交（任务 #${r.task.id}）`, 'ok');
      openTaskDrawer();
    } else {
      toast(title + ' 已执行', 'ok');
    }
    await loadStatus();
    await loadVdDetail();
  });
}

/* 任务提交后的统一反馈：危险操作是异步执行的，
   提示必须是"已提交"而不是"已完成"，并直接把任务中心打开，
   否则用户会以为操作瞬间结束了、或者不知道去哪看进度。 */
function notifyTaskSubmitted(r, label) {
  if (r && r.task) {
    toast(`${label} 已提交（任务 #${r.task.id}）`, 'ok');
    openTaskDrawer();
  } else {
    toast(`${label} 已执行`, 'ok');
  }
}

function openTaskDrawer() {
  const scrim = $('#task-scrim');
  const drawer = $('#task-drawer');
  if (!scrim || !drawer) return;
  scrim.classList.remove('hidden');
  drawer.classList.add('open');
  renderTaskList();
  startTaskPoll();
}

let _taskPoll = null;

/* 长任务（扩容、初始化）耗时以分钟计，60 秒的全局刷新太慢，
   看不到进度变化；抽屉打开期间单独用更短的周期轮询。 */
function startTaskPoll() {
  if (_taskPoll) clearInterval(_taskPoll);
  _taskPoll = setInterval(() => {
    if ($('#task-drawer') && $('#task-drawer').classList.contains('open')) {
      renderTaskList().catch(() => {});
    }
  }, 2500);
}

function stopTaskPoll() {
  if (_taskPoll) {
    clearInterval(_taskPoll);
    _taskPoll = null;
  }
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
  if (!vds.length) { tb.innerHTML = '<tr><td colspan="10" class="muted">无虚拟磁盘</td></tr>'; return; }
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
      <td class="num">C${esc(v.ctrl != null ? v.ctrl : 0)}</td>
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
      dr.innerHTML = `<td colspan="10"><div class="vd-sub">
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
    { target: a.target, action: a.action, vd: v.vd, ctrl: Number(v.ctrl != null ? v.ctrl : 0) },
    `${a.text}`,
    `虚拟盘 ${label}：${a.desc}`,
    a.danger
  );
}

function expandVd(v) {
  const raid = RAID_TYPE_MAP[String(v.type || '').toUpperCase()] || 'r5';
  const vdCtrl = Number(v.ctrl != null ? v.ctrl : 0);
  // 只能加入同一张控制器上的盘：跨卡的盘后端会拒绝，
  // 与其让用户在提交时才看到报错，不如根本不出现在候选里
  const eligible = (state.status && state.status.physical_disks || [])
    .filter(d => d.state === 'UGood' && bayCtrl(d) === vdCtrl);
  if (!eligible.length) {
    toast(`控制器 C${vdCtrl} 上没有可加入阵列的 UGood 盘`, 'error');
    return;
  }
  const opts = eligible.map(d =>
    `<option value="${esc(bayCtrl(d))}:${esc(d.eid)}:${esc(d.slot)}">${esc(d.label)} · ${esc(d.model || '')}</option>`
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
          const [dctrl, eid, slot] = $('#expand-drive').value.split(':').map(Number);
          btnLoading(btn, true);
          try {
            const r = await api('/api/raid_expand', {
              method: 'POST',
            body: { vd: v.vd, ctrl: vdCtrl, raid, drives: [{ ctrl: dctrl, eid, slot }] },
            });
            if (r && r.ok === false) throw new Error(r.error || '扩容失败');
            notifyTaskSubmitted(r, '动态扩容');
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
    disks.filter(pdRaidEligible).map(diskKey)
  );
  Array.from(state.raidSel).forEach(k => { if (!eligibleKeys.has(k)) state.raidSel.delete(k); });

  if (!disks.length) {
    tb.innerHTML = '<tr><td colspan="15" class="muted">无物理磁盘</td></tr>';
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
    const key = diskKey(d);
    tr.innerHTML = `
      <td class="admin-col ${hideSel ? 'col-hidden' : ''}"></td>
      <td class="num">${esc('C' + bayCtrl(d))}</td>
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
        if (cb.checked) {
          // 一个阵列的成员盘必须同属一张控制器：跨卡选择在硬件上不成立，
          // 在勾选时就拦住，不要等到提交才报错
          const picked = state.raidSel.size
            ? parseDiskKey(Array.from(state.raidSel)[0])[0]
            : bayCtrl(d);
          if (bayCtrl(d) !== picked) {
            cb.checked = false;
            toast(`一个阵列只能使用同一张 RAID 卡上的磁盘（已选 C${picked}，本盘在 C${bayCtrl(d)}）`, 'error');
            return;
          }
          state.raidSel.add(key);
        } else {
          state.raidSel.delete(key);
        }
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
    const r = await api('/api/disk_action', { method: 'POST', body: { ctrl: bayCtrl(d), eid: d.eid, slot: d.slot, action, ...(extraBody || {}) } });
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
        const r = await api('/api/disk_erase', { method: 'POST', body: { ctrl: bayCtrl(d), eid: d.eid, slot: d.slot, pattern, confirm: label, acknowledge: true } });
        if (r && r.ok === false) throw new Error(r.error || '启动擦除失败');
        notifyTaskSubmitted(r, `磁盘 ${label} 安全擦除`);
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
    const [ckey, eid, slot] = parseDiskKey(key);
    const d = disks.find(
      x => bayCtrl(x) === ckey && Number(x.eid) === eid && Number(x.slot) === slot
    );
    if (!d || !pdRaidEligible(d)) {
      toast(`磁盘 C${ckey}:E${eid}:S${slot} 状态已变化，请重新勾选`, 'error');
      return;
    }
    chosen.push(d);
  }
  chosen.sort(
    (a, b) => (bayCtrl(a) - bayCtrl(b)) || (a.eid - b.eid) || (a.slot - b.slot)
  );

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
          drives: chosen.map(d => ({ ctrl: bayCtrl(d), eid: d.eid, slot: d.slot })),
        },
      });
      if (r && r.ok === false) throw new Error(r.error || '创建失败');
      notifyTaskSubmitted(r, `RAID${level} 阵列创建`);
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
    $('#event-list').innerHTML = stateFromError(e);
    return;
  }
  state.evTotal = data.total || 0;
  const list = $('#event-list');
  const events = data.events || [];
  if (!events.length) {
    list.innerHTML = stateBlock('empty', '暂无事件记录', '设备运行正常时，这里会保持空白。');
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
    const r = await api(`/api/disk_smart?ctrl=${encodeURIComponent(bayCtrl(d))}&eid=${encodeURIComponent(d.eid)}&slot=${encodeURIComponent(d.slot)}`);
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
  catch (e) { tb.innerHTML = `<tr><td colspan="5">${stateFromError(e)}</td></tr>`; return; }
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
  tb.innerHTML = '<tr><td colspan="4" class="muted">加载中…</td></tr>';
  let data;
  try { data = await api('/api/users'); }
  catch (e) { tb.innerHTML = `<tr><td colspan="4">${stateFromError(e)}</td></tr>`; return; }
  tb.innerHTML = '';
  const users = data.users || [];
  fillRoleSelect();
  if (!users.length) { tb.innerHTML = '<tr><td colspan="4">' + stateBlock('empty', '还没有本地用户', '当前未启用登录认证，任何人都能访问。建议立即创建第一个管理员。', '', true) + '</td></tr>'; return; }
  users.forEach(u => {
    const tr = document.createElement('tr');
    const info = ROLE_INFO[u.role] || ROLE_INFO.viewer;
    tr.innerHTML = `
      <td>${esc(u.username)}</td>
      <td><span class="badge ${u.role === 'admin' ? 'info' : u.role === 'operator' ? 'ok' : u.role === 'auditor' ? 'warn' : ''}">${esc(info.name)}</span></td>
      <td class="tiny">${esc(info.perms)}</td>
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
  // 16 槽压制到 2U：4 排托盘各压扁一档，整体高度和 8/12 槽是同一量级。
  // 之前按 4U 算，面板直接翻倍到 730px 高——盘位多不等于机箱高，
  // 2U 的 16 盘位背板本来就是靠把托盘压扁来塞下 4 排的。
  { bays: 16, u: '2U', uHeight: 220 },
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

/* ---------- 机箱背板（SES）实测数据 ----------

   盘位面板以前只能按"用户选的机箱规格"画：机器到底有几个槽位是猜的。
   SES 能给出背板的真实槽位表，所以：

   * 规格选「自动」时，盘位数按背板实测走，而不是按已装盘数凑一个最小值；
   * 每块盘按自己的物理槽位号取真实指示灯状态；
   * 当面板槽位数与背板一致时，空槽也能对上物理位置，
     于是"背板看见盘、控制器没认到"能在正确的槽位上亮出来。
   * 用户显式选了规格就以用户的为准——那是他的显式选择，
     但不能默默无语：规格与背板不一致时给出提示和一键对齐。 */
let _ses = null;
let _sesPromise = null;

function ensureSes() {
  if (_sesPromise) return _sesPromise;
  _sesPromise = api('/api/enclosure')
    .then(d => {
      _ses = d && d.available ? d : null;
      return _ses;
    })
    .catch(() => {
      _ses = null;
      return null;
    });
  return _sesPromise;
}

function sesSlotCount() {
  const t = (_ses && _ses.totals) || null;
  return t && t.slots ? Number(t.slots) : 0;
}

function sesSlotMap() {
  const encs = (_ses && _ses.enclosures) || [];
  // 多背板时槽位号会在每个背板内重复，而面板不知道盘属于哪个背板，
  // 硬按槽位号合并会把两块背板对错——宁可不合并。
  if (encs.length !== 1) return {};
  const out = {};
  (encs[0].slots || []).forEach(s => { out[Number(s.slot)] = s; });
  return out;
}

function bayKey(d) {
  return bayCtrl(d) + ':' + d.eid + ':' + d.slot;
}

/* 磁盘所属控制器编号。旧数据没有 ctrl 字段时按 0 处理。 */
function bayCtrl(d) {
  const c = Number(d && d.ctrl);
  return Number.isFinite(c) ? c : 0;
}

/* 磁盘在界面上的唯一键。必须带控制器编号：两张 RAID 卡的
   eid/slot 会重复，只用 eid:slot 会选错卡上的盘。 */
function diskKey(d) {
  return bayCtrl(d) + ':' + d.eid + ':' + d.slot;
}

/* 把 diskKey / bayKey 拆回 [ctrl, eid, slot] */
function parseDiskKey(key) {
  return String(key).split(':').map(Number);
}

function loadBayLayout() {
  try {
    // v2：托盘键加入控制器编号，旧排版无法安全沿用，直接重置
    state.bayLayout = JSON.parse(localStorage.getItem('lsi-bay-layout-v2') || '{}');
  } catch (e) {
    state.bayLayout = {};
  }
}

function saveBayLayout() {
  try {
    localStorage.setItem('lsi-bay-layout-v2', JSON.stringify(state.bayLayout));
  } catch (e) { /* 忽略 */ }
}

function arrangeDisks(list, capacity, groupKey) {
  const key = String(groupKey);
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
let _bcacheCsetPeriodStats = {};

function switchBcacheHitPeriod(uuid, period, btn) {
  const card = btn.closest('.stat-mini');
  if (!card) return;
  card.querySelectorAll('[data-hit-period]').forEach(b => b.classList.toggle('active', b === btn));
  const ps = (_bcacheCsetPeriodStats[uuid] || {})[period] || {};
  const setNum = (key, v) => {
    const el = card.querySelector(`[data-hit-key="${key}"]`);
    if (!el) return;
    el.textContent = v == null || v === '' ? '—' : String(v);
  };
  const ratio = ps.cache_hit_ratio;
  const ratioEl = card.querySelector('[data-hit-key="ratio"]');
  if (ratioEl) ratioEl.textContent = ratio == null ? '—' : `${ratio}%`;
  setNum('cache_hits', ps.cache_hits);
  setNum('cache_bypass_hits', ps.cache_bypass_hits);
  setNum('cache_miss', (Number(ps.cache_bypass_misses) || 0) + (Number(ps.cache_misses) || 0));
}

function onTrayDragStart(ev) {
  _dragState = {
    group: ev.currentTarget.dataset.groupKey,
    idx: Number(ev.currentTarget.dataset.idx),
  };
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
  const srcGroup = _dragState.group;
  const srcIdx = _dragState.idx;
  const dstGroup = ev.currentTarget.dataset.groupKey;
  const dstIdx = Number(ev.currentTarget.dataset.idx);
  if (srcGroup !== dstGroup || srcIdx === dstIdx) return;
  moveBay(String(srcGroup), srcIdx, dstIdx);
}

function onTrayDragEnd(ev) {
  ev.currentTarget.classList.remove('dragging');
  _dragState = null;
  $$('.drop-target').forEach(el => el.classList.remove('drop-target'));
}

/* 机箱是物理设备，一台机器只有一个前面板。
   机器里可以插多张 RAID 卡，但盘位属于机箱：控制器只是每块盘的一个
   属性（显示在托盘上），不能按卡把前面板拆成多个。 */
const MACHINE_GROUP = 'machine';

const TONE_RANK = { ok: 0, unknown: 1, warn: 2, crit: 3 };

/* 多控制器场景下取最严重的一个色调：一张卡正常不能掩盖另一张卡异常 */
function worstTone(values, fallback) {
  let worst = fallback || 'ok';
  (values || []).forEach(v => {
    const t = ctrlHealthTone(v);
    if ((TONE_RANK[t] || 0) > (TONE_RANK[worst] || 0)) worst = t;
  });
  return worst;
}

/* 整机盘位顺序：控制器 → 机箱 → 槽位 */
function machineDiskList(st) {
  return (st.physical_disks || []).slice().sort((a, b) =>
    (bayCtrl(a) - bayCtrl(b)) ||
    (Number(a.eid) - Number(b.eid)) ||
    (Number(a.slot) - Number(b.slot))
  );
}

/* 真实 19" 机箱的盘位比例。
   机箱内宽 482.6mm，每行 4 个盘位 → 盘位面宽 120.65mm；
   行高 = 机箱高度(u × 44.45mm) ÷ 行数。
   由这个比例反推盘位高度，面板变宽时整体等比放大，
   而不是把盘位横向拉成细长条。 */
const RACK_INNER_MM = 482.6;
const U_MM = 44.45;

function bayAspect(spec) {
  const rows = Math.max(1, spec.bays / 4);
  // spec.u 形如 '2U'/'4U'，必须取数字部分：
  // Number('2U') 是 NaN，会让 aspect-ratio 整条声明失效。
  const uCount = parseInt(spec.u, 10) || 2;
  const rowHeightMm = (uCount * U_MM) / rows;
  return (RACK_INNER_MM / 4) / rowHeightMm;
}

/* 计算整机盘位规划。盘数超过所选机箱规格时自动扩档，
   否则多出来的盘会直接消失（用户只会在别的页面发现盘少了）。 */
function machineBayPlan(st) {
  const list = machineDiskList(st);
  const explicit = state.bayCapacity && state.bayCapacity !== 'auto';
  let spec = explicit ? baySpecFor(Number(state.bayCapacity)) : null;
  let fromSes = false;
  if (!spec) {
    // 「自动」= 让系统决定。背板实测能给出真实槽位数，比按已装盘数
    // 倒推一个最小值准确得多，所以优先用它。
    const sesBays = sesSlotCount();
    const sesSpec = sesBays ? baySpecFor(sesBays) : null;
    if (sesSpec) {
      spec = sesSpec;
      fromSes = true;
    }
  }
  if (!spec) spec = autoBaySpec(list.length);
  let capacity = spec.bays;
  let expanded = false;
  if (list.length > capacity) {
    spec = autoBaySpec(list.length);
    capacity = spec.bays;
    expanded = true;
  }
  return {
    list, spec, capacity, expanded,
    fromSes,
    // 槽位数来自背板时不能声称是哪个 U —— SES 只知道槽位，不知道机箱高度。
    uLabel: fromSes ? '背板实测' : spec.u,
    explicitSpec: explicit ? Number(state.bayCapacity) : null,
  };
}

function moveBay(groupKey, from, to) {
  const st = state.status;
  const plan = machineBayPlan(st);
  const slots = arrangeDisks(plan.list, plan.capacity, groupKey);
  if (from < 0 || from >= slots.length || to < 0 || to >= slots.length || !slots[from]) return;
  const moved = slots[from];
  slots[from] = slots[to];
  slots[to] = moved;
  state.bayLayout[String(groupKey)] = slots.map(d => (d ? bayKey(d) : null));
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
      wrap.innerHTML = stateFromError(e);
      return;
    }
  }
  const disks = st.physical_disks || [];
  const ctrl = st.controller || {};
  loadBayLayout();
  // 背板数据要先拿到：盘位数与槽位对齐都依赖它。
  await ensureSes();
  wrap.innerHTML = '';
  if (!disks.length) {
    wrap.innerHTML = stateBlock('empty', '没有物理磁盘', '控制器没有报告任何物理盘；刚插入的盘通常要等下一轮采集。');
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

  // 一台机器一个前面板：盘位属于机箱，控制器只是每块盘的属性。
  const plan = machineBayPlan(st);
  const list = plan.list;
  const capacity = plan.capacity;
  const uLabel = plan.uLabel;
  const groupKey = MACHINE_GROUP;
  const placed = arrangeDisks(list, capacity, groupKey);
  const controllers = ctrlListOf(st);

  // 背板视角：真实槽位表，以及"背板看见盘、控制器没认到"的槽位集合
  const sesMap = sesSlotMap();
  const sesBays = sesSlotCount();
  const sesTotals = (_ses && _ses.totals) || {};
  const sesAligned = sesBays > 0 && sesBays === capacity;
  const ctrlSlots = new Set(list.map(d => Number(d.slot)));
  const ghostSlots = new Set();
  if (sesAligned) {
    Object.values(sesMap).forEach(s => {
      if (s.installed && !ctrlSlots.has(Number(s.slot))) ghostSlots.add(Number(s.slot));
    });
  }
  const sesFoot = sesBays
    ? (plan.fromSes ? '' : ` · 背板实测 ${sesBays} 槽 / 已插 ${sesTotals.installed || 0}`)
      + (sesTotals.faults ? ` · 背板 ${sesTotals.faults} 个故障灯` : '')
    : '';

  // 显式选了规格但和背板对不上时，提示并给一键对齐。
  // 不静默改用户的选择，但也不能让"面板画 8 个槽、机器其实有 16 个"
  // 这种偏差一直藏着。
  if (sesBays && plan.explicitSpec && plan.explicitSpec !== sesBays && baySpecFor(sesBays)) {
    const hint = document.createElement('span');
    hint.className = 'tiny';
    hint.innerHTML = `背板实测 <strong>${sesBays}</strong> 槽，当前规格 <strong>${plan.explicitSpec}</strong> 槽——`
      + `空槽位置无法与硬件对应。<button class="btn sm" id="btn-bay-align" type="button">按背板对齐</button>`;
    tools.appendChild(hint);
    hint.querySelector('#btn-bay-align').addEventListener('click', () => {
      state.bayCapacity = String(sesBays);
      const sel = $('#bay-capacity');
      if (sel) sel.value = String(sesBays);
      renderEnclosures();
    });
  }
  if (sesBays && sesAligned && !plan.explicitSpec) {
    const hint = document.createElement('span');
    hint.className = 'tiny';
    hint.textContent = `盘位数取自背板实测（${sesBays} 槽）；可在「机箱规格」中手动指定。`;
    tools.appendChild(hint);
  }

  const ctrlTone = worstTone(controllers.map(c => c.health), 'ok');
  const bbuTone = worstTone(controllers.map(c => c.bbu_state), 'ok');
  const healthText = HEALTH_TEXT[st.health] || st.health || '未知';
  const rocText = controllers
    .filter(c => c.roc_temp != null)
    .map(c => `C${c.ctrl} ${c.roc_temp}°`)
    .join(' / ') || '—';
  const models = [...new Set(controllers.map(c => c.model).filter(Boolean))];
  const modelText = models.length === 0
    ? 'Controller'
    : models.length === 1
      ? `${controllers.length} × ${models[0]}`
      : models.join(' / ');
  const perCtrl = new Map();
  list.forEach(d => {
    const c = bayCtrl(d);
    perCtrl.set(c, (perCtrl.get(c) || 0) + 1);
  });
  // 空的控制器也要列出来（C0 ×0）：一台机器上装了卡却没接盘，
  // 是运维需要知道的状态，不能因为没有盘就从面板上消失。
  const ctrlSummary = controllers
    .map(c => `C${c.ctrl} ×${perCtrl.get(Number(c.ctrl)) || 0}`)
    .join(' · ') || '—';

  {
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
            <span class="chassis-name">DataMax-LSI</span>
            <span class="chassis-model">${esc(modelText)}</span>
            <span class="chassis-asset">${esc(st.host || '')} · ${esc(ctrlSummary)}</span>
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
          <span class="panel-label">${uLabel} · ${capacity} 盘位 · 已装 ${list.length}${plan.expanded ? '（按盘数自动升档）' : ''}${sesFoot}</span>
          <span class="chassis-vents"></span>
        </div>
      </div>`;
    const rack = view.querySelector('.bay-rack');
    rack.style.setProperty('--tray-aspect', bayAspect(plan.spec).toFixed(3));
    for (let i = 0; i < capacity; i++) {
      const d = placed[i];
      const tray = document.createElement('button');
      tray.type = 'button';
      tray.dataset.groupKey = groupKey;
      tray.dataset.idx = String(i);
      if (!d) {
        // 面板槽位数与背板一致时，显示序号就是物理槽号，可以把
        // "背板看见盘、控制器没认到"标在正确的槽位上。
        const ses = sesAligned ? sesMap[i] : null;
        if (ses && ghostSlots.has(i)) {
          tray.className = 'tray tray-ses-ghost';
          tray.title = `槽位 ${i}：背板报告已插盘，但 RAID 控制器没有识别到这块盘。`
            + '常见原因是背板到卡的链路接触不良、盘未上电，或盘刚插入尚未枚举。';
          tray.innerHTML = `
            <span class="tray-handle"></span>
            <span class="tray-led"></span>
            <span class="tray-meta">
              <span class="tray-slot">#${i}</span>
              <span class="tray-model">背板有盘 · 控制器未识别</span>
            </span>
            <span class="tray-state">未识别</span>`;
        } else {
          tray.className = 'tray tray-empty';
          tray.disabled = true;
          tray.innerHTML = `<span class="tray-slot">${sesAligned ? '#' + i : '空槽'}</span>`;
        }
      } else {
        const tone = diskVisualTone(d);
        const hs = isHotSpare(d);
        const prog = bayProgress(d);
        const ses = sesMap[Number(d.slot)];
        const sesFault = !!(ses && ses.fault);
        tray.className = 'tray tray-' + tone + (hs ? ' tray-hs' : '')
          + ((d.locate || (ses && ses.locate)) ? ' tray-locate' : '')
          + (sesFault ? ' tray-ses-fault' : '');
        const stateText = (hs ? '热备 · ' : '') + (d.state || '—');
        const tempText = d.temperature != null ? d.temperature + '°C' : '—';
        tray.innerHTML = `
          <span class="tray-handle"></span>
          <span class="tray-led"></span>
          <span class="tray-meta">
            <span class="tray-slot">${esc(d.label)}<span class="tray-ctrl" title="所属 RAID 控制器">C${bayCtrl(d)}</span></span>
            <span class="tray-model">${esc(d.model || '—')}</span>
          </span>
          <span class="tray-temp">${tempText}</span>
          <span class="tray-state">${esc(stateText)}${sesFault ? '<span class="tray-ses-badge" title="背板故障灯亮">SES 故障</span>' : ''}</span>
          ${prog ? `<span class="tray-progress"><i style="width:${prog.pct}%;background:${prog.color}"></i></span>` : ''}`;
        tray.title = `${d.model || '—'} · ${d.sn || '—'} · ${d.state || '—'} · ${tempText}`
          + (ses
            ? `\n背板槽位 #${ses.slot}：${ses.status}`
              + ` · 供电 ${ses.power_status || '—'}`
              + ` · 故障灯 ${ses.fault ? '亮' : '灭'}`
              + ` · 定位灯 ${ses.locate ? '亮' : '灭'}`
            : '');
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
  }
  renderBaySide(st, capacityTotal(plan));
}

function capacityTotal(plan) {
  return { capacity: plan.capacity, u: plan.uLabel, filled: plan.list.length };
}

/* 前面板旁边的机器级信息。

   面板本身要限制宽度（盘位不能无限放大），空出来的横向空间就用来放
   "一眼要看的数据"——盘位占用、状态分布、温度、容量、控制器。这些内容
   原来分散在别的卡片里，放在面板旁边反而是它们最该出现的位置。 */
function renderBaySide(st, plan) {
  const box = $('#bay-side');
  if (!box) return;
  const disks = st.physical_disks || [];
  const ctrls = ctrlListOf(st);
  const vds = st.virtual_disks || [];
  const degraded = vds.filter(v => String(v.state || '').toLowerCase() !== 'optl').length;
  const cap = plan.capacity || 0;
  const filled = disks.length;
  const temps = disks.map(d => d.temperature).filter(t => typeof t === 'number');
  const avg = temps.length ? (temps.reduce((a, b) => a + b, 0) / temps.length) : null;

  // 盘位状态分布：按可视化面板的口径归类，和面板上的颜色一一对应
  const buckets = { 正常: 0, 关注: 0, 严重: 0, 热备: 0 };
  disks.forEach(d => {
    const tone = diskVisualTone(d);
    const hs = isHotSpare(d);
    if (hs) buckets['热备'] += 1;
    else if (tone === 'crit') buckets['严重'] += 1;
    else if (tone === 'warn') buckets['关注'] += 1;
    else buckets['正常'] += 1;
  });
  const empty = Math.max(0, cap - filled);
  const maxN = Math.max(1, filled, empty);

  const sizeGB = disks.reduce((a, d) => {
    const m = String(d.size || '').match(/([\d.]+)\s*GB/i);
    return a + (m ? parseFloat(m[1]) : 0);
  }, 0);

  const row = (label, n, cls) => `<div class="bay-dist-row">
      <span>${esc(label)}</span>
      <span class="bay-dist-bar"><i class="${cls}" style="width:${Math.round((n / maxN) * 100)}%"></i></span>
      <span class="bay-dist-val">${n}</span>
    </div>`;

  box.innerHTML = `
    <div class="bay-stat-grid">
      <div class="bay-stat">
        <div class="k">盘位占用</div>
        <div class="v">${filled} / ${cap}</div>
        <div class="s">空槽 ${empty} 个</div>
      </div>
      <div class="bay-stat">
        <div class="k">已装容量</div>
        <div class="v">${sizeGB >= 1000 ? (sizeGB / 1000).toFixed(2) + 'T' : Math.round(sizeGB) + 'G'}</div>
        <div class="s">${disks.length} 块物理盘</div>
      </div>
      <div class="bay-stat">
        <div class="k">磁盘均温</div>
        <div class="v">${avg != null ? avg.toFixed(1) + '°' : '—'}</div>
        <div class="s">${temps.length ? `最高 ${Math.max.apply(null, temps)}°C` : '无温度数据'}</div>
      </div>
      <div class="bay-stat">
        <div class="k">阵列</div>
        <div class="v">${esc(vds.length)}</div>
        <div class="s">${degraded ? degraded + ' 个非最优' : '全部正常'}</div>
      </div>
    </div>

    <div class="bay-sec">
      <div class="bay-side-sec-title">盘位状态分布</div>
      <div class="bay-dist">
        ${row('正常', buckets['正常'], '')}
        ${row('关注', buckets['关注'], 'warn')}
        ${row('严重', buckets['严重'], 'crit')}
        ${row('热备', buckets['热备'], 'hs')}
        ${row('空槽', empty, 'empty')}
      </div>
    </div>

    <div class="bay-sec">
      <div class="bay-side-sec-title">控制器</div>
      ${ctrls
        .map(
          c => `<div class="bay-ctrl-row">
          <span class="bay-ctrl-id">C${esc(c.ctrl)}</span>
          <span class="bay-ctrl-model" title="${esc(c.model || '')}">${esc((c.model || '').replace(/^AVAGOMegaRAID/, 'MegaRAID '))}</span>
          <span class="tiny mono">${c.roc_temp != null ? esc(c.roc_temp) + '°C' : '—'}</span>
        </div>`
        )
        .join('') || '<div class="muted tiny">无控制器数据</div>'}
    </div>`;
}

async function loadLedger() {
  const tb = $('#ledger-table tbody');
  tb.innerHTML = '<tr><td colspan="12" class="muted">加载中…</td></tr>';
  let data;
  try {
    data = await api('/api/disk_ledger');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="12">${stateFromError(e)}</td></tr>`;
    return;
  }
  const rows = data.disks || [];
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="12">' + stateBlock('empty', '没有物理磁盘', '控制器未报告任何磁盘。', '', true) + '</td></tr>';
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
    wear.innerHTML = stateFromError(e);
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
    : stateBlock('empty', '暂无磨损数据', '尚未采到 SMART 属性，等待下一轮采集。');

  const lifeRows = data.lifetime_top || [];
  life.innerHTML = lifeRows.length
    ? lifeRows.map(x => `<div class="rank-row">
        <span class="rank-label">${esc(x.label)}</span>
        <span class="rank-sub">${esc(x.model || '—')}</span>
        <span class="rank-metrics">${esc(x.power_on_hours)} 小时 · ${esc(x.power_on_days)} 天</span>
      </div>`).join('')
    : stateBlock('empty', '暂无通电时长数据', '磁盘尚未回报 Power On Hours。');
}

async function loadReplacements() {
  const el = $('#replace-list');
  el.innerHTML = '<div class="loading-line">加载中…</div>';
  let data;
  try {
    data = await api('/api/disk_ledger');
  } catch (e) {
    el.innerHTML = stateFromError(e);
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
    return `<div class="replace-item" data-ctrl="${esc(bayCtrl(d))}" data-eid="${esc(d.eid)}" data-slot="${esc(d.slot)}">
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
        const ctrlId = Number(item.dataset.ctrl);
        const eid = Number(item.dataset.eid);
        const slot = Number(item.dataset.slot);
        const d = (state.status && state.status.physical_disks || []).find(
          x => bayCtrl(x) === ctrlId && Number(x.eid) === eid && Number(x.slot) === slot
        );
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
    body.innerHTML = stateFromError(e);
    return;
  }
  const cfg = data.config || {};
  const opts = [0, 1, 2, 3, 4].map(n => `<option value="${n}"${Number(cfg.desired_global) === n ? ' selected' : ''}>${n} 块</option>`).join('');
  const eligible = data.eligible || [];
  const eligibleHtml = eligible.length
    ? eligible.map(d => `<div class="hs-row">
        <span class="mono">${esc(d.label)}</span>
        <span class="tiny">${esc(d.model || '')}</span>
        ${state.isAdmin ? `<button class="btn sm" data-hs="${esc(bayCtrl(d))}:${esc(d.eid)}:${esc(d.slot)}">设为全局热备</button>` : '<span class="tiny">可配置</span>'}
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
        const [ctrlId, eid, slot] = parseDiskKey(btn.dataset.hs);
        const d = (state.status && state.status.physical_disks || []).find(
          x => bayCtrl(x) === ctrlId && Number(x.eid) === eid && Number(x.slot) === slot
        );
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
    body.innerHTML = stateFromError(e);
    return;
  }
  const rows = data.disks || [];
  if (!rows.length) {
    body.innerHTML = stateBlock('empty', '暂无寿命数据', '需要磁盘回报磨损指标后才能估算剩余寿命。');
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
    if (['overview', 'storage', 'perf', 'logs', 'ops', 'users', 'reports', 'hosts'].includes(v)) {
      localStorage.setItem('lsi-view', v);
    }
  } catch (e) { /* 忽略 */ }
  $$('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  ['overview', 'storage', 'perf', 'logs', 'ops', 'users', 'reports', 'hosts'].forEach(name => {
    $('#view-' + name).classList.toggle('hidden', name !== v);
  });
  if (v === 'overview') tabifyOverview(); else tabifyView(v);
  if (v === 'storage' && !state.storageLoaded) loadStorage();
  if (v === 'storage' && !state.fsUsage) loadFsUsage();
  if (v === 'storage' && !state.nfsLoaded) loadNfs();
  if (v === 'storage' && !state.bcacheLoaded) loadBcache();
  if (v === 'storage') loadCapacityForecast();
  if (v === 'storage') loadDataBackup();
  if (v === 'storage') loadBackupSchedule();
  if (v === 'storage') loadFsPaths();
  if (v === 'perf') loadPerfLive();
  if (v === 'perf') loadPerfTrend();
  if (v === 'perf') loadIoBench();
  if (v === 'ops' && !state.opsLoaded) loadOps();
  if (v === 'ops') loadBackup();
  if (v === 'ops') loadConfigSnapshots();
  if (v === 'ops') loadRecovery();
  if (v === 'ops') loadCacheProtection();
  if (v === 'ops') loadHardware();
  if (v === 'ops') loadEnclosure();
  if (v === 'ops') loadMaintenance();
  if (v === 'ops') loadCtrlPolicy();
  if (v === 'users' && !state.usersLoaded) loadUsers();
  if (v === 'reports') loadReports();
  if (v === 'hosts') loadHosts();
  if (v === 'hosts') loadFleetAlerts();
}


/* ---------- 页面内标签分组 ----------
   每页卡片太多，滚动很长。这里把每个视图的顶层卡片按每 4 块一组
   自动分组，标签名取该组第一块卡片的标题——不写死映射，后续加卡片
   也能自动归入。只重排 DOM 层级，卡片本身与所有 id 保持不变。 */
/* ---------- 页面内标签分组 ----------
   每页卡片太多、滚动很长。默认每 3 块一组；少数卡片需要独立成组
   （比如"数据备份与恢复"是一整块业务），用 SPECIAL 单独拎出来。
   只重排 DOM 层级，卡片本身与所有 id 保持不变。 */
const TAB_SPECIAL = {
  'card-data-backup': '数据备份与恢复',
};


/* 概览页专用分组：内容是静态的 13 个区块，按业务含义划分，
   比"每 3 块一组"更符合阅读顺序。 */
function tabifyOverview() {
  const view = document.getElementById('view-overview');
  if (!view || view.dataset.tabbed === '1') return;
  const kids = [...view.children].filter(el => el.nodeType === 1);
  if (kids.length < 6) return;
  const groups = [
    { label: '总览', idx: [0, 2] },
    { label: '系统与文件', idx: [1, 11] },
    { label: '告警与健康', idx: [3, 4] },
    { label: '磁盘与阵列', idx: [5, 8, 9, 10] },
    { label: '维护与趋势', idx: [6, 7] },
    { label: '授权', idx: [12] },
  ];
  const bar = document.createElement('div');
  bar.className = 'subtabs';
  const panels = [];
  for (const g of groups) {
    const items = g.idx.map(i => kids[i]).filter(Boolean);
    if (!items.length) continue;
    const panel = document.createElement('div');
    panel.className = 'subpanel';
    items.forEach(el => panel.appendChild(el));
    view.appendChild(panel);
    panels.push(panel);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'subtab';
    btn.textContent = g.label;
    btn.addEventListener('click', () => {
      bar.querySelectorAll('.subtab').forEach(b => b.classList.toggle('active', b === btn));
      panels.forEach(p => p.classList.toggle('active', p === panel));
    });
    bar.appendChild(btn);
  }
  panels[0]?.classList.add('active');
  bar.firstChild?.classList.add('active');
  view.insertBefore(bar, view.firstChild);
  view.dataset.tabbed = '1';
}

function tabifyView(name) {
  const view = document.getElementById('view-' + name);
  if (!view || view.dataset.tabbed === '1') return;
  // view 本身是 grid 的（概览页 12 列栅格）不做分组：
  // `.grid-hero`/`.grid-stats` 靠 display:contents 直接参与栅格，
  // 外面包一层会让这些选择器失效、整页散架。
  if (getComputedStyle(view).display === 'grid') return;

  const kids = [...view.children].filter(el => el.nodeType === 1);
  if (kids.length <= 4) return;

  const pick = (el, sel) => (el.querySelector(sel)?.textContent || '').trim();
  const labelOf = (el) => {
    const t = pick(el, '.card-head strong') || pick(el, 'h3') || pick(el, 'strong') || '内容';
    return t.slice(0, 16);
  };

  const chunks = [];
  let buf = [];
  const flush = () => { if (buf.length) { chunks.push({ label: labelOf(buf[0]), items: buf }); buf = []; } };
  for (const el of kids) {
    const special = TAB_SPECIAL[el.id];
    if (special) { flush(); chunks.push({ label: special, items: [el] }); continue; }
    buf.push(el);
    if (buf.length >= 3) flush();
  }
  flush();

  const bar = document.createElement('div');
  bar.className = 'subtabs';
  const panels = [];
  for (const chunk of chunks) {
    const panel = document.createElement('div');
    panel.className = 'subpanel';
    chunk.items.forEach(el => panel.appendChild(el));
    view.appendChild(panel);
    panels.push(panel);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'subtab';
    btn.textContent = chunk.label;
    btn.addEventListener('click', () => {
      bar.querySelectorAll('.subtab').forEach(b => b.classList.toggle('active', b === btn));
      panels.forEach(p => p.classList.toggle('active', p === panel));
    });
    bar.appendChild(btn);
  }
  panels[0]?.classList.add('active');
  bar.firstChild?.classList.add('active');
  view.insertBefore(bar, view.firstChild);
  view.dataset.tabbed = '1';
}

/* ---------- UI 绑定 ---------- */

/* ---------- 报表中心 ---------- */

/* ---------- 授权 ---------- */

/* ---------- 角色与权限 ---------- */

let ROLE_INFO = {
  viewer: { name: '只读', desc: '', perms: '' },
  operator: { name: '运维', desc: '', perms: '' },
  admin: { name: '管理员', desc: '', perms: '' },
  auditor: { name: '审计', desc: '', perms: '' },
};

/* 角色信息从后端下发，前端不写死中文名与权限描述——
   否则以后调整权限矩阵要同时改两处，迟早对不上。 */
function buildRoleInfo(desc) {
  const roles = (desc || {}).roles || {};
  const perms = (desc || {}).perms || {};
  const next = {};
  ['viewer', 'operator', 'admin', 'auditor'].forEach(k => {
    const r = roles[k] || { name: k, desc: '' };
    next[k] = {
      name: r.name || k,
      desc: r.desc || '',
      perms: (r.perms || []).map(p => (perms[p] || {}).label || p).join(' · '),
    };
  });
  if (Object.keys(roles).length) ROLE_INFO = next;
}

function fillRoleSelect() {
  const sel = $('#nu-role');
  if (!sel || sel.dataset.filled === '1') return;
  sel.innerHTML = Object.entries(ROLE_INFO)
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v.name)} — ${esc(v.desc)}</option>`)
    .join('');
  sel.dataset.filled = '1';
}

/* ---------- 多机管理 ---------- */

const HOST_HEALTH_TEXT = { ok: '正常', warn: '警告', crit: '严重', unknown: '未知' };

/* ---------- 多机统一告警视图 ---------- */

async function loadFleetAlerts() {
  const box = $('#fleet-alerts');
  if (!box) return;
  let d;
  try {
    d = await api('/api/hosts/alerts');
  } catch (e) {
    box.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
    $('#fleet-alert-stats').textContent = '';
    return;
  }
  const stats = d.stats || {};
  $('#fleet-alert-stats').textContent = stats.total
    ? `共 ${stats.total} 条 · 错误 ${stats.error || 0} · 警告 ${stats.warning || 0}${
        stats.offline_hosts ? ` · 失联节点 ${stats.offline_hosts}` : ''
      }`
    : '';

  const items = d.alerts || [];
  if (!items.length) {
    box.innerHTML = '<div class="muted">所有节点当前都没有未处理告警</div>';
    return;
  }
  box.innerHTML = items
    .map(
      a => `<div class="fleet-alert ${esc(a.level)}">
      <div class="fa-head">
        <span class="badge ${a.level === 'error' ? 'crit' : a.level === 'warning' ? 'warn' : 'info'}">${
          a.level === 'error' ? '错误' : a.level === 'warning' ? '警告' : '信息'
        }</span>
        <span class="fa-host">${esc(a.host)}${a.local ? ' <span class="tiny">（本机）</span>' : ''}</span>
        <span class="fa-title">${esc(a.title)}${Number(a.count) > 1 ? ` ×${esc(a.count)}` : ''}</span>
        <span class="tiny mono">${esc(a.last_seen || '')}</span>
      </div>
      <div class="fa-detail">${esc(a.detail || '')}</div>
      <div class="fa-ops">
        ${
          a.offline
            ? `<button class="btn sm ghost" data-fa-open="${esc(a.address)}">打开节点</button>`
            : a.alert_id && state.canOperate
              ? `<button class="btn sm" data-fa-ack="${esc(a.host_id)}:${esc(a.alert_id)}">确认</button>`
              : ''
        }
        ${a.offline || !a.address ? '' : `<button class="btn sm ghost" data-fa-jump="${esc(a.address)}">前往节点</button>`}
      </div>
    </div>`
    )
    .join('');

  $$('#fleet-alerts [data-fa-ack]').forEach(b =>
    b.addEventListener('click', async () => {
      const [hid, aid] = b.dataset.faAck.split(':');
      btnLoading(b, true);
      try {
        await api(`/api/hosts/${hid}/alerts/${aid}/ack`, { method: 'POST' });
        toast('告警已确认', 'ok');
        await loadFleetAlerts();
      } catch (e) {
        toast(e.message, 'error');
        btnLoading(b, false);
      }
    })
  );
  $$('#fleet-alerts [data-fa-open], #fleet-alerts [data-fa-jump]').forEach(b =>
    b.addEventListener('click', () =>
      window.open(b.dataset.faOpen || b.dataset.faJump, '_blank')
    )
  );
}

function bindFleetAlertUI() {
  const t = $('#remote-ack-toggle');
  if (t) {
    t.addEventListener('change', async () => {
      try {
        await api('/api/agent/remote_ack', { method: 'POST', body: { enabled: t.checked } });
        toast(t.checked ? '已允许中心代为确认告警' : '已禁止中心代为确认告警', 'ok');
      } catch (e) {
        toast(e.message, 'error');
        t.checked = !t.checked;
      }
    });
  }
}

/* ---------- 缓存保护与写缓存 ---------- */

function renderCacheProtection(list) {
  const box = $('#battery-body');
  if (!box) return;
  const items = list || [];
  if (!items.length) {
    box.innerHTML = stateBlock('empty', '没有控制器数据', 'storcli 未返回控制器信息，检查驱动与工具是否可用。');
    return;
  }
  box.innerHTML = items
    .map(cp => {
      const tone = cp.severity === 'crit' ? 'crit' : cp.severity === 'warn' ? 'warn' : 'ok';
      const unit = cp.unit || {};
      const unitText = unit.present
        ? `${unit.model || '缓存单元'} · ${unit.state || '状态未知'}${unit.temperature != null ? ` · ${unit.temperature}°C` : ''}`
        : `未安装（${unit.raw_presence || '未知'}）`;
      const ports = (cp.ports || [])
        .map(
          p => `<div class="cache-port">
          <span class="mono">${esc(p.vd)}</span>
          <span class="cache-chip ${p.temporarily_disabled ? 'warn' : p.write.includes('写回') ? 'crit' : 'ok'}">写：${esc(p.write)}</span>
          <span class="cache-chip">读：${esc(p.read)}</span>
          <span class="tiny mono">${esc(p.cache_code || '—')}</span>
        </div>`
        )
        .join('');
      const findings = (cp.findings || [])
        .map(
          f => `<div class="cache-finding ${esc(f.level)}">
          <div class="cf-head">
            <span class="badge ${f.level === 'crit' ? 'crit' : f.level === 'warn' ? 'warn' : f.level === 'ok' ? 'ok' : 'info'}">${
              { crit: '严重', warn: '关注', ok: '正常', info: '说明' }[f.level] || f.level
            }</span>
            <strong>${esc(f.title)}</strong>
          </div>
          <div class="cf-detail">${esc(f.detail)}</div>
          ${f.advice ? `<div class="cf-advice">建议：${esc(f.advice)}</div>` : ''}
        </div>`
        )
        .join('');
      return `<div class="cache-block">
        <div class="cache-head">
          <span class="badge ${tone}">${tone === 'crit' ? '危险' : tone === 'warn' ? '需关注' : '正常'}</span>
          <strong>控制器 C${esc(cp.ctrl)}</strong>
          <span class="tiny">${esc(unitText)}</span>
        </div>
        ${ports}
        <div class="cache-findings">${findings}</div>
      </div>`;
    })
    .join('');
}

async function loadCacheProtection() {
  const box = $('#battery-body');
  if (!box) return;
  try {
    const st = await api('/api/status');
    state.status = st;
    renderCacheProtection(st.cache_protection);
  } catch (e) {
    box.innerHTML = stateFromError(e);
  }
}

/* ---------- 换卡恢复向导 ---------- */

let _recoveryData = [];

async function loadRecovery() {
  const box = $('#recovery-body');
  if (!box) return;
  box.innerHTML = '<div class="loading-line">诊断中…</div>';
  let d;
  try {
    d = await api('/api/recovery/diagnose');
  } catch (e) {
    box.innerHTML = `<div class="muted">诊断失败：${esc(e.message)}</div>`;
    return;
  }
  _recoveryData = d.diagnoses || [];
  const sel = $('#recovery-ctrl');
  const ids = d.controllers || [];
  if (sel && sel.dataset.filled !== ids.join(',')) {
    sel.innerHTML = ids.map(id => `<option value="${esc(id)}">控制器 C${esc(id)}</option>`).join('');
    sel.dataset.filled = ids.join(',');
  }
  const cur = sel && sel.value !== '' ? Number(sel.value) : (ids[0] != null ? Number(ids[0]) : null);
  renderRecovery(cur);
}

function renderRecovery(ctrlId) {
  const box = $('#recovery-body');
  if (!box) return;
  const d = _recoveryData.find(x => Number(x.ctrl) === Number(ctrlId)) || _recoveryData[0];
  if (!d) {
    box.innerHTML = stateBlock('empty', '没有可诊断的控制器', '换卡向导需要至少一张已识别的 RAID 卡。');
    return;
  }
  const tone = d.severity === 'crit' ? 'crit' : d.severity === 'warn' ? 'warn' : 'ok';
  const toneText = { crit: '需要处理', warn: '需要确认', ok: '正常' }[tone];

  const evidence = (d.evidence || []).length
    ? `<div class="rec-evidence">${d.evidence
        .map(
          e => `<div class="rec-ev-item ${esc(e.level)}">
          <span class="badge ${e.level === 'high' ? 'crit' : e.level === 'medium' ? 'warn' : 'info'}">${
            e.level === 'high' ? '重要' : e.level === 'medium' ? '关注' : '信息'
          }</span><span>${esc(e.text)}</span></div>`
        )
        .join('')}</div>`
    : '';

  const steps = (d.steps || [])
    .map(
      s => `<div class="rec-step ${s.danger ? 'danger' : ''} ${s.blocker ? 'blocker' : ''}">
      <span class="rec-num">${esc(s.n)}</span>
      <div class="rec-body">
        <div class="rec-title">${esc(s.title)}${s.blocker ? ' <span class="badge crit">阻断</span>' : ''}${s.danger ? ' <span class="badge warn">高风险</span>' : ''}</div>
        <div class="rec-detail">${esc(s.detail)}</div>
        ${s.confirm ? `<div class="rec-confirm">执行前确认：${esc(s.confirm)}</div>` : ''}
        ${s.action ? `<button class="btn sm" data-rec-action="${esc(s.action)}" data-rec-ctrl="${esc(d.ctrl)}">${esc({
          snapshot: '记录当前配置',
          foreign_import: '导入外来配置',
          diff: '与上次快照比对',
        }[s.action] || '执行')}</button>` : ''}
      </div>
    </div>`
    )
    .join('');

  box.innerHTML = `
    <div class="rec-head">
      <span class="badge ${tone}">${esc(toneText)}</span>
      <strong>${esc(d.title)}</strong>
      ${d.snapshot ? `<span class="tiny">比对基准：快照 #${esc(d.snapshot.id)}（${esc(d.snapshot.ts)}）</span>` : '<span class="tiny">无比对基准</span>'}
    </div>
    <div class="rec-summary">${esc(d.summary)}</div>
    ${evidence}
    <div class="rec-steps">${steps}</div>
    ${
      d.controller
        ? `<div class="tiny rec-meta">当前控制器：${esc(d.controller.model || '—')} · 固件 ${esc(
            d.controller.fw || '—'
          )} · 健康 ${esc(d.controller.health || '—')} · ${esc(d.controller.num_disks || 0)} 盘 / ${esc(
            d.controller.num_vds || 0
          )} 阵列</div>`
        : ''
    }`;

  $$('#recovery-body [data-rec-action]').forEach(b =>
    b.addEventListener('click', () => runRecoveryAction(b.dataset.recAction, Number(b.dataset.recCtrl), b))
  );
}

async function runRecoveryAction(action, ctrlId, btn) {
  if (action === 'snapshot') {
    btnLoading(btn, true);
    try {
      await api('/api/config/snapshots', { method: 'POST', body: { ctrl: ctrlId, note: '恢复流程记录' } });
      toast('已记录当前配置', 'ok');
      await loadConfigSnapshots();
      await loadRecovery();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btnLoading(btn, false);
    }
    return;
  }
  if (action === 'diff') {
    await loadConfigSnapshots();
    await showConfigDiff();
    const el = $('#config-diff');
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    else if (el) el.classList.remove('hidden');
    return;
  }
  if (action === 'foreign_import') {
    confirmModal(
      '导入外来阵列配置',
      `<p>将把磁盘上的原阵列元数据导回控制器 C${ctrlId}。</p>
       <div class="restore-warn">
         <strong>执行前请确认</strong>
         <ul>
           <li>所有成员盘都已插到位（缺盘会导致导入出降级阵列）</li>
           <li><strong>没有</strong>用这些盘创建过新阵列——建过阵列元数据已被覆盖，导入无法找回数据</li>
           <li>已经记录了一份配置快照作为回退依据</li>
         </ul>
       </div>`,
      '确认导入',
      true,
      async () => {
        const r = await api('/api/foreign_import', {
          method: 'POST',
          body: { ctrl: ctrlId, acknowledge: true },
        });
        if (r && r.task) {
          toast(`导入任务已提交（#${r.task.id}）`, 'ok');
          openTaskDrawer();
        } else {
          toast('外来配置已导入', 'ok');
        }
        await loadRecovery();
      }
    );
  }
}

function bindRecoveryUI() {
  const run = $('#btn-recovery-run');
  if (run) {
    run.addEventListener('click', async () => {
      btnLoading(run, true);
      try {
        _recoveryData = [];
        await loadRecovery();
      } finally {
        btnLoading(run, false);
      }
    });
  }
  const sel = $('#recovery-ctrl');
  if (sel) sel.addEventListener('change', () => renderRecovery(Number(sel.value)));
}

/* ---------- 控制器配置快照 ---------- */

const CONFIG_REASON = { baseline: '基线', auto: '自动', drift: '检测到变更', manual: '手动记录' };

async function loadConfigSnapshots() {
  const tb = $('#config-table tbody');
  if (!tb) return;
  let d;
  try {
    d = await api('/api/config/snapshots');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="7" class="muted">${esc(e.message)}</td></tr>`;
    return;
  }
  state.configData = d;
  const latest = d.latest;
  $('#config-latest').textContent = latest
    ? `C${d.ctrl} 最近快照 ${latest.ts} · 指纹 ${latest.hash}`
    : `C${d.ctrl} 暂无快照，下次采集会自动建立基线`;

  const hist = d.history || [];
  if (!hist.length) {
    tb.innerHTML = '<tr><td colspan="7">' + stateBlock('empty', '还没有配置快照', '下次采集会自动建立基线，之后每次配置变化都会记录。', '', true) + '</td></tr>';
    return;
  }
  tb.innerHTML = hist
    .map(
      h => `<tr>
      <td class="num">${esc(h.id)}</td>
      <td class="num">${esc(h.ts)}</td>
      <td class="num">${esc(h.hash)}</td>
      <td>${h.reason === 'drift'
        ? '<span class="badge warn">检测到变更</span>'
        : `<span class="badge">${esc(CONFIG_REASON[h.reason] || h.reason || '自动')}</span>`}</td>
      <td>${esc(h.operator || '—')}</td>
      <td>${esc(h.note || '—')}</td>
      <td class="ops"><button class="btn sm ghost" data-detail="${esc(h.id)}">查看</button></td>
    </tr>`
    )
    .join('');
  $$('#config-table [data-detail]').forEach(b =>
    b.addEventListener('click', async () => {
      try {
        const r = await api('/api/config/snapshots/' + b.dataset.detail);
        const s = r.snapshot;
        showModal({
          title: `配置快照 #${s.id} · C${s.ctrl}`,
          body: `<dl class="kv">
              <dt>时间</dt><dd>${esc(s.ts)}</dd>
              <dt>指纹</dt><dd class="mono">${esc(s.hash)}</dd>
              <dt>型号</dt><dd>${esc(s.data.model || '—')}</dd>
              <dt>固件</dt><dd>${esc(s.data.firmware || '—')}</dd>
            </dl>
            <div class="tiny" style="margin:12px 0 6px">控制器设置</div>
            <pre class="smart-pre">${esc(JSON.stringify(s.data.settings || {}, null, 1))}</pre>
            <div class="tiny" style="margin:12px 0 6px">虚拟磁盘</div>
            <pre class="smart-pre">${esc(JSON.stringify(s.data.vds || [], null, 1))}</pre>`,
          actions: [{ label: '关闭', handler: closeModal }],
        });
      } catch (e) {
        toast(e.message, 'error');
      }
    })
  );
}

async function showConfigDiff() {
  const box = $('#config-diff');
  if (!box) return;
  let d;
  try {
    d = await api('/api/config/diff');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  box.classList.remove('hidden');
  if (d.ok === false) {
    box.innerHTML = `<div class="muted">${esc(d.error)}</div>`;
    return;
  }
  if (d.note) {
    box.innerHTML = `<div class="muted">${esc(d.note)}</div>`;
    return;
  }
  const changes = d.changes || [];
  if (!changes.length) {
    box.innerHTML = `<div class="cfg-diff-head">快照 #${esc(d.a.id)} → #${esc(d.b.id)}：配置未发生变化</div>`;
    return;
  }
  box.innerHTML = `<div class="cfg-diff-head">
      快照 #${esc(d.a.id)}（${esc(d.a.ts)}） → #${esc(d.b.id)}（${esc(d.b.ts)}）：共 ${changes.length} 项差异
    </div>
    <div class="cfg-diff-list">${changes
      .map(
        c => `<div class="cfg-diff-row ${esc(c.impact || '')}">
        <span class="badge ${c.impact === 'high' ? 'crit' : c.impact === 'medium' ? 'warn' : 'info'}">${
          c.impact === 'high' ? '重要' : c.impact === 'medium' ? '一般' : '信息'
        }</span>
        <span class="cfg-diff-label">${esc(c.label)}</span>
        <span class="cfg-diff-path mono">${esc(c.from)} → ${esc(c.to)}</span>
      </div>`
      )
      .join('')}</div>`;
}

function bindConfigUI() {
  const snap = $('#btn-config-snap');
  if (snap) {
    snap.addEventListener('click', async () => {
      btnLoading(snap, true);
      try {
        await api('/api/config/snapshots', { method: 'POST', body: { note: '手动记录' } });
        toast('已记录当前控制器配置', 'ok');
        await loadConfigSnapshots();
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        btnLoading(snap, false);
      }
    });
  }
  const diff = $('#btn-config-diff');
  if (diff) diff.addEventListener('click', showConfigDiff);
}

/* ---------- 数据与备份 ---------- */

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i++;
  }
  return `${i === 0 ? x : x.toFixed(1)} ${units[i]}`;
}

async function loadBackup() {
  const tb = $('#backup-table tbody');
  if (!tb) return;
  let d;
  try {
    d = await api('/api/backups');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="6" class="muted">${esc(e.message)}</td></tr>`;
    return;
  }
  renderBackup(d);
}

function renderBackup(d) {
  const usage = d.usage || {};
  const cfg = d.config || {};
  $('#backup-usage').innerHTML = [
    ['数据目录', fmtBytes(usage.data_bytes)],
    ['数据库', fmtBytes(usage.db_bytes)],
    ['备份占用', fmtBytes(usage.backup_bytes)],
    ['样本总数', String((d.preview || {}).total_samples || 0)],
  ]
    .map(([k, v]) => `<div class="usage-item"><span class="k">${esc(k)}</span><span class="v mono">${esc(v)}</span></div>`)
    .join('');

  const pend = d.pending;
  const pendBox = $('#backup-pending');
  if (pend) {
    pendBox.classList.remove('hidden');
    pendBox.innerHTML = `<div>
        <strong>有一次待应用的恢复</strong>
        <span class="tiny">${esc(pend.file)} · 暂存于 ${esc(pend.staged_at)} · 操作者 ${esc(pend.operator || '—')}</span>
        <span class="tiny">重启服务后生效。应用前会自动备份当前数据（若策略开启）。</span>
      </div>
      ${state.isAdmin ? '<button class="btn sm ghost" id="btn-restore-cancel">取消恢复</button>' : ''}`;
    const cancel = $('#btn-restore-cancel');
    if (cancel) {
      cancel.addEventListener('click', async () => {
        try {
          await api('/api/backups/restore/cancel', { method: 'POST' });
          toast('已取消待应用的恢复', 'ok');
          await loadBackup();
        } catch (e) {
          toast(e.message, 'error');
        }
      });
    }
  } else {
    pendBox.classList.add('hidden');
    pendBox.innerHTML = '';
  }

  $('#ret-days').value = cfg.retention_days != null ? cfg.retention_days : 0;
  $('#ret-keep').value = cfg.keep_backups != null ? cfg.keep_backups : 10;
  $('#ret-auto').checked = cfg.auto_backup !== false;
  $('#retention-hint').textContent = cfg._retention_locked
    ? '保留天数由环境变量 LSI_RETENTION_DAYS 指定，界面不可修改'
    : '';
  $('#ret-days').disabled = !!cfg._retention_locked;
  renderRetentionPreview(d.preview || {});

  const items = d.backups || [];
  if (!items.length) {
    $('#backup-table tbody').innerHTML = '<tr><td colspan="6">' + stateBlock('empty', '还没有备份', '点击上方「创建备份」生成第一份数据快照。', '', true) + '</td></tr>';
    return;
  }
  $('#backup-table tbody').innerHTML = items
    .map(b => `<tr>
      <td class="num">${esc(b.file)}</td>
      <td>${b.scope === 'full' ? '<span class="badge info">完整</span>' : '<span class="badge">配置</span>'}</td>
      <td class="num">${esc(b.created_at)}</td>
      <td class="num">${esc(fmtBytes(b.bytes))}</td>
      <td>${esc(b.note || '—')}</td>
      <td class="ops">
        <button class="btn sm ghost" data-verify="${esc(b.file)}">校验</button>
        ${state.canAudit ? `<button class="btn sm ghost" data-download="${esc(b.file)}">下载</button>` : ''}
        ${state.isAdmin ? `<button class="btn sm" data-restore="${esc(b.file)}">恢复</button>` : ''}
        ${state.isAdmin ? `<button class="btn sm ghost" data-del="${esc(b.file)}">删除</button>` : ''}
      </td>
    </tr>`)
    .join('');

  $$('#backup-table [data-verify]').forEach(b =>
    b.addEventListener('click', async () => {
      try {
        const r = await api(`/api/backups/${encodeURIComponent(b.dataset.verify)}/verify`);
        toast(r.ok ? `校验通过（${r.manifest.scope === 'full' ? '完整' : '配置'}备份）` : `校验失败：${r.error}`, r.ok ? 'ok' : 'error');
      } catch (e) {
        toast(e.message, 'error');
      }
    })
  );
  $$('#backup-table [data-download]').forEach(b =>
    b.addEventListener('click', () => {
      window.location.href = `/api/backups/${encodeURIComponent(b.dataset.download)}/download`;
    })
  );
  $$('#backup-table [data-del]').forEach(b =>
    b.addEventListener('click', () => {
      confirmModal('删除备份', `<p>确认删除 <strong>${esc(b.dataset.del)}</strong>？删除后该备份文件无法找回。</p>`, '删除', true, async () => {
        await api(`/api/backups/${encodeURIComponent(b.dataset.del)}/remove`, { method: 'POST' });
        toast('备份已删除', 'ok');
        await loadBackup();
      });
    })
  );
  $$('#backup-table [data-restore]').forEach(b =>
    b.addEventListener('click', () => openRestoreModal(b.dataset.restore))
  );
}

function renderRetentionPreview(p) {
  const box = $('#retention-preview');
  if (!box) return;
  if (!p.days) {
    box.innerHTML = '<span class="tiny">当前为永久保留，不会清理任何历史样本。</span>';
    return;
  }
  box.innerHTML = `<span class="tiny">按当前策略将清理 <strong>${esc(p.samples)}</strong> 条样本（共 ${esc(p.total_samples)} 条），
    清理后最早数据为 ${esc(p.cutoff)} 之后。${p.oldest ? `现存最早样本：${esc(p.oldest)}。` : ''}</span>
    ${state.isAdmin ? '<button class="btn sm danger" id="btn-ret-apply">立即执行清理</button>' : ''}`;
  const btn = $('#btn-ret-apply');
  if (btn) {
    btn.addEventListener('click', () => {
      confirmModal(
        '立即执行数据清理',
        `<p>将删除 <strong>${esc(p.samples)}</strong> 条历史样本，此操作不可撤销。</p><p class="tiny">建议先执行一次完整备份。</p>`,
        '确认清理',
        true,
        async () => {
          const r = await api('/api/backups/retention/apply', { method: 'POST' });
          toast(`已删除 ${r.result.removed_samples} 条样本、${r.result.removed_backups} 份备份`, 'ok');
          await loadBackup();
        }
      );
    });
  }
}

function openRestoreModal(name) {
  showModal({
    title: '恢复数据',
    body: `<p>将从备份 <strong class="mono">${esc(name)}</strong> 恢复数据。</p>
      <div class="restore-warn">
        <strong>这是覆盖性操作</strong>
        <ul>
          <li>当前配置与数据库将被备份中的内容替换</li>
          <li>恢复<strong>不会立即生效</strong>——内容先暂存，重启服务后应用</li>
          <li>应用前会自动备份当前数据（若策略开启），可回退</li>
        </ul>
      </div>
      <div class="field" style="margin-top:12px">
        <label for="restore-confirm">输入 <code>RESTORE</code> 以确认</label>
        <input class="input mono" id="restore-confirm" placeholder="RESTORE" />
      </div>`,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: '暂存并等待重启',
        cls: 'danger',
        handler: async btn => {
          btnLoading(btn, true);
          try {
            await api(`/api/backups/${encodeURIComponent(name)}/restore`, {
              method: 'POST',
              body: { acknowledge: true, confirm: $('#restore-confirm').value.trim() },
            });
            toast('已暂存恢复，重启服务后生效', 'ok');
            closeModal();
            await loadBackup();
          } catch (e) {
            toast(e.message, 'error');
            btnLoading(btn, false);
          }
        },
      },
    ],
  });
}

function bindBackupUI() {
  const mk = async scope => {
    try {
      const r = await api('/api/backups', { method: 'POST', body: { scope } });
      toast(`已创建${scope === 'full' ? '完整' : '配置'}备份（${fmtBytes(r.backup.bytes)}）`, 'ok');
      await loadBackup();
    } catch (e) {
      toast(e.message, 'error');
    }
  };
  const bc = $('#btn-backup-config');
  if (bc) bc.addEventListener('click', () => mk('config'));
  const bf = $('#btn-backup-full');
  if (bf) {
    bf.addEventListener('click', () => {
      confirmModal(
        '完整备份',
        '<p>完整备份会打包全部历史样本（CSV 与数据库），数据量大时可能需要几十秒到几分钟，期间磁盘 IO 会升高。</p>',
        '开始备份',
        false,
        () => mk('full')
      );
    });
  }
  const save = $('#btn-ret-save');
  if (save) {
    save.addEventListener('click', async () => {
      btnLoading(save, true);
      try {
        const r = await api('/api/backups/config', {
          method: 'POST',
          body: {
            retention_days: Number($('#ret-days').value) || 0,
            keep_backups: Number($('#ret-keep').value) || 10,
            auto_backup: $('#ret-auto').checked,
          },
        });
        toast('保留策略已保存', 'ok');
        renderRetentionPreview(r.preview || {});
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        btnLoading(save, false);
      }
    });
  }
}

async function loadHosts() {
  const tb = $('#hosts-table tbody');
  if (!tb) return;
  let d;
  try {
    d = await api('/api/hosts');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="12" class="muted">${esc(e.message)}</td></tr>`;
    $('#host-stats').innerHTML = '';
    return;
  }
  renderHosts(d);
  loadAgentToken();
}

const _hostFilter = { q: '', status: 'all', group: 'all', sel: new Set() };
let _hostsPayload = null;

function updateHostSelLabel() {
  const el = $('#host-selected');
  if (el) el.textContent = _hostFilter.sel.size ? `已选 ${_hostFilter.sel.size} 个` : '未选中';
}

async function bulkHostAction(action) {
  const ids = [..._hostFilter.sel];
  if (!ids.length) { toast('请先勾选节点', 'error'); return; }
  let groupName = '';
  if (action === 'group') {
    const g = window.prompt('新的分组名（留空表示清除分组）', '');
    if (g === null) return;
    groupName = g.trim();
  }
  await bulkHostActionDo(action, ids, groupName);
}

async function bulkHostActionDo(action, ids, groupName) {
  try {
    const r = await api('/api/hosts/bulk', {
      method: 'POST',
      body: { ids, action, group_name: groupName || '' },
    });
    const label = action === 'enable' ? '启用' : action === 'disable' ? '停用' : '修改分组';
    toast(`已${label} ${r.changed} 个节点`, 'ok');
    _hostFilter.sel.clear();
    await loadHosts();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function hostMatches(h) {
  const s = h.summary || {};
  if (_hostFilter.q) {
    const blob = `${h.name || ''} ${h.address || ''} ${h.group_name || ''}`.toLowerCase();
    if (!blob.includes(_hostFilter.q.toLowerCase())) return false;
  }
  const st = _hostFilter.status;
  if (st === 'online' && !h.online) return false;
  if (st === 'offline' && h.online) return false;
  if (st === 'alert' && !Number(s.alerts_open || 0)) return false;
  if (st === 'degraded' && !Number(s.vds_degraded || 0)) return false;
  if (_hostFilter.group !== 'all' && String(h.group_name || '') !== _hostFilter.group) return false;
  return true;
}

function renderHosts(d) {
  _hostsPayload = d;
  const all = d.hosts || [];
  _hostsCache = all;
  const stats = d.stats || {};

  const cards = [
    ['节点总数', String(stats.total || 0), '含本机'],
    ['在线', String(stats.online || 0), stats.offline ? `${stats.offline} 台离线` : '全部在线'],
    ['未处理告警', String(all.reduce((a, h) => a + Number((h.summary || {}).alerts_open || 0), 0)), '所有节点合计'],
    ['磁盘总数', String(all.reduce((a, h) => a + Number((h.summary || {}).disks_total || 0), 0)), '所有节点合计'],
  ];
  $('#host-stats').innerHTML = cards.map(([k, v, s], i) => `<div class="card" style="--d:${i}">
      <div class="eyebrow">${esc(k)}</div><div class="stat-value">${esc(v)}</div>
      <div class="stat-sub">${esc(s)}</div></div>`).join('');

  const last = d.last || {};
  $('#hosts-last').textContent = last.ts
    ? `上次采集 ${fmtDateTime(new Date(last.ts * 1000).toISOString().slice(0, 19).replace('T', ' '))}`
    : '';

  // 分组下拉：从现有节点里收集，保持稳定顺序
  const groups = [...new Set(all.map(h => String(h.group_name || '')).filter(Boolean))].sort();
  const gsel = $('#host-group');
  if (gsel && gsel.dataset.sig !== groups.join('|')) {
    gsel.innerHTML = '<option value="all">全部分组</option>'
      + groups.map(g => `<option value="${esc(g)}">${esc(g)}</option>`).join('');
    gsel.dataset.sig = groups.join('|');
    if (!groups.includes(_hostFilter.group)) _hostFilter.group = 'all';
    gsel.value = _hostFilter.group;
  }

  const hosts = all.filter(hostMatches);
  const sel = _hostFilter.sel;

  if (!all.length) {
    $('#hosts-table tbody').innerHTML = `<tr><td colspan="14">${stateBlock(
      'empty', '还没有纳管任何节点',
      '点击右上角「添加节点」，填入对端地址与本机采集令牌即可加入统一视图。',
      state.isAdmin ? '<button class="btn sm" id="btn-host-add-empty">添加节点</button>' : ''
    )}</td></tr>`;
    const addEmpty = $('#btn-host-add-empty');
    if (addEmpty) addEmpty.addEventListener('click', () => $('#btn-host-add').click());
    updateHostSelLabel(); return;
  }
  if (!hosts.length) {
    $('#hosts-table tbody').innerHTML = '<tr><td colspan="14" class="muted">没有符合筛选条件的节点</td></tr>';
    updateHostSelLabel(); return;
  }

  $('#hosts-table tbody').innerHTML = hosts.map(h => {
    const s = h.summary || {};
    const online = h.online;
    const tone = !online ? 'crit' : s.health === 'crit' ? 'crit' : s.health === 'warn' ? 'warn' : 'ok';
    const health = !online ? '离线' : HOST_HEALTH_TEXT[s.health] || s.health || '未知';
    const ctrls = (s.controllers || []).map(c => `C${c.ctrl}`).join(' ') || '—';
    const bad = Number(s.disks_bad_count || 0);
    const deg = Number(s.vds_degraded || 0);
    const ops = [
      online && !h.local ? `<button class="btn sm ghost" data-open="${esc(h.address)}">打开</button>` : '',
      state.isAdmin ? `<button class="btn sm ghost" data-edit="${esc(h.id)}">编辑</button>` : '',
      state.isAdmin && !h.local ? `<button class="btn sm ghost" data-remove="${esc(h.id)}">移除</button>` : '',
    ].filter(Boolean).join(' ');
    return `<tr>
      <td><input type="checkbox" data-pick="${esc(h.id)}" ${sel.has(Number(h.id)) ? 'checked' : ''} /></td>
      <td><span class="host-dot ${tone}" title="${online ? '在线' : esc(h.error || '离线')}"></span></td>
      <td>${esc(h.name)}${h.local ? ' <span class="badge info">本机</span>' : ''}${h.enabled ? '' : ' <span class="badge">已停用</span>'}</td>
      <td class="tiny">${esc(h.group_name || '—')}</td>
      <td class="num">${esc(h.address)}</td>
      <td>${online ? `<span class="badge ${tone === 'ok' ? 'ok' : tone === 'warn' ? 'warn' : 'crit'}">${esc(health)}</span>` : `<span class="tiny" style="color:var(--crit)">${esc(h.error || '离线')}</span>`}</td>
      <td class="num">${esc(ctrls)}</td>
      <td class="num">${online ? esc(s.disks_total || 0) : '—'}${bad ? ` <span class="badge crit">${esc(bad)}</span>` : ''}</td>
      <td class="num">${online ? esc(s.vds_total || 0) : '—'}${deg ? ` <span class="badge warn">${esc(deg)}</span>` : ''}</td>
      <td class="num">${online && s.max_temp != null ? esc(s.max_temp) + '°C' : '—'}</td>
      <td class="num">${online ? esc(s.alerts_open || 0) : '—'}</td>
      <td class="num">${online ? esc(s.tasks_active || 0) : '—'}</td>
      <td class="num">${esc(h.last_seen || '—')}</td>
      <td class="ops">${ops || '<span class="tiny">—</span>'}</td>
    </tr>`;
  }).join('');

  $$('#hosts-table [data-pick]').forEach(cb => cb.addEventListener('change', () => {
    const id = Number(cb.dataset.pick);
    if (cb.checked) sel.add(id); else sel.delete(id);
    updateHostSelLabel();
  }));
  updateHostSelLabel();

  $$('#hosts-table [data-open]').forEach(b =>
    b.addEventListener('click', () => window.open(b.dataset.open, '_blank'))
  );
  $$('#hosts-table [data-edit]').forEach(b =>
    b.addEventListener('click', () => openHostModal(Number(b.dataset.edit)))
  );
  $$('#hosts-table [data-remove]').forEach(b =>
    b.addEventListener('click', () => {
      const id = Number(b.dataset.remove);
      const h = hosts.find(x => Number(x.id) === id) || {};
      confirmModal(
        '移除节点',
        `<p>确认从本中心移除「<strong>${esc(h.name || '')}</strong>」？</p>
         <p class="tiny">仅移除监控登记，不会对被管节点本身做任何改动。</p>`,
        '移除',
        true,
        async () => {
          await api(`/api/hosts/${id}/remove`, { method: 'POST' });
          toast('节点已移除', 'ok');
          await loadHosts();
        }
      );
    })
  );
}

function openHostModal(hostId) {
  const isEdit = !!hostId;
  const h = isEdit ? (_hostsCache.find(x => Number(x.id) === Number(hostId)) || {}) : {};
  showModal({
    title: isEdit ? `编辑节点 ${h.name || ''}` : '添加存储节点',
    body: `<div class="field"><label for="h-name">名称</label>
        <input class="input" id="h-name" value="${esc(h.name || '')}" placeholder="如：机房A-存储01" /></div>
      <div class="field"><label for="h-addr">地址</label>
        <input class="input mono" id="h-addr" value="${esc(h.address || '')}" placeholder="http://192.168.88.30:5200" /></div>
      <div class="field"><label for="h-token">采集令牌</label>
        <input class="input mono" id="h-token" value="" placeholder="${isEdit ? '留空表示不修改' : '被管节点「多机管理 → 本机采集令牌」中的值'}" /></div>
      <div class="field"><label for="h-note">备注（选填）</label>
        <input class="input" id="h-note" value="${esc(h.note || '')}" /></div>
      <p class="tiny">被管节点需与本中心运行同一产品，并在其「多机管理」页面复制采集令牌。</p>`,
    actions: [
      { label: '取消', handler: closeModal },
      {
        label: isEdit ? '保存' : '添加',
        cls: 'primary',
        handler: async btn => {
          btnLoading(btn, true);
          const body = {
            name: $('#h-name').value.trim(),
            address: $('#h-addr').value.trim(),
            note: $('#h-note').value.trim(),
          };
          const tk = $('#h-token').value.trim();
          if (tk) body.token = tk;
          try {
            if (isEdit) await api(`/api/hosts/${hostId}`, { method: 'POST', body });
            else await api('/api/hosts', { method: 'POST', body });
            toast(isEdit ? '节点已保存' : '节点已添加', 'ok');
            closeModal();
            await loadHosts();
          } catch (e) {
            toast(e.message, 'error');
            btnLoading(btn, false);
          }
        },
      },
    ],
  });
}

let _hostsCache = [];

async function loadAgentToken() {
  if (!state.isAdmin) return;
  try {
    const d = await api('/api/agent/token');
    $('#agent-token').textContent = d.token || '—';
    const t = $('#remote-ack-toggle');
    if (t) t.checked = !!d.remote_ack;
  } catch (e) {
    $('#agent-token').textContent = '—';
  }
}

function bindHostsUI() {
  const refresh = $('#btn-hosts-refresh');
  if (refresh) {
    refresh.addEventListener('click', async () => {
      btnLoading(refresh, true);
      try {
        const d = await api('/api/hosts/refresh', { method: 'POST' });
        const off = (d.results || []).filter(r => !r.online);
        toast(off.length ? `采集完成，${off.length} 台离线` : '采集完成，全部在线', off.length ? 'error' : 'ok');
        _hostsCache = d.hosts || [];
        renderHosts({ hosts: _hostsCache, stats: { total: _hostsCache.length, online: _hostsCache.filter(h => h.online).length, offline: _hostsCache.filter(h => !h.online).length }, last: {} });
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        btnLoading(refresh, false);
      }
    });
  }
  const add = $('#btn-host-add');
  if (add) add.addEventListener('click', () => openHostModal(null));
  const copy = $('#btn-token-copy');
  if (copy) {
    copy.addEventListener('click', async () => {
      const t = $('#agent-token').textContent;
      if (!t || t === '—') return;
      try {
        await navigator.clipboard.writeText(t);
        toast('令牌已复制', 'ok');
      } catch (e) {
        toast('复制失败，请手动选择文本', 'error');
      }
    });
  }
  const rot = $('#btn-token-rotate');
  if (rot) {
    rot.addEventListener('click', () => {
      confirmModal(
        '重置采集令牌',
        '<p>重置后，所有已经用旧令牌登记本机的中心都会采集失败，需要重新填入新令牌。</p>',
        '确认重置',
        true,
        async () => {
          const d = await api('/api/agent/token/rotate', { method: 'POST' });
          $('#agent-token').textContent = d.token || '—';
          toast('令牌已重置', 'ok');
        }
      );
    });
  }
}

async function loadLicense() {
  const box = $('#license-body');
  if (!box) return;
  let d;
  try {
    d = await api('/api/license');
  } catch (e) {
    box.innerHTML = stateFromError(e);
    return;
  }
  state.license = d.license || {};
  renderLicense(state.license);
}

function renderLicense(L) {
  const box = $('#license-body');
  if (!box) return;

  const tone = L.licensed ? 'ok' : L.trial ? 'info' : 'warn';
  const badge = L.licensed
    ? '<span class="badge ok">已授权</span>'
    : L.trial
      ? '<span class="badge info">试用中</span>'
      : '<span class="badge warn">社区版</span>';

  const rows = [
    ['版本', esc(L.edition_name || '—')],
    ['授权对象', esc(L.licensee || '—')],
    ['序列号', esc(L.serial || '—')],
    ['可管理设备数', L.seats ? esc(L.seats) : '—'],
    ['签发日期', esc(L.issued_at || '—')],
    ['到期日期', esc(L.expires_at || (L.licensed ? '永久' : '—'))],
    L.trial ? ['试用剩余', `${esc(L.days_left)} 天`] : null,
  ].filter(Boolean);

  // 功能清单：一眼看出当前版本包含什么、缺什么
  const feats = Object.values(L.features || {})
    .map(f => `<span class="feat-pill ${f.enabled ? 'on' : 'off'}" title="${f.enabled ? '已包含' : '需要' + (({ pro: '专业版', enterprise: '企业版' })[f.edition] || f.edition)}">
      ${f.enabled ? '✓' : '✕'} ${esc(f.name)}</span>`)
    .join('');

  box.innerHTML = `
    <div class="license-head">
      ${badge}
      <span class="tiny">${esc(L.edition_name || '')}</span>
      ${L.reason ? `<span class="tiny" style="color:var(--warn)">${esc(L.reason)}</span>` : ''}
    </div>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
    <div class="tiny" style="margin:12px 0 6px">功能清单</div>
    <div class="feat-list">${feats}</div>
    ${L.crypto_available ? '' : '<p class="tiny" style="color:var(--warn);margin-top:10px">运行环境缺少 cryptography 库，无法验证签名授权。</p>'}`;

  if (state.isAdmin) {
    $('#btn-license-install').classList.toggle('hidden', false);
    $('#btn-license-remove').classList.toggle('hidden', !L.licensed);
  }
}

function bindLicenseUI() {
  const install = $('#btn-license-install');
  const file = $('#license-file');
  if (install && file) {
    install.addEventListener('click', () => file.click());
    file.addEventListener('change', async () => {
      const f = file.files && file.files[0];
      if (!f) return;
      const text = await f.text();
      try {
        const r = await api('/api/license', { method: 'POST', body: { license: text } });
        toast('授权已安装', 'ok');
        state.license = r.license || {};
        renderLicense(state.license);
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        file.value = '';
      }
    });
  }
  const rm = $('#btn-license-remove');
  if (rm) {
    rm.addEventListener('click', () => {
      confirmModal(
        '移除授权',
        '<p>移除后将从已授权版本退回社区版，报表、告警确认等增值功能将不可用。存储监控与磁盘管理不受影响。</p>',
        '确认移除',
        true,
        async () => {
          const r = await api('/api/license/remove', { method: 'POST' });
          toast('授权已移除', 'ok');
          state.license = r.license || {};
          renderLicense(state.license);
        }
      );
    });
  }
}

let _reportPeriod = 'day';
let _reportData = null;

const REPORT_LEVEL_TEXT = { ok: '正常', info: '关注', warn: '建议更换', crit: '立即更换' };

async function loadReports() {
  const meta = $('#report-meta');
  if (!meta) return;
  meta.textContent = '生成中…';
  try {
    const d = await api('/api/reports/summary?period=' + encodeURIComponent(_reportPeriod));
    _reportData = d.report;
  } catch (e) {
    meta.textContent = '加载失败：' + e.message;
    return;
  }
  renderReport(_reportData);
  loadReportSchedule();
}

function renderReport(r) {
  if (!r) return;
  const m = r.meta || {};
  const healthText = { ok: '正常', warn: '警告', crit: '严重', unknown: '未知' }[r.health.overall] || r.health.overall;

  $('#report-meta').innerHTML =
    `主机 <strong>${esc(m.host || '')}</strong> · 周期 ${esc(m.title || '')} · 生成于 ${esc(m.generated_at || '')}` +
    ` · 整体健康 ${stateBadge(r.health.overall === 'ok' ? 'Optimal' : r.health.overall === 'crit' ? 'Failed' : 'Degraded')}` +
    ` <span class="tiny">${esc(healthText)}</span>`;

  const cap = r.capacity || {};
  const raid = r.raid || {};
  const disks = r.disks || {};
  const alerts = r.alerts || {};
  const tasks = r.tasks || {};

  const cards = [
    ['已用容量', cap.used || '—', `占总容量 ${cap.use_percent != null ? cap.use_percent + '%' : '—'}`],
    ['总容量', cap.size || '—', '文件系统合计'],
    ['虚拟磁盘', String(raid.total || 0), raid.degraded ? `${raid.degraded} 个非最优` : '全部正常'],
    ['物理磁盘', String(disks.total || 0), disks.avg_temp != null ? `均温 ${disks.avg_temp}°C` : '无温度数据'],
    ['周期内告警', String(alerts.opened || 0), `恢复 ${alerts.resolved || 0} · 未处理 ${alerts.open_now || 0}`],
    ['运维操作', String(tasks.total || 0), tasks.failed ? `${tasks.failed} 次失败` : '无失败'],
  ];
  $('#report-cards').innerHTML = cards
    .map(([k, v, s], i) => `<div class="card" style="--d:${i}">
      <div class="eyebrow">${esc(k)}</div>
      <div class="stat-value">${esc(v)}</div>
      <div class="stat-sub">${esc(s)}</div>
    </div>`)
    .join('');

  const capRows = (cap.mounts || [])
    .map(x => `<tr>
      <td>${esc(x.mountpoint)}</td>
      <td class="num">${esc(x.device || '—')}</td>
      <td>${esc(x.fstype || '—')}</td>
      <td class="num">${esc(x.size)}</td>
      <td class="num">${esc(x.used)}</td>
      <td class="num">${esc(x.avail)}</td>
      <td class="num">${esc(x.use_percent)}%</td>
      <td class="num">${esc(x.growth)}</td>
    </tr>`)
    .join('');
  $('#report-cap-table tbody').innerHTML = capRows || '<tr><td colspan="8" class="muted">周期内无文件系统样本</td></tr>';

  const vdRows = (raid.vds || [])
    .map(v => `<tr>
      <td class="num">C${esc(v.ctrl)}</td>
      <td class="num">${esc(v.dg_vd || '—')}</td>
      <td>${esc(v.type || '—')}</td>
      <td class="num">${esc(v.size || '—')}</td>
      <td>${stateBadge(v.state)}</td>
    </tr>`)
    .join('');
  $('#report-vd-table tbody').innerHTML = vdRows || '<tr><td colspan="5" class="muted">无虚拟磁盘</td></tr>';

  const wearRows = (disks.worn || [])
    .map(d => `<tr>
      <td class="num">${esc(d.label || '—')}</td>
      <td class="num">${esc(d.reallocated)}</td>
      <td class="num">${esc(d.pending)}</td>
      <td class="num">${esc(d.uncorrectable)}</td>
      <td><span class="badge ${d.level === 'crit' ? 'crit' : d.level === 'warn' ? 'warn' : d.level === 'info' ? 'info' : ''}">${esc(REPORT_LEVEL_TEXT[d.level] || d.level)}</span></td>
    </tr>`)
    .join('');
  $('#report-wear-table tbody').innerHTML = wearRows || '<tr><td colspan="5" class="muted">无磁盘数据</td></tr>';

  const growth = disks.smart_growth || [];
  const bc = r.bcache || {};
  $('#report-activity').innerHTML = `
    <div class="report-activity">
      <div>
        <div class="tiny">周期内告警明细（${(alerts.items || []).length}）</div>
        <div class="mini-list">${
          (alerts.items || []).slice(0, 8).map(a =>
            `<div class="mini-row"><span class="badge ${a.level === 'error' ? 'crit' : a.level === 'warning' ? 'warn' : 'info'}">${esc({ error: '错误', warning: '警告', info: '信息' }[a.level] || a.level)}</span>
             <span class="mini-text">${esc(a.title)}</span><span class="tiny mono">${esc(a.first_seen)}</span></div>`
          ).join('') || '<div class="muted">周期内没有新增告警</div>'
        }</div>
      </div>
      <div>
        <div class="tiny">周期内运维操作（${(tasks.items || []).length}）</div>
        <div class="mini-list">${
          (tasks.items || []).slice(0, 8).map(t =>
            `<div class="mini-row"><span class="badge ${t.status === 'failed' ? 'crit' : 'ok'}">${esc({ succeeded: '成功', failed: '失败', running: '执行中', queued: '排队中' }[t.status] || t.status)}</span>
             <span class="mini-text">${esc(t.title)} ${esc(t.target || '')}</span><span class="tiny mono">${esc(t.created_at)}</span></div>`
          ).join('') || '<div class="muted">周期内没有运维操作</div>'
        }</div>
      </div>
      <div>
        <div class="tiny">磁盘健康与缓存</div>
        <div class="mini-list">
          <div class="mini-row"><span class="mini-text">SMART 属性增长的磁盘</span><span class="tiny mono">${growth.length} 块</span></div>
          ${growth.slice(0, 5).map(g => `<div class="mini-row"><span class="mini-text mono">${esc(g.disk)}</span>
            <span class="tiny mono">重映射 +${esc(g.reallocated)} · 待定 +${esc(g.pending)} · 无法纠正 +${esc(g.uncorrectable)}</span></div>`).join('')}
          ${(bc.devices || []).map(d => `<div class="mini-row"><span class="mini-text mono">${esc(d.device)}</span>
            <span class="tiny mono">小时命中率 ${esc(d.latest_hour)}% · 均值 ${esc(d.avg_hour)}%</span></div>`).join('')}
        </div>
      </div>
    </div>`;
}

function reportUrl(extra) {
  return `/api/reports/export?period=${encodeURIComponent(_reportPeriod)}${extra || ''}`;
}

/* ---------- 报表定时投递 ---------- */

function rschState(p, unit) {
  if (!p) return '';
  if (!p.valid_time) return '时间格式无效，这个周期不会触发';
  const bits = [p.window_key === p.last_sent ? `本${unit}已投递` : `本${unit}待投递`];
  if (p.retry_after) bits.push(`上次失败，${String(p.retry_after).slice(11, 16)} 重试`);
  return bits.join(' · ');
}

async function loadReportSchedule() {
  const box = $('#rsch-history');
  if (!box) return;
  let d;
  try {
    d = await api('/api/reports/schedule');
  } catch (e) {
    box.innerHTML = `<div class="muted tiny">加载失败：${esc(e.message)}</div>`;
    return;
  }
  const cfg = d.config || {};
  const ps = d.periods || {};

  $('#rsch-enabled').checked = !!cfg.enabled;
  $('#rsch-to').value = cfg.recipients || '';
  if (cfg.recipients) {
    $('#rsch-to-hint').textContent = '使用这里填写的收件人';
  } else if (d.alert_email_to) {
    $('#rsch-to-hint').textContent = `沿用告警收件人：${d.alert_email_to}`;
  } else {
    $('#rsch-to-hint').textContent = '告警收件人也是空的，投递会直接失败';
  }
  $('#rsch-sendmail').textContent =
    `通过 ${d.sendmail_path || 'sendmail'} 发出，正文含 HTML 报表 + CSV 附件`;

  const day = cfg.daily || {};
  $('#rsch-daily-on').checked = !!day.enabled;
  $('#rsch-daily-time').value = day.time || '08:00';
  $('#rsch-daily-state').textContent = rschState(ps.day, '日');

  const week = cfg.weekly || {};
  $('#rsch-weekly-on').checked = !!week.enabled;
  $('#rsch-weekly-day').value = String(week.weekday || 1);
  $('#rsch-weekly-time').value = week.time || '08:30';
  $('#rsch-weekly-state').textContent = rschState(ps.week, '周');

  const month = cfg.monthly || {};
  const mday = $('#rsch-monthly-day');
  if (!mday.dataset.filled) {
    mday.innerHTML = Array.from({ length: 31 }, (_, i) =>
      `<option value="${i + 1}">${i + 1} 日</option>`).join('');
    mday.dataset.filled = '1';
  }
  $('#rsch-monthly-on').checked = !!month.enabled;
  mday.value = String(month.day || 1);
  $('#rsch-monthly-time').value = month.time || '09:00';
  $('#rsch-monthly-state').textContent = rschState(ps.month, '月');

  const hist = cfg.history || [];
  box.innerHTML = hist.length
    ? '<div class="bay-side-sec-title">最近投递</div>' +
      hist
        .slice(0, 8)
        .map(
          (h) => `<div class="rsch-rec">
        <span class="mono tiny">${esc(h.at || '')}</span>
        <span class="st ${h.ok ? 'ok' : 'fail'}">${h.ok ? '成功' : '失败'}</span>
        <span class="msg" title="${esc(h.message || '')}">${esc(h.label || '')} · ${esc(h.trigger || '')} · ${esc(h.message || '')}</span>
      </div>`
        )
        .join('')
    : '<div class="muted tiny">还没有投递记录。保存设置后可用「立即发送一份」验证收件人是否配通。</div>';
}

async function rschSave() {
  const btn = $('#btn-report-sched-save');
  btnLoading(btn, true);
  try {
    const d = await api('/api/reports/schedule', {
      method: 'POST',
      // 必须传对象：api() 只在 body 不是字符串时才补 Content-Type，
      // 传字符串的话后端 get_json() 拿到 None，接口返回 200 但什么都没保存。
      body: {
        enabled: $('#rsch-enabled').checked,
        recipients: $('#rsch-to').value.trim(),
        daily: { enabled: $('#rsch-daily-on').checked, time: $('#rsch-daily-time').value },
        weekly: {
          enabled: $('#rsch-weekly-on').checked,
          weekday: Number($('#rsch-weekly-day').value),
          time: $('#rsch-weekly-time').value,
        },
        monthly: {
          enabled: $('#rsch-monthly-on').checked,
          day: Number($('#rsch-monthly-day').value),
          time: $('#rsch-monthly-time').value,
        },
      },
    });
    toast(d.ok ? '投递设置已保存' : '保存失败', d.ok ? 'ok' : 'error');
    await loadReportSchedule();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function rschSendNow() {
  const btn = $('#btn-report-send');
  btnLoading(btn, true);
  try {
    const r = await api('/api/reports/send', {
      method: 'POST',
      body: { period: _reportPeriod },
    });
    toast(r.ok ? `已投递：${r.message}` : `投递失败：${r.message}`, r.ok ? 'ok' : 'error');
    await loadReportSchedule();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

/* ---------- 性能中心 ---------- */

/* IO 慢的一键诊断：把结论按可能性排序，每条都带证据。
   这里刻意不展示所有原始读数——用户来问"为什么慢"，需要的是判断，
   不是再看一遍 IOPS。原始数据在下面的实时性能表里。 */
const IODIAG_LEVEL = { crit: '严重', warn: '关注', info: '线索', ok: '正常' };

async function runIoDiagnosis() {
  const btn = $('#btn-iodiag');
  const box = $('#iodiag-findings');
  btnLoading(btn, true);
  $('#iodiag-verdict').className = 'iodiag-verdict';
  $('#iodiag-verdict').innerHTML = '<span class="muted tiny">采样中（约 2 秒）…</span>';
  try {
    const d = await api('/api/io/diagnose');
    if (d.ok === false) throw new Error(d.error || '诊断失败');
    $('#iodiag-at').textContent = d.at || '';
    $('#iodiag-verdict').className = 'iodiag-verdict ' + (d.severity || '');
    $('#iodiag-verdict').innerHTML =
      `<span class="badge ${d.severity === 'crit' ? 'crit' : d.severity === 'warn' ? 'warn' : 'ok'}">`
      + `${d.severity === 'crit' ? '存在瓶颈' : d.severity === 'warn' ? '需要关注' : '未见异常'}</span> `
      + `<strong>${esc(d.summary || '')}</strong>`
      + ((d.excluded || []).length
        ? `<div class="tiny" style="margin-top:6px">已排除 ${d.excluded.length} 块系统盘：`
          + d.excluded.map(x => `${esc(x.name)}（${esc(x.reason)}，${x.iops} IOPS / ${x.latency_ms}ms）`).join('、')
          + ' —— 它属于设备自身操作系统，不计入存储性能判定。</div>'
        : '');

    const t = d.totals || {};
    $('#iodiag-metrics').innerHTML = [
      ['数据面 IOPS', t.iops != null ? String(t.iops) : '—', '已排除系统盘'],
      ['数据面吞吐', fmtRate(t.bytes_per_sec || 0), '读 + 写'],
      ['平均 IO 尺寸', t.avg_io_kb != null ? t.avg_io_kb + ' KB' : '—',
        t.avg_io_kb == null ? '采样窗口内无 IO' : (t.avg_io_kb < 16 ? '偏小，随机为主' : '顺序为主')],
      ['可用内存', d.mem_available_percent != null ? d.mem_available_percent + '%' : '—', '低于 10% 会放大 IO'],
    ].map(([k, v, s]) => `<div class="card stat-card">
        <div class="eyebrow">${esc(k)}</div>
        <div class="stat-value">${esc(v)}</div>
        <div class="stat-sub">${esc(s)}</div>
      </div>`).join('');

    box.innerHTML = (d.findings || []).map((f, i) => `
      <div class="iodiag-finding ${esc(f.level)}">
        <div class="head">
          <span class="badge ${f.level === 'crit' ? 'crit' : f.level === 'warn' ? 'warn' : f.level === 'ok' ? 'ok' : 'info'}">
            ${i === 0 && (f.level === 'crit' || f.level === 'warn') ? '首要' : esc(IODIAG_LEVEL[f.level] || f.level)}
          </span>
          <strong>${esc(f.title)}</strong>
        </div>
        <div class="why">${esc(f.detail)}</div>
        ${f.advice ? `<div class="do">建议：${esc(f.advice)}</div>` : ''}
      </div>`).join('');

    const procs = d.processes || [];
    $('#iodiag-procs').innerHTML = procs.length
      ? `<div class="bay-side-sec-title" style="margin-top:16px">IO 占用最高的进程</div>
         <div class="table-wrap">
           <table class="data">
             <thead><tr><th>PID</th><th>进程</th><th>命令行</th><th class="num">读</th><th class="num">写</th><th class="num">合计</th></tr></thead>
             <tbody>${procs.map(p => `<tr>
               <td class="mono">${p.pid}</td>
               <td class="mono">${esc(p.name)}</td>
               <td class="tiny">${esc((p.cmdline || '').slice(0, 110) || '—')}</td>
               <td class="num mono">${fmtRate(p.read_bps)}</td>
               <td class="num mono">${fmtRate(p.write_bps)}</td>
               <td class="num mono">${fmtRate(p.total_bps)}</td>
             </tr>`).join('')}</tbody>
           </table>
         </div>`
      : '<div class="muted tiny" style="margin-top:14px">采样窗口内没有进程产生块设备 IO。</div>';
  } catch (e) {
    $('#iodiag-verdict').className = 'iodiag-verdict warn';
    $('#iodiag-verdict').textContent = '诊断失败：' + e.message;
    box.innerHTML = '';
  } finally {
    btnLoading(btn, false);
  }
}

const PERF_ROLE = {
  vd: '阵列虚拟盘', bcache: 'bcache 缓存卷', nvme: 'NVMe',
  md: '软 RAID', system: '系统盘', disk: '数据盘',
};

/* ---------- 基准测试（唯一会写盘的功能） ---------- */
let _benchTimer = null;

function renderBench(d) {
  const modeSel = $('#bench-mode');
  if (modeSel && !modeSel.dataset.filled && (d.modes || []).length) {
    modeSel.innerHTML = d.modes.map(m => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join('');
    modeSel.value = 'rand-rw';
    modeSel.dataset.filled = '1';
  }
  const sel = $('#bench-target');
  if (sel && !sel.dataset.filled) {
    const ts = d.targets || [];
    sel.innerHTML = ts.length
      ? ts.map(t => `<option value="${esc(t.mountpoint)}">${esc(t.mountpoint)} · ${esc(t.device)} · 空闲 ${t.free_mb} MB</option>`).join('')
      : '<option value="">没有符合条件的测试目录</option>';
    sel.dataset.filled = '1';
  }
  const running = !!d.running;
  $('#btn-bench-start').disabled = running || !d.available || !(d.targets || []).length;
  $('#btn-bench-cancel').disabled = !running;
  $('#btn-bench-start').textContent = running ? '测试中…' : '开始测试';
  $('#bench-state').textContent = d.available
    ? (running ? (d.phase || '') : (d.finished_at ? `上次完成 ${d.finished_at}` : 'fio 就绪'))
    : '未安装 fio';

  // 实时值：运行中取最新采样点，结束后取 fio 的汇总
  const live = d.live || [];
  const last = live.length ? live[live.length - 1] : null;
  const sum = d.summary || null;
  const show = running && last ? last : sum;
  $('#bk-iops').textContent = show && show.iops != null ? Math.round(show.iops).toLocaleString() : '—';
  $('#bk-bw').textContent = show && show.bw_bytes != null ? (show.bw_bytes / 1048576).toFixed(1)
    : (show && show.mbps != null ? show.mbps.toFixed(1) : '—');
  const lat = show ? (show.lat_ms != null ? show.lat_ms : show.latency_ms) : null;
  $('#bk-lat').textContent = lat != null ? Number(lat).toFixed(3) : '—';
  $('#bk-p99').textContent = sum && sum.p99_ms != null ? sum.p99_ms : '—';
  $('#bk-pct').textContent = running ? (d.progress || 0) + '%' : (sum ? '100%' : '—');
  $('#bk-elapsed').textContent = running
    ? `已运行 ${d.elapsed || 0}/${d.total || 0} 秒`
    : (d.finished_at ? `完成于 ${d.finished_at.slice(11)}` : '');
  $('#bench-bar-fill').style.width = (running ? (d.progress || 0) : (sum ? 100 : 0)) + '%';

  $('#bench-summary').innerHTML = sum && sum.ok
    ? [['IOPS（平均）', `${Math.round(sum.iops).toLocaleString()} ops/s`],
       ['吞吐量（平均）', `${(sum.bw_bytes / 1048576).toFixed(1)} MB/s`],
       ['平均延迟', `${sum.lat_ms} ms`],
       ['P95 / P99', `${sum.p95_ms} / ${sum.p99_ms} ms`],
       ['总数据量', `${(sum.total_bytes / 1073741824).toFixed(2)} GB`],
       ['读写比例', `${sum.read_ratio}% 读 / ${(100 - sum.read_ratio).toFixed(1)}% 写`]]
       .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')
    : `<dt>状态</dt><dd>${esc(d.error || (d.cancelled ? '已取消' : '尚未测试'))}</dd>`;

  const logs = d.log || [];
  $('#bench-log').innerHTML = logs.length
    ? logs.map(l => `<div class="row"><span class="t">${esc(l.at)}</span><span>${esc(l.msg)}</span></div>`).join('')
    : '<span class="muted tiny">尚未测试</span>';
}

async function loadIoBench() {
  try {
    renderBench(await api('/api/io/bench'));
  } catch (e) {
    $('#bench-state').textContent = '加载失败';
  }
}

async function startIoBench() {
  if (!$('#bench-ack').checked) {
    toast('请先勾选确认：允许在所选目录生成并删除临时文件', 'error');
    return;
  }
  const btn = $('#btn-bench-start');
  btnLoading(btn, true);
  try {
    const d = await api('/api/io/bench', {
      method: 'POST',
      body: {
        target: $('#bench-target').value,
        size_mb: Number($('#bench-size').value),
        mode: $('#bench-mode').value,
        threads: Number($('#bench-threads').value),
        iodepth: Number($('#bench-qd').value),
        bs: $('#bench-bs').value,
        runtime: Number($('#bench-runtime').value),
        ramp: Number($('#bench-ramp').value),
        read_mix: Number($('#bench-mix').value),
        drop_cache: $('#bench-nocache').checked,
        direct: true,
      },
    });
    if (d.ok) toast('基准测试已开始，期间带宽会被占满', 'ok');
    await loadIoBench();
    startBenchPoll();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

function startBenchPoll() {
  if (_benchTimer) clearInterval(_benchTimer);
  _benchTimer = setInterval(async () => {
    if (state.view !== 'perf') return;
    try {
      const d = await api('/api/io/bench');
      renderBench(d);
      if (!d.running && _benchTimer) { clearInterval(_benchTimer); _benchTimer = null; }
    } catch (e) { /* 忽略单次轮询失败 */ }
  }, 1200);
}
const PERF_METRIC = {
  iops: { idx: 1, unit: 'IOPS', label: 'IOPS' },
  mbps: { idx: -1, unit: 'MB/s', label: '读+写带宽' },
  latency: { idx: 4, unit: 'ms', label: '平均延迟' },
  util: { idx: 5, unit: '%', label: '利用率' },
};

let _perfChart = null;
let _perfTimer = null;
let _perfDevices = [];
let _perfMeta = {};

function perfLatClass(ms) {
  if (ms >= 200) return 'crit';
  if (ms >= 50) return 'warn';
  return 'ok';
}

function perfMountText(r) {
  const m = (r.mounts || []).join(', ');
  if (m) return m;
  const s = (r.serves || []).filter(x => (x.mounts || []).length);
  if (s.length) return `${s[0].holder} → ${s[0].mounts.join(', ')}`;
  return '';
}

async function loadPerfLive() {
  const tb = $('#perf-table tbody');
  if (!tb) return;
  let d;
  try {
    d = await api('/api/perf/live');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="9">${stateFromError(e)}</td></tr>`;
    return;
  }
  if (d.ok === false) {
    tb.innerHTML = `<tr><td colspan="9" class="muted">采集失败：${esc(d.error || '未知错误')}</td></tr>`;
    return;
  }
  const t = d.total || {};
  _perfMeta = {};
  (d.rows || []).forEach(r => { _perfMeta[r.name] = r; });

  $('#perf-at').textContent = `${d.at || ''} · ${d.interval || 0}s 采样`;
  $('#perf-cards').innerHTML = [
    ['总 IOPS', (t.iops || 0).toFixed(0), '全部块设备合计'],
    ['读带宽', fmtRate(t.read_bps || 0), '采样窗口内的平均速率'],
    ['写带宽', fmtRate(t.write_bps || 0), '采样窗口内的平均速率'],
    ['最忙设备', d.busiest || '—', d.busiest ? `利用率 ${d.busiest_util}%` : '当前无 IO'],
  ].map(([k, v, s]) => `<div class="card stat-card">
      <div class="eyebrow">${esc(k)}</div>
      <div class="stat-value">${esc(v)}</div>
      <div class="stat-sub">${esc(s)}</div>
    </div>`).join('');

  tb.innerHTML = (d.rows || []).map(r => {
    const lat = r.latency_ms || 0;
    const util = r.util_percent || 0;
    const mount = perfMountText(r);
    return `<tr>
      <td class="mono">${esc(r.name)}</td>
      <td><span class="badge ${r.role === 'vd' || r.role === 'bcache' ? 'info' : ''}">${esc(PERF_ROLE[r.role] || r.role)}</span></td>
      <td>${esc(r.model || '—')}${mount ? `<div class="tiny mono">${esc(mount)}</div>` : ''}</td>
      <td class="num mono">${(r.iops || 0).toFixed(1)}</td>
      <td class="num mono">${fmtRate(r.read_bps || 0)}</td>
      <td class="num mono">${fmtRate(r.write_bps || 0)}</td>
      <td class="num mono"><span class="cap-days ${perfLatClass(lat)}">${lat.toFixed(2)} ms</span></td>
      <td class="num mono">${util.toFixed(1)}%</td>
      <td class="num mono">${(r.queue_depth || 0).toFixed(2)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="9">' + stateBlock('empty', '没有可展示的块设备', '', '', true) + '</td></tr>';
}

function perfDevicesFromHistory(devices) {
  if (_perfDevices.length) return;
  // 默认只看前三个设备：全画上去图例就成了一堵墙，看不出任何趋势
  const preferred = devices.filter(n => (_perfMeta[n] || {}).role === 'vd' || (_perfMeta[n] || {}).role === 'bcache');
  _perfDevices = (preferred.length ? preferred : devices).slice(0, 3);
}

async function loadPerfTrend() {
  const empty = $('#perf-chart-empty');
  const canvas = $('#perf-chart');
  if (!canvas) return;
  let d;
  try {
    d = await api('/api/perf/history?hours=' + state.perfHours);
  } catch (e) {
    empty.textContent = '加载失败：' + e.message;
    empty.classList.remove('hidden');
    return;
  }
  const devices = d.devices || [];
  perfDevicesFromHistory(devices);
  const chips = $('#perf-dev-chips');
  chips.innerHTML = devices.map((n, i) => {
    const on = _perfDevices.includes(n);
    return `<button class="perf-dev-chip ${on ? 'on' : ''}" data-dev="${esc(n)}">
      <span class="sw" style="color:${chartPalette()[i % 6]}"></span>${esc(n)}
      <span class="sub">${esc(PERF_ROLE[(_perfMeta[n] || {}).role] || '')}</span>
    </button>`;
  }).join('');

  const metric = PERF_METRIC[state.perfMetric] || PERF_METRIC.iops;
  const datasets = [];
  (d.series || []).forEach((s, i) => {
    if (!_perfDevices.includes(s.name)) return;
    const color = chartPalette()[i % 6];
    datasets.push({
      label: s.name,
      data: (s.points || []).map(p => ({
        x: p[0],
        y: metric.idx === -1 ? Number(((p[2] || 0) + (p[3] || 0)).toFixed(2)) : p[metric.idx],
      })),
      borderColor: color,
      backgroundColor: color,
    });
  });

  if (_perfChart) { _perfChart.destroy(); _perfChart = null; }
  if (!datasets.length || typeof Chart === 'undefined') {
    empty.textContent = typeof Chart === 'undefined' ? '图表组件加载失败' : '暂无数据（历史样本按采集间隔累积，刚部署时需要等几轮）';
    empty.classList.remove('hidden');
    canvas.style.visibility = 'hidden';
    return;
  }
  empty.classList.add('hidden');
  canvas.style.visibility = 'visible';

  const css = getComputedStyle(document.body);
  const tick = css.getPropertyValue('--muted-foreground').trim() || '#7f8b9a';
  const grid = css.getPropertyValue('--border').trim() || '#e2e7ee';
  const tt = css.getPropertyValue('--popover').trim() || '#fff';
  const tti = css.getPropertyValue('--popover-foreground').trim() || '#26303d';
  const fmt = state.perfHours > 24 ? fmtDayClock : fmtClock;
  datasets.forEach(ds => { ds.borderWidth = 1.8; ds.pointRadius = 0; ds.pointHitRadius = 8; ds.tension = 0.34; });

  _perfChart = new Chart(canvas, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 900, easing: 'easeOutQuart' },
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: {
          labels: {
            color: tick, boxWidth: 12, boxHeight: 2, usePointStyle: true, pointStyle: 'line', padding: 16,
            font: { family: "'DM Sans', 'PingFang SC', sans-serif", size: 11, weight: '500' },
          },
        },
        tooltip: {
          backgroundColor: tt, titleColor: tti, bodyColor: tti, borderColor: grid, borderWidth: 1,
          padding: 11, cornerRadius: 10,
          titleFont: { family: "'JetBrains Mono', monospace", size: 11, weight: '600' },
          bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
          callbacks: {
            title: (items) => items.length ? fmtDayClock(items[0].parsed.x) : '',
            label: (item) => `${item.dataset.label}: ${item.parsed.y} ${metric.unit}`,
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          ticks: { color: tick, maxTicksLimit: 8, callback: (v) => fmt(v) },
          grid: { color: grid, drawTicks: false },
          border: { display: false },
        },
        y: {
          beginAtZero: true,
          ticks: { color: tick, maxTicksLimit: 6 },
          grid: { color: grid, drawTicks: false },
          border: { display: false },
          title: { display: true, text: metric.label, color: tick, font: { size: 11 } },
        },
      },
    },
  });
}

function startPerfAuto() {
  if (_perfTimer) clearInterval(_perfTimer);
  _perfTimer = setInterval(() => {
    if (state.view !== 'perf') return;
    if (!$('#perf-auto') || !$('#perf-auto').checked) return;
    loadPerfLive();
  }, 5000);
}

/* ---------- 服务器目录选择器 ---------- */
async function pickServerDir(inputId) {
  const input = $('#' + inputId);
  if (!input) return;
  let cur = input.value || '/mnt';
  const mask = document.createElement('div');
  mask.className = 'dirpick-mask';
  mask.innerHTML = `<div class="dirpick">
    <div class="dirpick-head">
      <button class="btn sm ghost" data-up type="button">上一级</button>
      <span class="dirpick-path"></span>
      <button class="btn sm ghost" data-close type="button">关闭</button>
    </div>
    <div class="dirpick-list"></div>
    <div class="dirpick-foot">
      <span class="tiny" style="margin-right:auto;color:var(--ink-faint)">只列当前层目录，不递归扫描</span>
      <button class="btn sm primary" data-choose type="button">选择此目录</button>
    </div>
  </div>`;
  document.body.appendChild(mask);

  const listEl = mask.querySelector('.dirpick-list');
  const pathEl = mask.querySelector('.dirpick-path');
  let parentPath = '/';

  async function go(path) {
    listEl.innerHTML = '<div class="state loading compact"><div class="t">读取中…</div></div>';
    try {
      const d = await api('/api/fs/browse?path=' + encodeURIComponent(path));
      cur = d.path;
      parentPath = d.parent || '/';
      pathEl.textContent = cur;
      input.dataset.cur = cur;
      listEl.innerHTML = (d.dirs || []).length
        ? d.dirs.map(x => `<button class="dirpick-row" type="button" data-dir="${esc(x.path)}">
            <span class="nm">${esc(x.name)}</span>
            ${x.is_mount ? '<span class="tag">挂载点</span>' : ''}
            ${x.writable ? '' : '<span class="tag">只读</span>'}
          </button>`).join('')
        : '<div class="state empty compact"><div class="t">没有子目录</div></div>';
      listEl.querySelectorAll('[data-dir]').forEach(b =>
        b.addEventListener('click', () => go(b.dataset.dir)));
    } catch (e) {
      listEl.innerHTML = `<div class="state error compact"><div class="t">读取失败</div><div class="d">${esc(e.message)}</div></div>`;
    }
  }

  mask.querySelector('[data-up]').addEventListener('click', () => go(parentPath));
  mask.querySelector('[data-close]').addEventListener('click', () => mask.remove());
  mask.querySelector('[data-choose]').addEventListener('click', () => {
    input.value = cur;
    mask.remove();
  });
  mask.addEventListener('click', (ev) => { if (ev.target === mask) mask.remove(); });
  document.addEventListener('keydown', function esc(ev) {
    if (ev.key === 'Escape') { mask.remove(); document.removeEventListener('keydown', esc); }
  });
  await go(cur);
}

/* ---------- 数据备份与恢复 ---------- */

async function loadFsPaths() {
  const dl = $('#db-paths');
  if (!dl || dl.dataset.filled) return;
  try {
    const d = await api('/api/fs/paths');
    dl.innerHTML = (d.paths || []).map(p =>
      `<option value="${esc(p.path)}">${esc(p.kind === 'mount' ? (p.fstype || '挂载点') : '目录')}${p.writable ? '' : ' · 只读'}</option>`
    ).join('');
    dl.dataset.filled = '1';
  } catch (e) { /* 路径候选失败不影响手输 */ }
}

async function loadDataBackup() {
  try {
    const d = await api('/api/data_backup');
    const el = $('#dbackup-state');
    if (el) el.textContent = d.running ? (d.phase || '运行中') : (d.finished ? `上次 ${d.finished}` : '就绪');
  } catch (e) { /* 状态读取失败不阻塞页面 */ }
}

async function startDataBackup() {
  const body = {
    kind: $('#db-kind').value,
    name: $('#db-name').value.trim() || 'backup',
    source: $('#db-source').value.trim(),
    target: $('#db-target').value.trim(),
    keep: Number($('#db-keep').value) || 7,
  };
  if (!body.source || !body.target) { toast('请填写源目录与目标', 'error'); return; }
  const btn = $('#btn-dbackup-start');
  btnLoading(btn, true);
  try {
    const r = await api('/api/data_backup', { method: 'POST', body });
    if (r.ok) toast('备份已开始，完成后可在「加载归档」里看到', 'ok');
    await loadDataBackup();
  } catch (e) { toast(e.message, 'error'); }
  finally { btnLoading(btn, false); }
}

async function loadBackupSchedule() {
  try {
    const d = await api('/api/data_backup/schedule');
    const s = (d.schedules || [])[0] || {};
    $('#db-sched-on').checked = !!s.enabled;
    $('#db-sched-freq').value = s.freq || 'daily';
    $('#db-sched-time').value = s.time || '02:00';
    $('#db-sched-state').textContent = s.id ? `已配置（${s.freq || 'daily'} ${s.time || ''}）` : '未配置';
  } catch (e) { /* 忽略 */ }
}

async function saveBackupSchedule() {
  const schedule = {
    id: 'bkp-1',
    enabled: $('#db-sched-on').checked,
    freq: $('#db-sched-freq').value,
    time: $('#db-sched-time').value,
    name: $('#db-name').value.trim() || 'backup',
    job: {
      kind: $('#db-kind').value,
      name: $('#db-name').value.trim() || 'backup',
      source: $('#db-source').value.trim(),
      target: $('#db-target').value.trim(),
      keep: Number($('#db-keep').value) || 7,
    },
  };
  if (!schedule.job.source || !schedule.job.target) { toast('请先填写源目录与目标', 'error'); return; }
  try {
    await api('/api/data_backup/schedule', { method: 'POST', body: { schedule } });
    toast('定时备份已保存', 'ok');
    await loadBackupSchedule();
  } catch (e) { toast(e.message, 'error'); }
}

async function loadBackupArchives() {
  const tb = $('#db-archives tbody');
  const dest = $('#db-target').value.trim();
  if (!dest) { toast('请先填写目标目录', 'error'); return; }
  tb.innerHTML = '<tr><td colspan="6" class="muted">读取中…</td></tr>';
  try {
    const d = await api('/api/data_backup/archives?dest=' + encodeURIComponent(dest));
    const list = d.archives || [];
    tb.innerHTML = list.length ? list.map(a => `<tr>
      <td class="mono tiny">${esc(a.archive.split('/').pop())}</td>
      <td>${esc(a.level)}</td>
      <td class="tiny">${esc(a.created_at)}</td>
      <td class="num mono">${fmtBytes(a.bytes)}</td>
      <td class="num">${a.verified ? '<span class="badge ok">已签名</span>' : '<span class="badge warn">无清单</span>'}</td>
      <td><button class="btn sm ghost" data-db-dry="${esc(a.archive)}">演练</button>
          <button class="btn sm ghost" data-db-restore="${esc(a.archive)}">恢复</button></td>
    </tr>`).join('') : `<tr><td colspan="6">${stateBlock('empty', '这个目录还没有备份', '先填好源目录，点「开始备份」。', '', true)}</td></tr>`;
    $$('#db-archives [data-db-dry]').forEach(b => b.addEventListener('click', () => restoreArchive(b.dataset.dbDry, true)));
    $$('#db-archives [data-db-restore]').forEach(b => b.addEventListener('click', () => restoreArchive(b.dataset.dbRestore, false)));
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="6">${stateFromError(e)}</td></tr>`;
  }
}

async function restoreArchive(archive, dry) {
  const target = $('#db-target').value.trim() + '/restore-' + Date.now().toString().slice(-6);
  try {
    const r = await api('/api/data_backup/restore_chain', {
      method: 'POST',
      body: { archive, target, dry_run: dry },
    });
    if (dry) {
      toast(`演练通过：将写入 ${r.entries} 个条目到 ${target}`, 'ok');
    } else {
      toast(`已按 ${r.steps} 份归档恢复到 ${r.target}`, 'ok');
    }
  } catch (e) { toast(e.message, 'error'); }
}

/* ---------- 容量预测 ---------- */

function capDaysCell(days, label) {
  if (days === null || days === undefined) return `<span class="cap-days flat">—</span>`;
  if (days === 0) return `<span class="cap-days crit">已达${label}</span>`;
  const cls = days <= 7 ? 'crit' : days <= 30 ? 'warn' : 'ok';
  return `<span class="cap-days ${cls}">${days} 天</span>`;
}

async function loadCapacityForecast() {
  const tb = $('#cap-table tbody');
  if (!tb) return;
  tb.innerHTML = '<tr><td colspan="9" class="muted">分析中…</td></tr>';
  let d;
  try {
    d = await api('/api/capacity/forecast');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="9">${stateFromError(e)}</td></tr>`;
    return;
  }
  const items = d.items || [];
  $('#cap-note').textContent = `拟合窗口 ${d.window_days || 14} 天 · ${d.generated_at || ''}`;

  const risk = d.risk || [];
  $('#cap-risk').innerHTML = risk.map(r => {
    const crit = (r.days_to_90 || 999) <= 7;
    return `<div class="cap-risk-item ${crit ? 'crit' : ''}">
      <span class="badge ${crit ? 'crit' : 'warn'}">${crit ? '紧迫' : '关注'}</span>
      <strong>${esc(r.mountpoint)}</strong>
      <span>预计 <strong>${r.days_to_90}</strong> 天后达到 90%（${esc(r.eta_90)}），
        约 ${r.days_to_full} 天后写满（${esc(r.eta_full)}）</span>
    </div>`;
  }).join('');
  if (risk.length) $('#cap-risk').style.marginBottom = '14px';

  tb.innerHTML = items.map(i => {
    const r2 = i.r2;
    const conf = r2 === null || r2 === undefined ? '<span class="muted">—</span>'
      : `<span class="mono">${r2.toFixed(2)}</span>${r2 < 0.5 ? '<div class="tiny">波动大</div>' : ''}`;
    const grow = i.growth_per_day === null || i.growth_per_day === undefined
      ? '<span class="muted">—</span>'
      : (i.growth_per_day === 0 ? '<span class="muted">基本不增长</span>' : fmtBytes(i.growth_per_day) + '/天');
    return `<tr>
      <td>
        <strong>${esc(i.mountpoint)}</strong>
        <div class="tiny mono">${esc(i.device || '')}</div>
        ${i.note ? `<div class="tiny">${esc(i.note)}</div>` : ''}
      </td>
      <td>${esc(i.fstype || '')}</td>
      <td class="num">${fmtBytes(i.size)}</td>
      <td class="num">${fmtBytes(i.used)}</td>
      <td class="num mono">${i.use_percent}%</td>
      <td class="num">${grow}</td>
      <td>${capDaysCell(i.days_to_90, '90%')}${i.eta_90 ? `<div class="tiny mono">${esc(i.eta_90)}</div>` : ''}</td>
      <td>${capDaysCell(i.days_to_full, '满')}${i.eta_full ? `<div class="tiny mono">${esc(i.eta_full)}</div>` : ''}</td>
      <td class="num">${conf}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="9">' + stateBlock('empty', '暂无可分析的文件系统', '容量预测需要至少 6 小时的历史样本。', '', true) + '</td></tr>';
}

/* ---------- 硬件环境 ---------- */

/* ---------- 机箱背板（SES）---------- */

async function loadEnclosure() {
  const box = $('#ses-slots');
  if (!box) return;
  let d;
  try {
    d = await api('/api/enclosure');
  } catch (e) {
    box.innerHTML = `<div class="muted tiny">加载失败：${esc(e.message)}</div>`;
    return;
  }
  if (!d.available) {
    $('#ses-note').textContent = d.error || '未发现 SES 背板';
    $('#ses-cards').innerHTML = '';
    box.innerHTML = '<div class="muted tiny">这台机器没有可读的 SES 机箱背板——直连背板或未启用 SES 时属正常情况。</div>';
    $('#ses-issues').innerHTML = '';
    return;
  }
  const t = d.totals || {};
  const enc = (d.enclosures || [])[0] || {};
  $('#ses-note').textContent = `${enc.vendor || ''} ${enc.model || ''} · ${enc.device || ''} · WWID ${enc.wwid || '—'}`;
  $('#ses-cards').innerHTML = [
    ['背板数量', String(t.enclosures || 0), 'SES 设备'],
    ['槽位总数', String(t.slots || 0), enc.slots_unsupported ? `另有 ${enc.slots_unsupported} 个未实现槽位` : '按背板实际能力'],
    ['已装磁盘', String(t.installed || 0), `空槽 ${Math.max(0, (t.slots || 0) - (t.installed || 0))} 个`],
    ['故障 / 定位灯', `${t.faults || 0} / ${t.locating || 0}`, t.faults ? '背板已标记故障' : '无故障标记'],
  ].map(([k, v, s]) => `<div class="card stat-card">
      <div class="eyebrow">${esc(k)}</div>
      <div class="stat-value">${esc(v)}</div>
      <div class="stat-sub">${esc(s)}</div>
    </div>`).join('');

  const issues = d.issues || [];
  $('#ses-issues').innerHTML = issues.map(i => `
    <div class="cache-finding warn" style="margin-top:12px">
      <div class="cf-head">
        <span class="badge warn">不一致</span>
        <strong>${esc(i.title)}</strong>
      </div>
      <div class="cf-detail">${esc(i.detail)}</div>
      ${i.advice ? `<div class="cf-advice">建议：${esc(i.advice)}</div>` : ''}
    </div>`).join('');

  box.innerHTML = (enc.slots || []).map(s => {
    const cls = [
      'ses-slot',
      s.installed ? 'installed' : 'empty',
      s.fault ? 'fault' : '',
      s.locate ? 'locate' : '',
    ].filter(Boolean).join(' ');
    const stText = s.installed ? '已装盘' : (s.status || '空槽');
    return `<div class="${cls}" title="槽位 ${s.slot} · ${esc(s.status)} · 供电 ${esc(s.power_status || '—')}">
      <span class="n">#${s.slot}</span>
      <span class="st">${esc(stText)}</span>
      <span class="lamp">
        <i class="${s.installed ? 'on-ok' : ''}"></i>
        <i class="${s.fault ? 'on-crit' : ''}"></i>
        <i class="${s.locate ? 'on-primary' : ''}"></i>
      </span>
    </div>`;
  }).join('') || '<div class="muted tiny">背板未报告任何磁盘槽位</div>';
}

/* ---------- 维护计划 ---------- */

let _maintView = null;

function maintFreqText(p) {
  const wd = ['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  if (p.freq === 'interval') return `每 ${p.interval_hours || '?'} 小时`;
  if (p.freq === 'daily') return '每天';
  if (p.freq === 'weekly') return `每${wd[p.weekday || 1]}`;
  if (p.freq === 'monthly') return `每月 ${p.day || 1} 日`;
  return p.freq;
}

async function loadMaintenance() {
  const tb = $('#maint-table tbody');
  if (!tb) return;
  let d;
  try {
    d = await api('/api/maintenance');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="7">${stateFromError(e)}</td></tr>`;
    return;
  }
  _maintView = d;
  $('#maint-enabled').checked = !!(d.config || {}).enabled;

  const sel = $('#maint-new-action');
  if (sel && !sel.dataset.filled) {
    sel.innerHTML = (d.actions || []).map(a => `<option value="${esc(a.key)}">${esc(a.label)}</option>`).join('');
    sel.dataset.filled = '1';
  }

  const plans = d.plans || [];
  const lastRun = (d.config || {}).last_run || {};
  tb.innerHTML = plans.map(p => {
    const ran = (d.config || {}).history || [];
    const last = ran.find(h => String(h.plan_id) === String(p.id));
    const done = p.last_run ? '本周期已执行' : '本周期待执行';
    return `<tr>
      <td><strong>${esc(p.name)}</strong></td>
      <td>${esc((d.actions.find(a => a.key === p.action) || {}).label || p.action)}</td>
      <td>${esc(maintFreqText(p))}</td>
      <td class="mono">${esc(p.freq === 'interval' ? '—' : p.time || '')}</td>
      <td class="tiny">${last ? esc(last.at) : '<span class="muted">—</span>'}</td>
      <td class="num tiny">${esc(done)}</td>
      <td><span class="maint-row-actions">
        <button class="btn sm" data-maint-run="${p.id}">立即执行</button>
        <button class="btn sm ghost" data-maint-del="${p.id}">删除</button>
      </span></td>
    </tr>`;
  }).join('') || '<tr><td colspan="7" class="muted">还没有维护计划。建议至少配一条「每周巡读」。</td></tr>';

  const hist = (d.config || {}).history || [];
  $('#maint-history').innerHTML = hist.length
    ? '<div class="bay-side-sec-title">最近执行</div>' +
      hist.slice(0, 6).map(h => `<div class="rsch-rec">
        <span class="mono tiny">${esc(h.at || '')}</span>
        <span class="st ${h.ok ? 'ok' : 'fail'}">${h.ok ? '已下发' : '失败'}</span>
        <span class="msg" title="${esc(h.message || '')}">${esc(h.name || '')} · ${esc(h.trigger || '')} · ${esc(h.message || '')}</span>
      </div>`).join('')
    : '';
}

async function maintSave(plans) {
  try {
    const d = await api('/api/maintenance', {
      method: 'POST',
      body: { enabled: $('#maint-enabled').checked, plans },
    });
    if (d.ok) toast('维护计划已保存', 'ok');
    await loadMaintenance();
    return d.ok;
  } catch (e) {
    toast(e.message, 'error');
    return false;
  }
}

function maintCurrentPlans() {
  const raw = (_maintView && _maintView.plans) || [];
  // 只回传后端需要的字段：把 window_key / last_run 这类派生值写回去，
  // 下次读取时会被当成计划的一部分，字段只会越滚越多。
  return raw.map(p => ({
    id: p.id, name: p.name, action: p.action, freq: p.freq,
    enabled: p.enabled, time: p.time, weekday: p.weekday,
    day: p.day, interval_hours: p.interval_hours, params: p.params || {},
  }));
}

async function maintAdd() {
  const action = $('#maint-new-action').value;
  const freq = $('#maint-new-freq').value;
  const name = $('#maint-new-name').value.trim() || ($('#maint-new-action').selectedOptions[0] || {}).text || '维护计划';
  const plan = {
    id: Date.now() % 100000,
    name,
    action,
    freq,
    enabled: true,
    time: $('#maint-new-time').value || '02:00',
    weekday: Number($('#maint-new-weekday').value) || 1,
    day: 1,
    interval_hours: 24,
    params: { ctrl: 0 },
  };
  const plans = maintCurrentPlans();
  plans.push(plan);
  if (await maintSave(plans)) $('#maint-new-name').value = '';
}

/* ---------- 硬件环境 ---------- */

/* ---------- 控制器与虚拟盘策略 ---------- */
let _policyData = null;

const POLICY_ONOFF_LABELS = {
  on: '开启', off: '关闭',
};

async function loadCtrlPolicy() {
  const box = $('#policy-body');
  if (!box) return;
  let d;
  try {
    d = await api('/api/ctrl_policy?ctrl=0');
  } catch (e) {
    box.innerHTML = stateFromError(e);
    return;
  }
  _policyData = d;
  const v = d.values || {};
  const cat = d.catalog || { ctrl: [], vd: [] };
  const rates = cat.ctrl.filter(c => c.type === 'int');
  const switches = cat.ctrl.filter(c => c.type === 'onoff');

  $('#policy-hint').textContent = `C${d.ctrl} · 读取于 ${new Date().toLocaleTimeString()}`;

  box.innerHTML = `
    <div class="bench-form" style="grid-template-columns:repeat(3,minmax(0,1fr))">
      ${rates.map(c => `<div class="field">
        <label for="pol-${esc(c.key)}">${esc(c.label)}</label>
        <div style="display:flex;gap:8px">
          <input class="input" id="pol-${esc(c.key)}" type="number" min="${c.values[0]}" max="${c.values[1]}" value="${esc(v[c.key] || '')}" />
          <button class="btn sm" data-policy="${esc(c.key)}">应用</button>
        </div></div>`).join('')}
    </div>
    <div class="policy-grid" style="margin-top:14px">
      ${switches.map(c => `<label class="policy-item" title="${esc(c.risk || '')}">
        <input type="checkbox" data-toggle="${esc(c.key)}" ${v[c.key] === 'on' ? 'checked' : ''} />
        ${esc(c.label)}${c.risk ? ' <span class="tiny" style="color:var(--warn)">⚠</span>' : ''}
      </label>`).join('')}
    </div>
    <div class="bay-side-sec-title" style="margin-top:16px">虚拟盘策略</div>
    <div class="bench-form" style="grid-template-columns:repeat(5,minmax(0,1fr))">
      <div class="field"><label for="vd-pick">虚拟盘</label>
        <select class="select" id="vd-pick">
          ${(d.vds || []).map(x => `<option value="${esc(x.vd)}">${esc(x.vd)} · ${esc(x.state)}</option>`).join('')
            || '<option value="">无</option>'}
        </select></div>
      ${(cat.vd || []).map(x => `<div class="field"><label title="${esc(x.risk || '')}">${esc(x.label)}</label>
        <select class="select" data-vdpolicy="${esc(x.key)}">
          ${x.values.map(vv => `<option value="${esc(vv)}">${esc(vv)}</option>`).join('')}
        </select></div>`).join('')}
    </div>`;

  $$('#policy-body [data-policy]').forEach(btn => btn.addEventListener('click', async () => {
    const key = btn.dataset.policy;
    const val = $(`#pol-${key}`).value;
    await setCtrlPolicy(key, val);
  }));
  $$('#policy-body [data-toggle]').forEach(cb => cb.addEventListener('change', async () => {
    await setCtrlPolicy(cb.dataset.toggle, cb.checked ? 'on' : 'off');
  }));
  $$('#policy-body [data-vdpolicy]').forEach(sel => sel.addEventListener('change', async () => {
    const vd = $('#vd-pick').value;
    if (vd === '') return;
    if (sel.dataset.vdpolicy === 'accesspolicy' && sel.value !== 'RW') {
      if (!window.confirm(`把 ${vd} 设为 ${sel.value} 会立即影响业务访问，确认继续？`)) {
        await loadCtrlPolicy();
        return;
      }
    }
    try {
      await api('/api/vd_policy', {
        method: 'POST',
        body: { ctrl: 0, vd: Number(vd.split('/').pop()), key: sel.dataset.vdpolicy, value: sel.value },
      });
      toast('已下发，稍后刷新查看状态', 'ok');
    } catch (e) { toast(e.message, 'error'); }
  }));
}

async function setCtrlPolicy(key, value) {
  try {
    await api('/api/ctrl_policy', { method: 'POST', body: { ctrl: 0, key, value } });
    toast('策略已下发，稍后刷新查看', 'ok');
    setTimeout(loadCtrlPolicy, 1500);
  } catch (e) {
    toast(e.message, 'error');
    await loadCtrlPolicy();
  }
}

async function loadPhyCounters() {
  const box = $('#phy-body');
  if (!box) return;
  box.innerHTML = '<div class="muted tiny">读取中…</div>';
  let d;
  try {
    d = await api('/api/phy_counters?ctrl=0');
  } catch (e) {
    box.innerHTML = `<div class="muted">读取失败：${esc(e.message)}</div>`;
    return;
  }
  const rows = d.disks || [];
  box.innerHTML = rows.length ? `<table class="data">
    <thead><tr><th>磁盘</th><th>计数项</th><th class="num">值</th><th></th></tr></thead>
    <tbody>${rows.map(x => {
      const items = (x.phy || []).filter(p => String(p.v) !== '0');
      if (!items.length) {
        return `<tr><td class="mono">${esc(x.label)}</td><td colspan="2" class="muted">${esc(x.err || '无异常计数（全 0）')}</td>
          <td><button class="btn sm ghost" data-phyreset="${x.eid}:${x.slot}">复位</button></td></tr>`;
      }
      return items.map((p, i) => `<tr>
        <td class="mono">${i === 0 ? esc(x.label) : ''}</td>
        <td class="tiny">${esc(p.k)}</td>
        <td class="num mono">${esc(p.v)}</td>
        <td>${i === 0 ? `<button class="btn sm ghost" data-phyreset="${x.eid}:${x.slot}">复位</button>` : ''}</td>
      </tr>`).join('');
    }).join('')}</tbody></table>`
    : stateBlock('empty', '没有可读的物理盘', '读取 PHY 计数需要先识别到磁盘。');

  $$('#phy-body [data-phyreset]').forEach(b => b.addEventListener('click', async () => {
    const [eid, slot] = b.dataset.phyreset.split(':');
    if (!window.confirm('复位会清零该盘的 PHY 错误计数，用于观察之后是否继续增长。确认？')) return;
    try {
      await api('/api/phy_reset', { method: 'POST', body: { ctrl: 0, eid: Number(eid), slot: Number(slot) } });
      toast('已下发复位', 'ok');
    } catch (e) { toast(e.message, 'error'); }
  }));
}

const HW_STATUS_TEXT = {
  ok: '正常', nc: '非临界', cr: '临界', nr: '不可恢复',
  ns: '无读数', na: '不适用', nf: '不存在',
};

function hwRow(s) {
  const st = HW_STATUS_TEXT[s.status] || s.status;
  const cls = s.status === 'ok' ? 'ok' : s.status === 'nc' ? 'warn' : ['cr', 'nr'].includes(s.status) ? 'crit' : '';
  return `<tr>
    <td>${esc(s.name)}</td>
    <td class="num mono">${esc(s.raw || '—')}</td>
    <td>${cls ? `<span class="badge ${cls}">${esc(st)}</span>` : `<span class="muted">${esc(st)}</span>`}</td>
  </tr>`;
}

async function loadHardware(force) {
  const tb = $('#hw-fan-table tbody');
  if (!tb) return;
  if (force) {
    ['#hw-fan-table', '#hw-temp-table', '#hw-other-table'].forEach(sel => {
      const b = $(sel + ' tbody');
      if (b) b.innerHTML = '<tr><td colspan="3" class="muted">重新读取…</td></tr>';
    });
  }
  let d;
  try {
    d = await api('/api/hardware' + (force ? '?refresh=1' : ''));
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="3">${stateFromError(e)}</td></tr>`;
    return;
  }
  const g = d.groups || {};
  $('#hw-source').textContent = d.ok
    ? `${d.source === 'ipmi' ? 'BMC' : 'hwmon'} · ${d.at || ''}`
    : `传感器不可读：${d.error || '未知'}`;

  const c = d.counts || {};
  $('#hw-cards').innerHTML = [
    ['风扇', `${c.fans_spinning || 0} / ${c.fans || 0}`, '在转 / 传感器位（"无读数"的多为空位）'],
    ['温度传感器', String(c.temps || 0), 'BMC 与 hwmon 合计'],
    ['电压传感器', String(c.power || 0), '主板与 VRM'],
    ['整体', d.severity === 'crit' ? '严重' : d.severity === 'warn' ? '关注' : '正常',
      (d.findings || []).length ? `${d.findings.length} 项待处理` : '无异常'],
  ].map(([k, v, s]) => `<div class="card stat-card">
      <div class="eyebrow">${esc(k)}</div>
      <div class="stat-value">${esc(v)}</div>
      <div class="stat-sub">${esc(s)}</div>
    </div>`).join('');

  $('#hw-findings').innerHTML = (d.findings || []).map(f => `
    <div class="cache-finding ${esc(f.level)}" style="margin-top:10px">
      <div class="cf-head">
        <span class="badge ${f.level === 'crit' ? 'crit' : 'warn'}">${f.level === 'crit' ? '严重' : '关注'}</span>
        <strong>${esc(f.title)}</strong>
      </div>
      <div class="cf-detail">${esc(f.detail)}</div>
      ${f.advice ? `<div class="cf-advice">建议：${esc(f.advice)}</div>` : ''}
    </div>`).join('');

  const fans = g.fans || [];
  $('#hw-fan-table tbody').innerHTML =
    fans.map(hwRow).join('') || '<tr><td colspan="3" class="muted">未探测到风扇传感器</td></tr>';
  const temps = (g.temps || []).slice().sort((a, b) => (b.value || 0) - (a.value || 0));
  $('#hw-temp-table tbody').innerHTML =
    temps.map(hwRow).join('') || '<tr><td colspan="3" class="muted">未探测到温度传感器</td></tr>';
  const others = [...(g.power || []), ...(g.discrete || [])];
  $('#hw-other-table tbody').innerHTML =
    others.map(hwRow).join('') || '<tr><td colspan="3" class="muted">无</td></tr>';
}

/* ---------- 顶栏控制台：全局搜索 / 通知 / 操作记录 ---------- */

const NAV_ITEMS = [
  { view: 'overview', title: '监控概览', sub: '健康评分、告警、趋势' },
  { view: 'storage', title: '文件管理', sub: 'bcache 加速、文件系统、NFS' },
  { view: 'logs', title: '日志中心', sub: '事件日志与控制器日志' },
  { view: 'ops', title: '运维中心', sub: '盘位可视化、资产台账、换盘' },
  { view: 'reports', title: '报表中心', sub: '日报、周报、月报与导出' },
  { view: 'hosts', title: '多机管理', sub: '机房内所有存储节点的统一视图' },
  { view: 'users', title: '用户管理', sub: '账号与角色' },
];

let _cmdkItems = [];
let _cmdkIndex = 0;
let _taskFilter = 'all';
let _taskExpandedId = null;

function cmdkIsOpen() {
  const el = $('#cmdk');
  return el && !el.classList.contains('hidden');
}

function cmdkOpen() {
  const el = $('#cmdk');
  if (!el) return;
  el.classList.remove('hidden');
  const input = $('#cmdk-input');
  input.value = '';
  cmdkRender('');
  input.focus();
}

function cmdkClose() {
  const el = $('#cmdk');
  if (el) el.classList.add('hidden');
}

/* 搜索目标：页面 + 物理磁盘 + 虚拟磁盘。
   全部来自已有的 state，不额外请求——命令面板必须即时响应。 */
function cmdkBuild(query) {
  const q = String(query || '').trim().toLowerCase();
  const groups = [];

  const navs = NAV_ITEMS.filter(
    n => !q || (n.title + ' ' + n.sub + ' ' + n.view).toLowerCase().includes(q)
  );
  if (navs.length) {
    groups.push({
      name: '页面',
      items: navs.map(n => ({ kind: 'nav', view: n.view, title: n.title, sub: n.sub, tag: '页面' })),
    });
  }

  const disks = (state.status && state.status.physical_disks) || [];
  const hitDisks = disks.filter(d => {
    if (!q) return false;
    const hay = [
      d.label, d.full_label, 'c' + bayCtrl(d), d.model, d.sn, d.state, d.dg,
    ].join(' ').toLowerCase();
    return hay.includes(q);
  });
  if (hitDisks.length) {
    groups.push({
      name: '物理磁盘',
      items: hitDisks.map(d => ({
        kind: 'disk',
        disk: d,
        title: `${d.full_label || d.label || ''} · ${d.state || ''}`,
        sub: [d.model, d.size, d.sn].filter(Boolean).join(' · '),
        tag: 'C' + bayCtrl(d),
      })),
    });
  }

  const vds = state.vds || [];
  const hitVds = vds.filter(v => {
    if (!q) return false;
    const hay = ['vd ' + v.vd, v.dg_vd, v.type, v.name, v.size, v.os_device, 'c' + (v.ctrl != null ? v.ctrl : 0)]
      .join(' ').toLowerCase();
    return hay.includes(q);
  });
  if (hitVds.length) {
    groups.push({
      name: '虚拟磁盘',
      items: hitVds.map(v => ({
        kind: 'vd',
        vd: v,
        title: `C${v.ctrl != null ? v.ctrl : 0} · VD ${v.vd} · ${v.dg_vd || ''}`,
        sub: [v.type, v.size, v.state, v.os_device].filter(Boolean).join(' · '),
        tag: '阵列',
      })),
    });
  }

  return groups;
}

function cmdkRender(query) {
  const box = $('#cmdk-results');
  const groups = cmdkBuild(query);
  _cmdkItems = [];
  groups.forEach(g => g.items.forEach(i => _cmdkItems.push(i)));
  _cmdkIndex = 0;
  $('#cmdk-hint').textContent = _cmdkItems.length ? `${_cmdkItems.length} 项结果` : '';

  if (!_cmdkItems.length) {
    box.innerHTML = query
      ? '<div class="muted" style="padding:16px 12px">没有匹配的磁盘、阵列或页面</div>'
      : '<div class="muted" style="padding:16px 12px">输入盘位、序列号、型号或页面名开始搜索</div>';
    return;
  }

  let idx = 0;
  box.innerHTML = groups
    .map(g => {
      const rows = g.items
        .map(i => {
          const n = idx++;
          return `<button type="button" class="cmdk-item${n === 0 ? ' active' : ''}" data-idx="${n}">
            <span class="cmdk-tag">${esc(i.tag)}</span>
            <span class="cmdk-item-title">${esc(i.title)}</span>
            <span class="cmdk-item-sub">${esc(i.sub || '')}</span>
          </button>`;
        })
        .join('');
      return `<div class="cmdk-group">${esc(g.name)}</div>${rows}`;
    })
    .join('');

  $$('#cmdk-results .cmdk-item').forEach(el => {
    el.addEventListener('click', () => cmdkRun(_cmdkItems[Number(el.dataset.idx)]));
    el.addEventListener('mousemove', () => {
      const i = Number(el.dataset.idx);
      if (i !== _cmdkIndex) cmdkHighlight(i);
    });
  });
}

function cmdkHighlight(i) {
  _cmdkIndex = i;
  $$('#cmdk-results .cmdk-item').forEach(el =>
    el.classList.toggle('active', Number(el.dataset.idx) === i)
  );
  const el = $(`#cmdk-results .cmdk-item[data-idx="${i}"]`);
  if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
}

function cmdkRun(item) {
  if (!item) return;
  cmdkClose();
  if (item.kind === 'nav') {
    switchView(item.view);
    return;
  }
  if (item.kind === 'disk') {
    switchView('overview');
    openDrawer(item.disk);
    return;
  }
  if (item.kind === 'vd') {
    switchView('overview');
    const card = $('#vd-table');
    if (card && card.scrollIntoView) card.scrollIntoView({ block: 'center' });
  }
}

/* 铃铛统计的是"未确认告警"，不是"未读事件"。
   运维关心的是有没有事情需要处理，而每条采集完成、每次登录都算
   "未读事件"的话，角标永远是满的，也就等于没有信息。 */
async function renderNotifications() {
  const body = $('#notify-body');
  if (!body) return;
  let d;
  try {
    d = await api('/api/alerts?status=open&limit=30');
  } catch (e) {
    body.innerHTML = `<div class="muted" style="padding:14px 6px">加载失败：${esc(e.message)}</div>`;
    return;
  }
  const alerts = d.alerts || [];
  const counts = d.counts || {};
  const unack = Number(counts.active || 0);

  const badge = $('#notify-count');
  if (badge) {
    badge.textContent = unack > 99 ? '99+' : String(unack);
    badge.classList.toggle('hidden', unack === 0);
  }

  if (!alerts.length) {
    body.innerHTML = '<div class="muted" style="padding:14px 6px">没有需要处理的告警</div>';
    return;
  }
  body.innerHTML =
    alerts
      .map(a => {
        const lv = String(a.level || 'warning');
        const tone = lv === 'error' ? 'crit' : lv === 'warning' ? 'warn' : 'info';
        const text = { crit: '错误', warn: '警告', info: '信息' }[tone] || lv;
        const acked = a.status === 'acknowledged';
        return `<div class="pop-item${acked ? '' : ' unread'}">
        <span class="badge ${tone}">${text}</span>
        <span class="pop-msg">${esc(a.title || a.key)}${Number(a.count) > 1 ? ` ×${esc(a.count)}` : ''}</span>
        <span class="pop-time">${esc(a.last_seen || '')}${acked ? ' · 已确认' : ''}</span>
      </div>`;
      })
      .join('') +
    '<div class="pop-foot"><button class="btn sm ghost" id="pop-goto-alerts">前往告警中心</button></div>';
  const go = $('#pop-goto-alerts');
  if (go) {
    go.addEventListener('click', () => {
      $('#notify-panel').classList.add('hidden');
      switchView('overview');
      const card = $('#card-alerts');
      if (card && card.scrollIntoView) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }
}

const TASK_STATUS_TEXT = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
};

function taskTone(status) {
  const s = String(status || '');
  if (s === 'succeeded') return 'ok';
  if (s === 'failed') return 'crit';
  return 'info';
}

function fmtDuration(t) {
  const a = t.started_at || t.created_at;
  const b = t.finished_at;
  if (!a || !b) return '';
  const toSec = s => {
    const m = String(s).match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/);
    if (!m) return null;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) / 1000;
  };
  const x = toSec(a);
  const y = toSec(b);
  if (x == null || y == null) return '';
  const d = Math.max(0, Math.round(y - x));
  if (d < 60) return d + ' 秒';
  if (d < 3600) return Math.floor(d / 60) + ' 分 ' + (d % 60) + ' 秒';
  return Math.floor(d / 3600) + ' 小时 ' + Math.floor((d % 3600) / 60) + ' 分';
}

async function renderTaskList() {
  const box = $('#task-list');
  if (!box) return;
  let d;
  try {
    d = await api(`/api/tasks?limit=80&status=${encodeURIComponent(_taskFilter)}`);
  } catch (e) {
    box.innerHTML = stateFromError(e);
    return;
  }
  const tasks = d.tasks || [];
  applyTaskBadge(d.stats || {});

  if (!tasks.length) {
    box.innerHTML = stateBlock('empty', '还没有任务记录', '执行一次阵列或磁盘操作后，这里会留下完整留痕。');
    return;
  }

  const expanded = _taskExpandedId;
  box.innerHTML = tasks
    .map(t => {
      const tone = taskTone(t.status);
      const statusText = TASK_STATUS_TEXT[t.status] || t.status;
      const running = t.status === 'running' || t.status === 'queued';
      const pct = t.progress == null ? (t.status === 'succeeded' ? 100 : 0) : t.progress;
      const dur = fmtDuration(t);
      const attempt = Number(t.attempt || 1) > 1 ? ` · 第 ${t.attempt} 次` : '';
      const isOpen = expanded != null && Number(expanded) === Number(t.id);
      return `<div class="task-card ${tone}" data-id="${esc(t.id)}">
        <div class="task-card-head">
          <span class="badge ${tone}">${esc(statusText)}</span>
          <span class="task-card-title">${esc(t.title || t.kind)}</span>
          <span class="task-card-time mono">${esc(t.created_at || '')}</span>
        </div>
        ${t.target ? `<div class="task-card-target mono">${esc(t.target)}${esc(attempt)}</div>` : ''}
        ${running || (t.progress != null && t.status === 'running')
          ? `<div class="task-bar"><i style="width:${pct}%"></i></div>`
          : ''}
        ${t.message ? `<div class="task-card-msg ${tone}">${esc(t.message)}</div>` : ''}
        <div class="task-detail${isOpen ? '' : ' hidden'}" data-detail="${esc(t.id)}"></div>
      </div>`;
    })
    .join('');

  $$('#task-list .task-card').forEach(card =>
    card.addEventListener('click', ev => {
      if (ev.target.closest && ev.target.closest('button')) return;
      toggleTaskDetail(Number(card.dataset.id));
    })
  );

  // 抽屉每 2.5 秒轮询一次会重建列表。展开状态必须存在模块变量里、
  // 重渲染后还原，否则用户正在看输出时详情会自己收起来。
  if (expanded != null) {
    const el = $(`#task-list .task-detail[data-detail="${expanded}"]`);
    if (el) renderTaskDetailInto(el, Number(expanded));
  }
}

function toggleTaskDetail(id) {
  _taskExpandedId = _taskExpandedId === id ? null : id;
  renderTaskList();
}

async function renderTaskDetailInto(el, id) {
  el.innerHTML = '<div class="loading-line">加载中…</div>';
  let t;
  try {
    const d = await api('/api/tasks/' + id);
    t = d.task;
  } catch (e) {
    el.innerHTML = stateFromError(e);
    return;
  }
  if (!t) return;
  const dur = fmtDuration(t);
  const params = t.params && Object.keys(t.params).length
    ? JSON.stringify(t.params, null, 1)
    : '—';
  el.innerHTML = `
    <dl class="kv task-kv">
      <dt>操作者</dt><dd>${esc(t.operator || '—')}</dd>
      <dt>开始</dt><dd>${esc(t.started_at || '—')}</dd>
      <dt>结束</dt><dd>${esc(t.finished_at || (t.status === 'running' ? '进行中' : '—'))}</dd>
      <dt>耗时</dt><dd>${esc(dur || '—')}</dd>
      <dt>命令</dt><dd class="mono">${esc(t.command || '—')}</dd>
    </dl>
    <details class="task-sect"><summary>参数</summary><pre class="smart-pre">${esc(params)}</pre></details>
    ${t.output ? `<details class="task-sect"><summary>执行输出</summary><pre class="smart-pre">${esc(t.output)}</pre></details>` : ''}
    ${t.status === 'failed' && t.retryable
      ? `<div class="task-actions"><button class="btn sm" data-retry="${esc(t.id)}">重试此任务</button>
         <span class="tiny">该操作可安全重跑</span></div>`
      : ''}
    ${t.status === 'failed' && !t.retryable
      ? '<div class="tiny task-hint">此操作不可重试：重复执行可能造成额外影响，请人工确认当前状态后再手动操作。</div>'
      : ''}`;
  const rb = el.querySelector('[data-retry]');
  if (rb) {
    rb.addEventListener('click', async ev => {
      ev.stopPropagation();
      btnLoading(rb, true);
      try {
        const r = await api(`/api/tasks/${t.id}/retry`, { method: 'POST' });
        if (r && r.ok === false) throw new Error(r.error || '重试失败');
        toast(`任务已重新提交（#${r.task && r.task.id}）`, 'ok');
        await renderTaskList();
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        btnLoading(rb, false);
      }
    });
  }
}

function applyTaskBadge(stats) {
  const badge = $('#task-count');
  if (!badge) return;
  const active = Number(stats.active || 0);
  const failed = Number(stats.failed || 0);
  // 有任务在跑就报运行中；没有在跑但有失败，则报失败数——
  // 后者才是真正需要人介入的状态。
  if (active > 0) {
    badge.textContent = active > 99 ? '99+' : String(active);
    badge.className = 'tb-badge info';
  } else if (failed > 0) {
    badge.textContent = failed > 99 ? '99+' : String(failed);
    badge.className = 'tb-badge';
  } else {
    badge.textContent = '0';
    badge.className = 'tb-badge hidden';
  }
}

async function refreshTaskBadge() {
  try {
    const d = await api('/api/tasks?limit=1&status=all');
    applyTaskBadge(d.stats || {});
  } catch (e) {
    /* 后端不可用时静默：顶栏角标不该刷 toast */
  }
}

/* 刷新顶栏三个入口的计数（状态刷新后调用） */
async function refreshConsoleBadges() {
  await renderNotifications();
  await refreshTaskBadge();
  if ($('#task-drawer') && $('#task-drawer').classList.contains('open')) {
    await renderTaskList();
  }
}

function bindConsoleUI() {
  const search = $('#btn-search');
  if (search) search.addEventListener('click', cmdkOpen);
  const cmdkScrim = $('#cmdk-scrim');
  if (cmdkScrim) cmdkScrim.addEventListener('click', cmdkClose);

  const input = $('#cmdk-input');
  if (input) {
    input.addEventListener('input', () => cmdkRender(input.value));
    input.addEventListener('keydown', ev => {
      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        if (_cmdkItems.length) cmdkHighlight((_cmdkIndex + 1) % _cmdkItems.length);
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (_cmdkItems.length) cmdkHighlight((_cmdkIndex - 1 + _cmdkItems.length) % _cmdkItems.length);
      } else if (ev.key === 'Enter') {
        ev.preventDefault();
        cmdkRun(_cmdkItems[_cmdkIndex]);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        cmdkClose();
      }
    });
  }

  document.addEventListener('keydown', ev => {
    const tag = (ev.target && ev.target.tagName) || '';
    const typing = /INPUT|TEXTAREA|SELECT/.test(tag) || (ev.target && ev.target.isContentEditable);
    if ((ev.metaKey || ev.ctrlKey) && String(ev.key).toLowerCase() === 'k') {
      ev.preventDefault();
      if (cmdkIsOpen()) cmdkClose();
      else cmdkOpen();
      return;
    }
    if (ev.key === '/' && !typing && !cmdkIsOpen()) {
      ev.preventDefault();
      cmdkOpen();
      return;
    }
    if (ev.key === 'Escape' && cmdkIsOpen()) cmdkClose();
  });

  const notifyBtn = $('#btn-notify');
  if (notifyBtn) {
    notifyBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      const panel = $('#notify-panel');
      const willOpen = panel.classList.contains('hidden');
      panel.classList.toggle('hidden', !willOpen);
      if (willOpen) {
        const r = notifyBtn.getBoundingClientRect();
        panel.style.top = `${Math.round(r.bottom + 8)}px`;
        panel.style.right = `${Math.round(Math.max(12, window.innerWidth - r.right))}px`;
        renderNotifications();
      }
    });
  }
  const readBtn = $('#btn-notify-read');
  if (readBtn) {
    readBtn.addEventListener('click', async () => {
      try {
        const r = await api('/api/alerts/ack_all', { method: 'POST' });
        toast(`已确认 ${r.acknowledged || 0} 条告警`, 'ok');
        await renderNotifications();
        await loadAlertCenter();
      } catch (e) {
        toast(e.message, 'error');
      }
    });
  }
  document.addEventListener('click', ev => {
    const panel = $('#notify-panel');
    if (!panel || panel.classList.contains('hidden')) return;
    if (panel.contains(ev.target) || (ev.target.closest && ev.target.closest('#btn-notify'))) return;
    panel.classList.add('hidden');
  });

  const taskBtn = $('#btn-tasks');
  const taskOpen = () => {
    $('#task-scrim').classList.remove('hidden');
    $('#task-drawer').classList.add('open');
    renderTaskList();
    startTaskPoll();
  };
  const taskClose = () => {
    $('#task-scrim').classList.add('hidden');
    $('#task-drawer').classList.remove('open');
    _taskExpandedId = null;
    stopTaskPoll();
  };
  if (taskBtn) taskBtn.addEventListener('click', taskOpen);
  if ($('#task-close')) $('#task-close').addEventListener('click', taskClose);
  if ($('#task-scrim')) $('#task-scrim').addEventListener('click', taskClose);
  $$('#task-filter .chip').forEach(ch =>
    ch.addEventListener('click', () => {
      $$('#task-filter .chip').forEach(c => c.classList.toggle('active', c === ch));
      _taskFilter = ch.dataset.tlevel || 'all';
      renderTaskList();
    })
  );
}

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
    const r = await api('/api/foreign_import', {
      method: 'POST',
      body: { acknowledge: true, ctrl: Number(data.ctrl != null ? data.ctrl : 0) },
    });
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
      // 登录响应只带基础字段，权限矩阵在 /api/me 里。必须重新拉一次：
      // 拿登录响应拼 state.me 会让 perms 缺失，afterLogin 退到默认的
      // ['view']，结果是管理员登录后也看不到任何管理入口。
      try {
        state.me = await api('/api/me');
      } catch (e) {
        /* 拉取失败时保留登录响应里的基础信息，至少能进入只读界面 */
      }
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

  // 告警中心：状态筛选、批量确认、维护窗口
  // 报表中心：周期切换与导出
  $$('#report-period .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#report-period .chip').forEach(c => c.classList.toggle('active', c === ch));
    _reportPeriod = ch.dataset.period || 'day';
    loadReports();
  }));
  const repOpen = $('#btn-report-open');
  if (repOpen) repOpen.addEventListener('click', () => window.open(reportUrl(), '_blank'));
  const repHtml = $('#btn-report-html');
  if (repHtml) repHtml.addEventListener('click', () => {
    window.location.href = reportUrl('&download=1');
  });
  const repCsv = $('#btn-report-csv');
  if (repCsv) repCsv.addEventListener('click', () => {
    window.location.href = reportUrl('&format=csv');
  });
  const rsched = $('#btn-report-sched-save');
  if (rsched) rsched.addEventListener('click', rschSave);
  const rsend = $('#btn-report-send');
  if (rsend) rsend.addEventListener('click', rschSendNow);

  // 性能中心：实时刷新、指标与时间窗切换、设备曲线显隐
  const perfRefresh = $('#btn-perf-refresh');
  if (perfRefresh) perfRefresh.addEventListener('click', () => loadPerfLive());
  const iodBtn = $('#btn-iodiag');
  if (iodBtn) iodBtn.addEventListener('click', runIoDiagnosis);
  const benchStart = $('#btn-bench-start');
  if (benchStart) benchStart.addEventListener('click', startIoBench);
  const benchCancel = $('#btn-bench-cancel');
  if (benchCancel) benchCancel.addEventListener('click', async () => {
    try {
      await api('/api/io/bench/cancel', { method: 'POST' });
      toast('已请求取消，临时文件会被清理', 'ok');
      await loadIoBench();
    } catch (e) { toast(e.message, 'error'); }
  });
  $$('#perf-metric .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#perf-metric .chip').forEach(c => c.classList.toggle('active', c === ch));
    state.perfMetric = ch.dataset.metric || 'iops';
    loadPerfTrend();
  }));
  $$('#perf-range .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#perf-range .chip').forEach(c => c.classList.toggle('active', c === ch));
    state.perfHours = Number(ch.dataset.hours) || 24;
    loadPerfTrend();
  }));
  const devChips = $('#perf-dev-chips');
  if (devChips) devChips.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.perf-dev-chip');
    if (!btn) return;
    const name = btn.dataset.dev;
    const i = _perfDevices.indexOf(name);
    if (i >= 0) _perfDevices.splice(i, 1);
    else _perfDevices.push(name);
    loadPerfTrend();
  });
  startPerfAuto();

  const dbStart = $('#btn-dbackup-start');
  if (dbStart) dbStart.addEventListener('click', startDataBackup);
  $$('[data-pick]').forEach(b => b.addEventListener('click', () => pickServerDir(b.dataset.pick)));
  const dbSched = $('#btn-db-sched-save');
  if (dbSched) dbSched.addEventListener('click', saveBackupSchedule);
  const dbList = $('#btn-dbackup-list');
  if (dbList) dbList.addEventListener('click', loadBackupArchives);

  const capRefresh = $('#btn-cap-refresh');
  if (capRefresh) capRefresh.addEventListener('click', () => loadCapacityForecast());
  const hwRefresh = $('#btn-hw-refresh');
  if (hwRefresh) hwRefresh.addEventListener('click', () => loadHardware(true));
  const polRefresh = $('#btn-policy-refresh');
  if (polRefresh) polRefresh.addEventListener('click', () => loadCtrlPolicy());
  const phyBtn = $('#btn-phy');
  if (phyBtn) phyBtn.addEventListener('click', loadPhyCounters);

  // 多机管理：筛选、全选、批量动作
  const hostSearch = $('#host-search');
  if (hostSearch) hostSearch.addEventListener('input', () => {
    _hostFilter.q = hostSearch.value.trim();
    if (_hostsPayload) renderHosts(_hostsPayload);
  });
  const hostStatus = $('#host-status');
  if (hostStatus) hostStatus.addEventListener('change', () => {
    _hostFilter.status = hostStatus.value;
    if (_hostsPayload) renderHosts(_hostsPayload);
  });
  const hostGroup = $('#host-group');
  if (hostGroup) hostGroup.addEventListener('change', () => {
    _hostFilter.group = hostGroup.value;
    if (_hostsPayload) renderHosts(_hostsPayload);
  });
  const hostAll = $('#host-check-all');
  if (hostAll) hostAll.addEventListener('change', () => {
    const list = ((_hostsPayload || {}).hosts || []).filter(hostMatches);
    _hostFilter.sel = hostAll.checked ? new Set(list.map(h => Number(h.id))) : new Set();
    if (_hostsPayload) renderHosts(_hostsPayload);
  });
  ['enable', 'disable', 'group'].forEach((act) => {
    const btn = $('#btn-host-' + act);
    if (btn) btn.addEventListener('click', () => bulkHostAction(act));
  });
  const sesRefresh = $('#btn-ses-refresh');
  if (sesRefresh) sesRefresh.addEventListener('click', () => loadEnclosure());

  // 维护计划：保存开关、增删计划、立即执行
  const maintSaveBtn = $('#btn-maint-save');
  if (maintSaveBtn) maintSaveBtn.addEventListener('click', () => maintSave(maintCurrentPlans()));
  const maintAddBtn = $('#btn-maint-add');
  if (maintAddBtn) maintAddBtn.addEventListener('click', maintAdd);
  const maintTb = $('#maint-table tbody');
  if (maintTb) maintTb.addEventListener('click', async (ev) => {
    const runBtn = ev.target.closest('[data-maint-run]');
    const delBtn = ev.target.closest('[data-maint-del]');
    if (runBtn) {
      btnLoading(runBtn, true);
      try {
        const r = await api('/api/maintenance/run', {
          method: 'POST',
          body: { id: Number(runBtn.dataset.maintRun) },
        });
        toast(r.ok ? `已下发：${r.message}` : `执行失败：${r.message}`, r.ok ? 'ok' : 'error');
        await loadMaintenance();
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        btnLoading(runBtn, false);
      }
      return;
    }
    if (delBtn) {
      const id = Number(delBtn.dataset.maintDel);
      const plans = maintCurrentPlans().filter(p => Number(p.id) !== id);
      await maintSave(plans);
    }
  });

  $$('#alert-status-filter .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#alert-status-filter .chip').forEach(c => c.classList.toggle('active', c === ch));
    _alertStatus = ch.dataset.astatus || 'open';
    loadAlertCenter();
  }));
  const ackAll = $('#btn-alert-ackall');
  if (ackAll) ackAll.addEventListener('click', async () => {
    btnLoading(ackAll, true);
    try {
      const r = await api('/api/alerts/ack_all', { method: 'POST' });
      toast(`已确认 ${r.acknowledged || 0} 条告警`, 'ok');
      await loadAlertCenter();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btnLoading(ackAll, false);
    }
  });
  const silenceBtn = $('#btn-alert-silence');
  if (silenceBtn) silenceBtn.addEventListener('click', () => openSilenceModal('all'));
  const ackAllTop = $('#btn-alert-ackall');
  if (ackAllTop) ackAllTop.classList.toggle('hidden', !state.canOperate);
  if (silenceBtn) silenceBtn.classList.toggle('hidden', !state.canOperate);
  const auditBtn = $('#btn-audit-export');
  if (auditBtn) {
    auditBtn.classList.toggle('hidden', !state.canAudit);
    auditBtn.addEventListener('click', () => {
      window.location.href = '/api/audit/export';
    });
  }

  // 磁盘健康 TOP：切换排序口径
  $$('#health-metric .chip').forEach(ch => ch.addEventListener('click', () => {
    $$('#health-metric .chip').forEach(c => c.classList.toggle('active', c === ch));
    _healthMetric = ch.dataset.metric || 'wear';
    loadDiskHealth();
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
    $('#fs-usage-body').innerHTML = stateFromError(e);
    return;
  }
  state.fsUsage = data.filesystems || [];
  renderFsCard();
  renderFsTable();
}

function renderFsCard() {
  const list = (state.fsUsage || []).filter(fs => !fs.hidden);
  const body = $('#fs-usage-body');
  if (!list.length) { body.innerHTML = stateBlock('empty', '还没有文件系统样本', '采集器刚启动时，需要等待一轮采集。'); return; }
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
  if (!list.length) { tb.innerHTML = '<tr><td colspan="8">' + stateBlock('empty', '还没有文件系统样本', '采集器刚启动时，需要等待一轮采集。', '', true) + '</td></tr>'; return; }
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
  catch (e) { tb.innerHTML = `<tr><td colspan="4">${stateFromError(e)}</td></tr>`; return; }
  state.nfsLoaded = true;
  $('#nfs-unavailable').classList.toggle('hidden', !!data.available);
  $('#btn-nfs-install').classList.toggle('hidden', !state.isAdmin);
  $('#nfs-form').classList.toggle('hidden', !state.isAdmin || !data.available);
  $$('#nfs-table .admin-col').forEach(el => el.classList.toggle('col-hidden', !state.isAdmin));

  const rows = [];
  (data.exports || []).forEach(ex => {
    (ex.clients || []).forEach(c => rows.push({ path: ex.path, host: c.host, options: c.options }));
  });
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="4">' + stateBlock('empty', '还没有 NFS 共享', '点击上方「新增共享」导出一个目录。', '', true) + '</td></tr>'; return; }
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
    body.innerHTML = stateFromError(e);
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
  const cacheUnreg = csets.find(c => c.cache_present_unregistered && c.detected_cache_device) || null;
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
    ${cacheUnreg ? `<div class="banner" style="margin-top:10px;border-color:color-mix(in srgb, var(--warn) 45%, transparent);background:var(--warn-bg)">
      <span class="badge warn">缓存盘未注册</span><span>检测到缓存盘 ${esc(cacheUnreg.detected_cache_device)} 已在系统中，但尚未注册到 bcache（当前无缓存）。点击下方「重新注册并恢复缓存」即可恢复加速。</span>
    </div>` : cacheOffline ? `<div class="banner" style="margin-top:10px;border-color:color-mix(in srgb, var(--crit) 45%, transparent);background:var(--crit-bg)">
      <span class="badge crit">缓存盘离线</span><span>检测到缓存盘已不在系统中（bcache 已降级为 no cache）。请检查缓存盘连接，恢复后可重新注册并绑定。</span>
    </div>` : ''}
    ${csets.length ? `<div class="tiny" style="margin-top:14px">缓存集统计：</div>
      ${csets.map(c => {
        const uuid = c.uuid || '';
        const offline = !!c.cache_offline;
        const periodStats = c.period_stats || {};
        _bcacheCsetPeriodStats[uuid] = periodStats;
        const totalStats = periodStats.total || {
          cache_hits: c.cache_hits,
          cache_misses: c.cache_misses,
          cache_bypass_hits: c.cache_bypass_hits,
          cache_bypass_misses: c.cache_bypass_misses,
          cache_hit_ratio: c.cache_hit_ratio,
        };
        const fmtNum = v => v == null || v === '' ? '—' : v;
        const missOf = st => (Number(st.cache_bypass_misses) || 0) + (Number(st.cache_misses) || 0);
        const ci = c.cache_info || {};
        const parseSize = (t) => {
          const m = String(t || '').trim().toLowerCase().match(/^([0-9.]+)\s*([kmgt]?)$/);
          if (!m) return 0;
          const unit = { k: 1024, m: 1024 ** 2, g: 1024 ** 3, t: 1024 ** 4 }[m[2] || ''] || 1;
          return Number(m[1]) * unit;
        };
        const cacheTotalBytes = (Number(ci.nbuckets) || 0) * parseSize(ci.bucket_size);
        const capFmt = bytes => bytes > 0 ? `≈${fmtBytes(bytes)}B` : '—';
        const unregistered = !!c.cache_present_unregistered;
        const noLive = offline || unregistered;
        const avail = noLive ? null : Number(c.cache_available_percent);
        const usage = avail != null && avail >= 0 ? (100 - avail) : null;
        const cards = [
          { cap: true },
          { hitCard: true },
          { specCard: true },
          { bucketCard: true },
        ];
        return `<div class="stat-mini-grid">${cards.map(x => {
          if (x.hitCard) {
            return `<div class="stat-mini">
              <span class="stat-mini-k">缓存命中率</span>
              <span class="stat-mini-v" data-hit-key="ratio">${noLive ? '—' : (totalStats.cache_hit_ratio == null ? '—' : `${totalStats.cache_hit_ratio}%`)}</span>
              <span class="stat-mini-metrics">
                <span><em>cache_hits</em><b data-hit-key="cache_hits">${noLive ? '—' : fmtNum(totalStats.cache_hits)}</b></span>
                <span><em>cache_bypass_hits</em><b data-hit-key="cache_bypass_hits">${noLive ? '—' : fmtNum(totalStats.cache_bypass_hits)}</b></span>
                <span><em>cache_miss</em><b data-hit-key="cache_miss">${noLive ? '—' : fmtNum(missOf(totalStats))}</b></span>
              </span>
              <span class="stat-mini-picker">
                <button type="button" class="active" data-hit-period="total" onclick="switchBcacheHitPeriod('${esc(uuid)}','total',this)">累计</button>
                <button type="button" data-hit-period="five_minute" onclick="switchBcacheHitPeriod('${esc(uuid)}','five_minute',this)">5分钟</button>
                <button type="button" data-hit-period="hour" onclick="switchBcacheHitPeriod('${esc(uuid)}','hour',this)">1小时</button>
                <button type="button" data-hit-period="day" onclick="switchBcacheHitPeriod('${esc(uuid)}','day',this)">1天</button>
              </span>
            </div>`;
          }
          if (x.specCard) {
            const nb = Number(ci.nbuckets);
            const nbText = Number.isFinite(nb) && nb > 0 ? nb.toLocaleString('zh-CN') : '—';
            const bucketText = ci.bucket_size ? String(ci.bucket_size).replace(/k$/i, ' KB') : '—';
            const capText = capFmt(cacheTotalBytes);
            const discard = ci.discard == null ? '—' : (String(ci.discard) === '1' ? '开启' : (String(ci.discard) === '0' ? '关闭' : String(ci.discard)));
            const row = (label, val) => `<span><em>${label}</em><b>${offline ? '—' : val}</b></span>`;
            return `<div class="stat-mini">
              <span class="stat-mini-k">缓存盘规格</span>
              <span class="stat-mini-metrics">
                ${row('总桶数', nbText)}
                ${row('每桶容量', bucketText)}
                ${row('理论容量', capText)}
                ${row('discard（TRIM）', discard)}
              </span>
            </div>`;
          }
          if (x.bucketCard) {
            const row = (label, val) => `<span><em>${label}</em><b>${noLive ? '—' : `${fmtNum(val)}%`}</b></span>`;
            return `<div class="stat-mini bucket">
              <span class="stat-mini-k">缓存桶构成</span>
              <span class="stat-mini-metrics">
                ${row('脏数据桶占比（GC 标记 · 非待写回量）', ci.bucket_dirty)}
                ${row('干净数据（与磁盘一致）', ci.bucket_clean)}
                ${row('空闲（未使用）', ci.bucket_unused)}
                ${row('元数据（bcache 索引）', ci.bucket_metadata)}
              </span>
            </div>`;
          }
          if (x.cap) {
            const unusedPct = noLive ? null : Number(ci.bucket_unused);
            const cleanPct = noLive ? null : Number(ci.bucket_clean);
            const rowCap = (pct) => (noLive || pct == null || isNaN(pct))
              ? '—'
              : `${fmtNum(pct)}% · ${capFmt(cacheTotalBytes * pct / 100)}`;
            return `<div class="stat-mini cap">
              <span class="stat-mini-k">缓存容量</span>
              <span class="stat-mini-cap-vals">
                <span class="free"><b>${noLive ? '—' : `${avail}%`}</b><i>可用·含可回收</i></span>
                <span class="used"><b>${noLive ? '—' : `${usage}%`}</b><i>脏标记·待GC回收</i></span>
              </span>
              <span class="stat-mini-bar">${noLive ? '' : `<i style="width:${Math.max(0, Math.min(100, usage))}%"></i>`}</span>
              <span class="stat-mini-metrics">
                <span><em>总容量</em><b>${capFmt(cacheTotalBytes)}</b></span>
                <span><em>空桶（真空闲）</em><b>${rowCap(unusedPct)}</b></span>
                <span><em>干净缓存（可回收）</em><b>${rowCap(cleanPct)}</b></span>
              </span>
            </div>`;
          }
          return `<div class="stat-mini">
            <span class="stat-mini-k">${esc(x.k)}</span>
            <span class="stat-mini-v">${x.v}</span>
            <span class="stat-mini-s">${x.s}</span>
          </div>`;
        }).join('')}</div>`;
      }).join('')}` : ''}`;
  renderBcacheGcHint(csets, devs);
  appendBcacheControls(body, data, devData);
  if (devs.length) {
    renderBcacheTrend(devs[0].name);
  } else {
    state.bcacheTrendDevice = null;
    clearBcacheTrend();
  }
  if (csets.length) renderBcacheTopology(csets, devs);
  loadBcacheAlertSettings();
  if (state.isAdmin) {
    devs.forEach(x => loadBcacheTunables(x.name));
  }
  loadBcacheEmergency();
}

async function renderBcacheGcHint(csets, devs) {
  const body = $('#bcache-body');
  if (!body || !csets.length || !devs.length) return;
  const old = document.getElementById('bcache-gc-hint');
  if (old) old.remove();
  const c = csets[0];
  const dev = devs[0];
  const ci = c.cache_info || {};
  const dirtyPct = Number(ci.bucket_dirty);
  const rawDirty = String(dev.dirty_data || '').trim().toLowerCase();
  const dm = rawDirty.match(/^([0-9.]+)\s*([kmgt]?)$/);
  if (!(dirtyPct > 0) || !dm) return;
  const unit = { k: 1024, m: 1048576, g: 1073741824, t: 1099511627776 }[dm[2] || ''] || 1;
  const dirtyBytes = Number(dm[1]) * unit;
  if (!Number.isFinite(dirtyBytes) || dirtyBytes > 0) return;
  const grid = body.querySelector('.stat-mini-grid');
  if (!grid) return;
  const wrap = document.createElement('div');
  wrap.id = 'bcache-gc-hint';
  wrap.innerHTML = '<div class="banner" style="margin-top:10px"><span class="badge warn">桶标记待刷新</span><span style="flex:1">检测中…</span></div>';
  grid.after(wrap);
  const fmtDur = (s) => {
    const v = Number(s);
    if (s == null || s === '' || !Number.isFinite(v)) return '—';
    if (v < 60) return `${Math.round(v)} 秒`;
    if (v < 3600) return `${Math.round(v / 60)} 分钟`;
    return `${(v / 3600).toFixed(1)} 小时`;
  };
  let gc = {};
  try {
    gc = await api('/api/bcache/gc?uuid=' + encodeURIComponent(c.uuid));
  } catch (e) { /* 提示仍展示，GC 统计缺失时用占位 */ }
  const last = gc.btree_gc_last_sec;
  const freq = gc.btree_gc_average_frequency_sec;
  const hint = `脏标记 ${dirtyPct}% 尚未刷新，实际脏数据 ${esc(dev.dirty_data)}（已写回完成）。距上次 GC ${fmtDur(last)}，历史平均约每 ${fmtDur(freq)} 触发一次。`;
  const btn = state.isAdmin
    ? '<button class="btn sm primary" type="button">立即刷新标记</button>'
    : '';
  wrap.innerHTML = `<div class="banner" style="margin-top:10px">
    <span class="badge warn">桶标记待刷新</span>
    <span style="flex:1">${hint}</span>
    ${btn}
  </div>`;
  const refreshBtn = wrap.querySelector('button');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      try {
        const r = await api('/api/bcache/gc/trigger', { method: 'POST', body: { uuid: c.uuid } });
        toast(r.message || '已触发 GC', 'ok');
        await loadBcache();
      } catch (e) { toast(e.message, 'error'); }
    });
  }
}

async function loadBcacheEmergency() {
  const body = $('#bcache-body');
  if (!body) return;
  let data;
  try {
    data = await api('/api/bcache/emergency');
  } catch (e) {
    return;
  }
  const old = document.getElementById('bcache-emergency');
  if (old) old.remove();
  const el = document.createElement('div');
  el.id = 'bcache-emergency';
  const eligible = data.eligible_backing || [];
  const mounts = data.mounts || [];
  const admin = !!state.isAdmin;
  let html = '<div class="tiny" style="margin-top:14px">应急数据处理（缓存盘丢失时只读读取 backing 数据）</div>';
  if (admin && eligible.length) {
    html += '<div class="bcache-ops">' + eligible.map(d =>
      `<button class="btn sm" data-emg-mount="${esc(d.path)}">只读应急挂载 ${esc(d.path)}</button>`).join('') + '</div>';
  }
  if (mounts.length) {
    html += '<div class="hs-list" style="margin-top:8px">' + mounts.map(m =>
      `<div class="hs-row"><span class="mono">${esc(m.backing)}</span>
       <span class="tiny">${esc(m.loop)}</span>
       <span class="badge ok">${esc(m.mountpoint)} · ${m.readonly ? '只读' : '读写'}</span>
       ${admin ? `<button class="btn sm" data-emg-umount="${esc(m.backing)}">卸载</button>` : ''}
      </div>`).join('') + '</div>';
  } else {
    html += '<div class="tiny" style="margin-top:6px;color:var(--ink-faint)">当前没有应急只读挂载。检测到可挂载的 backing：' +
      (eligible.length ? eligible.map(d => d.path).join('、') : '无') + '</div>';
  }
  html += '<div class="tiny" style="margin-top:6px;color:var(--ink-faint)">应急模式只读，不会写入 backing，也不要求缓存盘存在。</div>';
  el.innerHTML = html;
  body.appendChild(el);
  el.querySelectorAll('[data-emg-mount]').forEach(b => b.addEventListener('click', async () => {
    try {
      const r = await api('/api/bcache/emergency/mount', { method: 'POST', body: { backing_path: b.dataset.emgMount } });
      toast(r.message || '已挂载', 'ok');
      await loadBcacheEmergency();
    } catch (e) { toast(e.message, 'error'); }
  }));
  el.querySelectorAll('[data-emg-umount]').forEach(b => b.addEventListener('click', async () => {
    try {
      const r = await api('/api/bcache/emergency/unmount', { method: 'POST', body: { backing_path: b.dataset.emgUmount } });
      toast(r.message || '已卸载', 'ok');
      await loadBcacheEmergency();
    } catch (e) { toast(e.message, 'error'); }
  }));
}

function appendBcacheControls(body, data, devData) {
  const admin = !!state.isAdmin;
  const devs = data.devices || [];
  const blockDevs = (devData && devData.devices) || [];
  const usable = d => !d.mounted && !d.in_bcache;
  const freeCache = blockDevs.filter(d => usable(d) && !d.has_partitions && (Number(d.rota) === 0 || /nvme/i.test(d.device)));
  const freeBacking = blockDevs.filter(d => usable(d) && d.bcache_role !== 'cache');
  const bcCands = blockDevs.filter(d => !d.in_bcache && d.bcache_role);
  const cacheCands = bcCands.filter(d => d.bcache_role === 'cache');
  const backingCands = bcCands.filter(d => d.bcache_role !== 'cache');
  const noActiveSet = (data.cache_sets || []).length === 0;
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
    const unregCset = (data.cache_sets || []).find(c => c.cache_present_unregistered && c.detected_cache_device);
    const poolCache = freeCache.length ? freeCache : (noActiveSet ? cacheCands : []);
    const poolBacking = freeBacking.length ? freeBacking : (noActiveSet ? backingCands : []);
    if (unregCset) {
      html += `<div class="tiny" style="margin-top:12px">检测到缓存盘 <span class="mono">${esc(unregCset.detected_cache_device)}</span> 已在系统中但尚未注册（缓存集 ${esc((unregCset.uuid || '').slice(0, 8))}… 当前无缓存）：</div>
        <div class="bcache-ops"><button class="btn sm primary" data-bcache-recover>重新注册并恢复缓存</button></div>`;
    } else if (poolCache.length && poolBacking.length) {
      const cacheOpts = poolCache.map(d =>
        `<option value="${esc(d.path)}">${esc(d.device)} · ${fmtSize(d.size)} · ${esc(d.model || d.tran || 'SSD')}${d.fstype || d.bcache_role ? ' · 旧数据(将被清空)' : ''}</option>`).join('');
      const backOpts = poolBacking.map(d =>
        `<option value="${esc(d.path)}">${esc(d.device)} · ${fmtSize(d.size)}${d.bcache_role || d.fstype || d.has_partitions ? ' · 旧数据(将被清空)' : ''}</option>`).join('');
      html += `
        <div class="field" style="margin-top:14px">
          <label>新建 bcache（缓存盘 + 被加速盘）</label>
          <div class="replace-steps" style="flex-wrap:wrap">
            <select class="select" id="bcache-cache">${cacheOpts}</select>
            <select class="select" id="bcache-backing">${backOpts || '<option value="">无可用盘</option>'}</select>
            <button class="btn sm danger" id="btn-bcache-create">创建缓存</button>
          </div>
          <label class="check-row" style="margin-top:8px"><input type="checkbox" id="bcache-ack" />
            <span>确认两块设备上的现有数据/旧 bcache 配置都可以被清空</span></label>
        </div>`;
    } else {
      const csetList = data.cache_sets || [];
      const degraded = csetList.some(c => c.cache_offline || c.cache_present_unregistered)
        || devs.some(x => /no cache/i.test(String(x.state || '')));
      const needBacking = !devs.length || devs.some(x => /no cache/i.test(String(x.state || '')));
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
          html += '<div class="tiny" style="margin-top:12px">检测到可用于恢复的缓存盘：</div><div class="bcache-ops"><button class="btn sm primary" data-bcache-recover>重新注册并恢复缓存</button></div>';
        } else if (needBacking && csetList.length && backingCands.length) {
          html += '<div class="tiny" style="margin-top:12px">检测到已有缓存集，可接入 backing：</div><div class="bcache-ops"><button class="btn sm primary" data-bcache-recover>接入 backing 并恢复缓存</button></div>';
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
                   <button class="btn sm danger" data-stop="${esc(n)}">停用</button>`}
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

  if (admin) {
    const cachePaths = Array.from(new Set([
      ...(data.cache_sets || []).map(c => c.cache_info && c.cache_info.device).filter(Boolean),
      ...cacheCands.map(d => d.path),
    ]));
    if (cachePaths.length) {
      html += `<div class="bcache-ops" style="margin-top:10px">
        <span class="tiny">销毁缓存盘（整块清空）：</span>
        ${cachePaths.map(p => `<button class="btn sm danger" data-destroy-cache="${esc(p)}">${esc(p)}</button>`).join('')}
      </div>`;
    }
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
      btnLoading(recoverBtn, true);
      toast('正在重新注册缓存盘并恢复缓存，可能需要几十秒，请勿关闭页面…');
      try {
        const r = await api('/api/bcache/recover', { method: 'POST' });
        toast(r.message || '缓存已恢复', 'ok');
        await loadBcache();
      } catch (e) {
        toast(e.message, 'error');
        try { await loadBcache(); } catch (_) { /* 忽略 */ }
        const again = $('#bcache-body').querySelector('[data-bcache-recover]');
        if (again) btnLoading(again, false);
      } finally {
        btnLoading(recoverBtn, false);
      }
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
    confirmModal(`停用 ${name}`, `<p class="warn-text">停用后 ${name} 会从系统移除；再次使用需要重新注册/attach。</p>`, '停用', true, async () => {
      const r = await api('/api/bcache/stop', { method: 'POST', body: { name } });
      toast(r.message || '已停用', 'ok');
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
  body.querySelectorAll('[data-destroy-cache]').forEach(b => b.addEventListener('click', () => {
    const path = b.dataset.destroyCache;
    confirmModal('一键销毁缓存盘',
      `<p class="warn-text">将注销缓存盘 ${esc(path)} 并清空整块盘（wipefs）。该盘上所有数据/旧 bcache 配置都会消失，且不可恢复。</p>
       <p>backing（被加速盘）上的正式数据不受影响，但缓存内容会丢失。</p>
       <div class="field"><label>输入设备路径确认</label>
       <input class="input mono" id="destroy-cache-confirm" placeholder="${esc(path)}" autocomplete="off" /></div>
       <label class="check-row"><input type="checkbox" id="destroy-cache-ack" />
       <span>我已知晓该盘将被整块清空且不可恢复</span></label>`,
      '确认销毁', true, async () => {
        const val = ($('#destroy-cache-confirm').value || '').trim();
        if (val !== path) throw new Error('设备路径不一致');
        if (!$('#destroy-cache-ack').checked) throw new Error('请勾选风险确认');
        const r = await api('/api/bcache/destroy_cache', { method: 'POST', body: { cache_path: path, confirm: val, acknowledge: true } });
        toast(r.message || '已销毁', 'ok');
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

function clearBcacheTrend() {
  if (_bcacheChart) { _bcacheChart.destroy(); _bcacheChart = null; }
  const old = document.getElementById('bcache-trend-wrap');
  if (old) old.remove();
}

async function renderBcacheTrend(devName) {
  if (typeof Chart === 'undefined' || !devName) return;
  const body = $('#bcache-body');
  if (!body) return;
  let s;
  try {
    s = await api('/api/bcache/trend?device=' + encodeURIComponent(devName) + '&hours=26');
  } catch (e) {
    return;
  }
  const points = (s && s.points) || [];
  clearBcacheTrend();
  const wrap = document.createElement('div');
  wrap.id = 'bcache-trend-wrap';
  const step = Math.max(1, Math.ceil(points.length / 320));
  const labels = [];
  const hourData = [];
  const dayData = [];
  const span = points.length ? (points[points.length - 1].ts - points[0].ts) : 0;
  const withDate = span > 3 * 3600 * 1000;
  for (let i = 0; i < points.length; i += step) {
    const p = points[i];
    const d = new Date(p.ts);
    const pad = n => String(n).padStart(2, '0');
    labels.push(withDate
      ? `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
      : `${pad(d.getHours())}:${pad(d.getMinutes())}`);
    hourData.push(p.hour);
    dayData.push(p.day);
  }
  const statusNote = points.length < 2
    ? '正在积累采样数据（每分钟 1 点），稍候即可看到 hour / day 两条趋势'
    : `${points.length} 个采样点 · 每分钟采样 · hour=近1小时窗口 · day=近1天窗口`;
  wrap.innerHTML = `
    <div class="tiny" style="margin-top:14px">cache_hit_ratio 趋势（${esc(s.device || devName)}）</div>
    <div class="tiny" style="margin:2px 0 6px;color:var(--ink-faint)">${esc(statusNote)}</div>
    <div style="position:relative;height:210px"><canvas id="bcache-trend-canvas"></canvas></div>`;
  body.appendChild(wrap);
  state.bcacheTrendDevice = devName;
  if (!state.bcacheTrendTimer) {
    state.bcacheTrendTimer = setInterval(() => {
      const view = $('#view-storage');
      if (!state.bcacheTrendDevice || (view && view.classList.contains('hidden'))) return;
      renderBcacheTrend(state.bcacheTrendDevice);
    }, 60000);
  }
  const ctx = document.getElementById('bcache-trend-canvas');
  _bcacheChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'hour · cache_hit_ratio (%)',
          data: hourData,
          borderColor: '#35c48b',
          backgroundColor: '#35c48b',
          tension: 0.3,
          borderWidth: 2,
          pointRadius: points.length < 30 ? 2 : 0,
          pointHitRadius: 6,
        },
        {
          label: 'day · cache_hit_ratio (%)',
          data: dayData,
          borderColor: '#4f8cff',
          backgroundColor: '#4f8cff',
          tension: 0.3,
          borderWidth: 2,
          pointRadius: points.length < 30 ? 2 : 0,
          pointHitRadius: 6,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600 },
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: {
          min: 0,
          max: 100,
          title: { display: true, text: 'cache_hit_ratio %' },
          ticks: { color: '#7f8b9a' },
          grid: { color: 'rgba(127,139,154,.14)' },
        },
        x: {
          ticks: { color: '#7f8b9a', maxTicksLimit: 10 },
          grid: { display: false },
        },
      },
      plugins: {
        legend: { labels: { color: '#9aa5b2' } },
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y}%`,
          },
        },
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
  const unreg = !!cset.cache_present_unregistered;
  const avail = Number(cset.cache_available_percent);
  const used = !unreg && avail >= 0 ? (100 - avail) : null;
  const availText = unreg ? '未注册' : `${avail}%`;
  const uuid = cset.uuid || '';
  const wrap = document.createElement('div');
  wrap.id = 'bcache-topology-wrap';
  wrap.innerHTML = `
    <div class="tiny" style="margin-top:14px">bcache 拓扑</div>
    <div class="bc-topo-line">
      <div class="bc-node"><span class="bc-node-k">缓存盘</span><span class="bc-node-v mono">${esc(cacheDev)}</span><span class="bc-node-s">discard ${esc(cacheInfo.discard || '—')}</span></div>
      <span class="bc-link">→</span>
      <div class="bc-node bc-node-set"><span class="bc-node-k">缓存集</span><span class="bc-node-v mono">${esc((uuid || '').slice(0, 8))}…</span><span class="bc-node-s">可用 ${esc(availText)}${used != null ? ` · 占用 ${used}%` : ''}</span></div>
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
      <span class="tiny">SSD 服务率骤降</span><input class="input" id="bc-drop" type="number" min="1" max="100" value="${esc(c.hit_drop_points)}" style="width:72px"><span class="tiny">点</span>
      <span class="tiny">积压阈值(MB)</span><input class="input" id="bc-backlog" type="number" min="1" value="${Math.round(Number(c.backlog_bytes) / 1048576)}" style="width:84px">
      <button class="btn sm primary" id="btn-bc-alert-save">保存</button>
    </div>` : `<div class="tiny">巡检间隔 60 秒，告警会走系统邮件/Webhook。当前阈值：可用率 < ${esc(c.cache_available_warn)}% · 脏数据滞留 ${esc(c.dirty_stuck_minutes)} 分钟 · SSD 服务率骤降 ≥ ${esc(c.hit_drop_points)} 点 · 积压 > ${(Number(c.backlog_bytes) / 1048576).toFixed(0)}MB</div>`}`;
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
    const ctrls = state.status ? ctrlListOf(state.status) : [];
    const cid = ctrls.length ? Number((state.status.controller || {}).ctrl != null ? state.status.controller.ctrl : ctrls[0].ctrl) : 0;
    const d = await api('/api/controller_events?ctrl=' + cid + '&lines=' + state.ctlLines + (q ? '&q=' + q : ''));
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
