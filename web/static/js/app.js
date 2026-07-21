/**
 * LSI RAID Monitor Web UI — Vue 3 + Element Plus
 */

const { createApp, reactive, computed, onMounted, onUnmounted, watch, nextTick, toRefs } = Vue;
const { ElMessage, ElMessageBox } = ElementPlus;
const { Sunny, Moon, RefreshRight, Download } = ElementPlusIconsVue;

const STATUS_INTERVAL = 5000;
const CHART_COLORS = [
  '#4f46e5', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444',
  '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16', '#f97316'
];

const state = reactive({
  status: {},
  events: [],
  history: { datasets: [] },
  currentHistoryHours: 24,
  collecting: false,
  collectionInterval: 30,
  collectionConfigSaving: false,
  isDark: false,
  diskOperations: [],
  // 磁盘操作后待确认的状态覆盖：CSV 采集追上之前，轮询不把它刷回旧值
  pendingDiskStates: {},
  dialogVisible: false,
  actionLoading: false,
  pendingAction: null,
  refreshTimer: null,
  detailDrawerVisible: false,
  selectedDisk: null,
  eventPage: 1,
  eventPageSize: 50,
  eventFilter: 'all',
  eventTotal: 0,
  locale: ElementPlusLocaleZhCn,
  alertConfig: { enabled: false, recipients: [], sendmail_path: '/usr/sbin/sendmail', sendmail_available: false, temp_warn: 45, temp_crit: 50, locked: {} },
  alertConfigForm: { alert_email_to: '', sendmail_path: '/usr/sbin/sendmail', temp_warn: 45, temp_crit: 50 },
  alertTesting: false,
  alertSaving: false,
  smartLoading: false,
  smartOutput: '',
  smartError: '',
  authRequired: false,
  loggedIn: true,
  username: '',
  userRole: '',
  activeTab: 'overview',
  loginForm: { username: 'admin', password: '' },
  loginError: '',
  loginLoading: false,
  users: [],
  usersLoading: false,
  userSaving: false,
  newUser: { username: '', password: '', role: 'viewer' },
  resetDialog: { visible: false, username: '', password: '', loading: false }
});

// 供 storage.js 等独立组件读取当前登录角色
window.LSI_STATE = state;

let tempChart = null;

// 401 统一处理：启用口令认证后，任何受保护接口返回 401 都回到登录遮罩
const _rawFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _rawFetch(...args);
  if (res.status === 401 && state.authRequired) {
    state.loggedIn = false;
  }
  return res;
};

// ---- 认证 ----

async function fetchAuthStatus() {
  try {
    const res = await _rawFetch('/api/auth/status');
    if (!res.ok) return;
    const data = await res.json();
    state.authRequired = !!data.auth_required;
    state.loggedIn = !!data.logged_in;
    state.username = data.username || '';
    state.userRole = data.role || '';
  } catch (err) { /* 忽略，默认免登录 */ }
}

async function doLogin() {
  state.loginLoading = true;
  state.loginError = '';
  try {
    const res = await _rawFetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: state.loginForm.username, password: state.loginForm.password })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || '登录失败');
    state.loggedIn = true;
    state.username = data.username || state.loginForm.username;
    state.userRole = data.role || '';
    state.loginForm.password = '';
    loadUsers();
    refresh();
  } catch (err) {
    state.loginError = err.message;
  } finally {
    state.loginLoading = false;
  }
}

async function doLogout() {
  try { await _rawFetch('/api/logout', { method: 'POST' }); } catch (err) { /* 忽略 */ }
  state.loggedIn = false;
  state.username = '';
  state.userRole = '';
}

// ---- 用户管理（仅管理员） ----

async function loadUsers() {
  state.usersLoading = true;
  try {
    const res = await fetch('/api/users');
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    state.users = data.users || [];
  } catch (err) {
    state.users = [];
    console.error('加载用户列表失败:', err);
  } finally {
    state.usersLoading = false;
  }
}

async function createUser() {
  if (!state.newUser.username || !state.newUser.password) {
    ElMessage.warning('请填写用户名和口令');
    return;
  }
  state.userSaving = true;
  try {
    const res = await fetch('/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state.newUser)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    ElMessage.success('用户已创建: ' + state.newUser.username);
    state.newUser = { username: '', password: '', role: 'viewer' };
    // 创建首个管理员后认证随即启用，刷新登录状态
    await fetchAuthStatus();
    await loadUsers();
  } catch (err) {
    ElMessage.error('创建用户失败: ' + err.message);
  } finally {
    state.userSaving = false;
  }
}

function openResetPassword(row) {
  state.resetDialog = { visible: true, username: row.username, password: '', loading: false };
}

async function doResetPassword() {
  if (!state.resetDialog.password) {
    ElMessage.warning('请输入新口令');
    return;
  }
  state.resetDialog.loading = true;
  try {
    const res = await fetch('/api/users/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: state.resetDialog.username, password: state.resetDialog.password })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    ElMessage.success('口令已重置: ' + state.resetDialog.username);
    state.resetDialog.visible = false;
  } catch (err) {
    ElMessage.error('重置口令失败: ' + err.message);
  } finally {
    state.resetDialog.loading = false;
  }
}

function confirmDeleteUser(row) {
  ElMessageBox.confirm(
    `确定要删除用户「${row.username}」吗？`,
    '删除用户',
    { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
  ).then(async () => {
    try {
      const res = await fetch('/api/users/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: row.username })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      ElMessage.success('已删除用户: ' + row.username);
      await fetchAuthStatus();
      await loadUsers();
    } catch (err) {
      ElMessage.error('删除用户失败: ' + err.message);
    }
  }).catch(() => {});
}

// ---- helpers ----

function stateClass(s) {
  const str = String(s || '').toLowerCase();
  if (['optimal', 'optl', 'opt', 'onln', 'online', 'active'].includes(str)) return 'text-emerald-600 dark:text-emerald-400';
  if (['degraded', 'warn', 'warning', 'hotspare'].includes(str)) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

function tempClass(v) {
  if (v === null || v === undefined || v === '') return '';
  const warn = state.status.thresholds?.warn || 45;
  const crit = state.status.thresholds?.crit || 50;
  if (v >= crit) return 'text-red-600 dark:text-red-400 font-bold';
  if (v >= warn) return 'text-amber-600 dark:text-amber-400 font-bold';
  return '';
}

function healthColor(score) {
  if (score >= 90) return '#10b981';
  if (score >= 75) return '#3b82f6';
  if (score >= 60) return '#f59e0b';
  return '#ef4444';
}

function healthLevelText(level) {
  const map = { excellent: '优秀', good: '良好', warning: '警告', critical: '危险' };
  return map[level] || level || '—';
}

function levelText(level) {
  const map = { info: '信息', warning: '警告', error: '错误' };
  return map[level] || level;
}

function pohText(poh) {
  if (!poh && poh !== 0) return '—';
  if (poh <= 0) return '—';
  return `${poh}h (${Math.floor(poh / 24)}d)`;
}

function countersHtml(row, keys, labels) {
  return keys.map((k, i) => {
    const v = row[k] || 0;
    return v > 0
      ? `<span class="text-red-600 dark:text-red-400 font-semibold">${labels[i]}:${v}</span>`
      : `${labels[i]}:0`;
  }).join(' ');
}

function diskSlotClass(d) {
  const str = String(d.state || '').toLowerCase();
  if (['optimal', 'optl', 'opt', 'onln', 'online', 'active'].includes(str)) {
    return 'disk-slot-ok';
  }
  if (['degraded', 'warn', 'warning', 'hotspare'].includes(str)) {
    return 'disk-slot-warn';
  }
  return 'disk-slot-error';
}

function showDiskDetail(row) {
  state.selectedDisk = row;
  state.smartOutput = '';
  state.smartError = '';
  state.detailDrawerVisible = true;
}

async function loadDiskSmart() {
  if (!state.selectedDisk) return;
  state.smartLoading = true;
  state.smartError = '';
  try {
    const result = await fetchDiskSmart(state.selectedDisk.eid, state.selectedDisk.slot);
    if (result.success) {
      state.smartOutput = result.stdout || '(无输出)';
    } else {
      state.smartError = result.error || '获取 SMART 失败';
      state.smartOutput = result.stdout || '';
    }
  } catch (err) {
    state.smartError = err.message;
    state.smartOutput = '';
  } finally {
    state.smartLoading = false;
  }
}

async function copySmartOutput() {
  if (!state.smartOutput) return;
  try {
    await navigator.clipboard.writeText(state.smartOutput);
    ElMessage.success('SMART 输出已复制到剪贴板');
  } catch (err) {
    ElMessage.error('复制失败: ' + err.message);
  }
}

// ---- API ----

async function fetchStatus() {
  const res = await fetch('/api/status');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchHistory(hours = 24) {
  const res = await fetch(`/api/history?hours=${hours}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchEvents(limit = 50, offset = 0, level = '') {
  let url = `/api/events?limit=${limit}&offset=${offset}`;
  if (level && level !== 'all') url += `&level=${encodeURIComponent(level)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function triggerCollect() {
  const res = await fetch('/api/collect', { method: 'POST' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchCollectionConfig() {
  const res = await fetch('/api/collection/config');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function updateCollectionConfig(intervalMinutes) {
  const res = await fetch('/api/collection/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ interval_minutes: intervalMinutes })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function operateDisk(eid, slot, action) {
  const res = await fetch(`/api/disk/${eid}/${slot}/operate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchAlertConfig() {
  const res = await fetch('/api/alert/config');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function updateAlertConfig(config) {
  const res = await fetch('/api/alert/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config)
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return res.json();
}

async function fetchDiskSmart(eid, slot) {
  const res = await fetch(`/api/disk/${eid}/${slot}/smart`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function triggerAlertTest() {
  const res = await fetch('/api/alert/test', { method: 'POST' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return res.json();
}

async function loadDiskOperations() {
  const res = await fetch('/api/disk/operations');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  state.diskOperations = data.actions || [];
}

// ---- chart ----

function chartGridColor() {
  return state.isDark ? '#334155' : '#e2e8f0';
}

function chartTickColor() {
  return state.isDark ? '#94a3b8' : '#64748b';
}

function renderChart() {
  const canvas = document.getElementById('temp-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const datasets = (state.history.datasets || []).map((ds, idx) => ({
    label: ds.label,
    data: ds.data.map(p => ({ x: p.x, y: p.y })),
    borderColor: CHART_COLORS[idx % CHART_COLORS.length],
    backgroundColor: CHART_COLORS[idx % CHART_COLORS.length],
    tension: 0.2,
    pointRadius: 2,
    pointHoverRadius: 5,
    borderWidth: 1.5,
  }));

  const thresholds = state.status.thresholds || { warn: 45, crit: 50 };

  const config = {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          labels: { usePointStyle: true, boxWidth: 8, color: chartTickColor() }
        },
        tooltip: { callbacks: { title: (items) => items[0]?.parsed.x || '' } }
      },
      scales: {
        x: {
          type: 'category',
          ticks: { maxTicksLimit: 12, maxRotation: 0, color: chartTickColor() },
          grid: { color: chartGridColor() }
        },
        y: {
          beginAtZero: false,
          suggestedMin: 20,
          title: { display: true, text: '温度 (°C)', color: chartTickColor() },
          ticks: { color: chartTickColor() },
          grid: { color: chartGridColor() }
        }
      },
      animation: { duration: 0 }
    },
    plugins: [{
      id: 'thresholdLines',
      beforeDraw: (chart) => {
        const { ctx, chartArea: { top, bottom, left, right }, scales: { y } } = chart;
        [thresholds.warn, thresholds.crit].forEach((val, i) => {
          const yPos = y.getPixelForValue(val);
          if (yPos < top || yPos > bottom) return;
          ctx.save();
          ctx.strokeStyle = i === 0 ? '#f59e0b' : '#ef4444';
          ctx.lineWidth = 1;
          ctx.setLineDash([5, 5]);
          ctx.beginPath();
          ctx.moveTo(left, yPos);
          ctx.lineTo(right, yPos);
          ctx.stroke();
          ctx.fillStyle = ctx.strokeStyle;
          ctx.font = '10px sans-serif';
          ctx.fillText(i === 0 ? `警告 ${val}°C` : `临界 ${val}°C`, right - 60, yPos - 4);
          ctx.restore();
        });
      }
    }]
  };

  if (tempChart) {
    tempChart.destroy();
  }
  tempChart = new Chart(ctx, config);
}

// ---- actions ----

// 把操作后回读到的磁盘状态覆盖到表格上（90 秒内有效，等 CSV 采集追上）
function applyPendingDiskStates() {
  const now = Date.now();
  const disks = state.status.physical_disks || [];
  for (const key of Object.keys(state.pendingDiskStates)) {
    const pending = state.pendingDiskStates[key];
    if (now > pending.expires) {
      delete state.pendingDiskStates[key];
      continue;
    }
    const row = disks.find(d => d.label === key);
    if (row && row.state !== pending.state) row.state = pending.state;
  }
}

async function refresh() {
  try {
    const offset = (state.eventPage - 1) * state.eventPageSize;
    const [status, history, events] = await Promise.all([
      fetchStatus(),
      fetchHistory(state.currentHistoryHours),
      fetchEvents(state.eventPageSize, offset, state.eventFilter)
    ]);
    state.status = status;
    applyPendingDiskStates();
    state.history = history;
    state.events = events.events || [];
    state.eventTotal = events.total || 0;
    nextTick(() => renderChart());
  } catch (err) {
    console.error('刷新失败:', err);
    ElMessage.error('刷新失败: ' + err.message);
  }
}

function startPolling() {
  refresh();
  state.refreshTimer = setInterval(refresh, STATUS_INTERVAL);
}

function stopPolling() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

async function collectNow() {
  state.collecting = true;
  try {
    const result = await triggerCollect();
    if (result.success) {
      ElMessage.success('采集完成');
      await refresh();
    } else if (result.busy) {
      ElMessage.warning('采集任务正在运行');
    } else {
      ElMessage.error('采集失败: ' + (result.stderr || result.error || '未知'));
    }
  } catch (err) {
    ElMessage.error('采集请求失败: ' + err.message);
  } finally {
    state.collecting = false;
  }
}

async function saveCollectionConfig() {
  state.collectionConfigSaving = true;
  try {
    const result = await updateCollectionConfig(state.collectionInterval);
    state.collectionInterval = result.interval_minutes;
    ElMessage.success(`自动采集周期已设为 ${result.interval_minutes === 60 ? '1 小时' : result.interval_minutes + ' 分钟'}`);
  } catch (err) {
    ElMessage.error('保存采集周期失败: ' + err.message);
  } finally {
    state.collectionConfigSaving = false;
  }
}

function exportCsv() {
  window.open(`/api/export/csv?hours=${state.currentHistoryHours}`, '_blank');
}

async function testAlert() {
  state.alertTesting = true;
  try {
    const result = await triggerAlertTest();
    if (result.success) {
      ElMessage.success('测试报警邮件已发送');
    } else {
      ElMessage.error('测试报警失败: ' + (result.error || '未知'));
    }
  } catch (err) {
    ElMessage.error('测试报警失败: ' + err.message);
  } finally {
    state.alertTesting = false;
  }
}

async function saveAlertConfig() {
  state.alertSaving = true;
  try {
    const result = await updateAlertConfig(state.alertConfigForm);
    if (result.success && result.config) {
      state.alertConfig = result.config;
      syncAlertConfigForm();
      ElMessage.success('报警配置已保存');
    } else {
      ElMessage.error('保存失败: ' + (result.error || '未知'));
    }
  } catch (err) {
    ElMessage.error('保存失败: ' + err.message);
  } finally {
    state.alertSaving = false;
  }
}

function syncAlertConfigForm() {
  state.alertConfigForm = {
    alert_email_to: state.alertConfig.recipients.join(', '),
    sendmail_path: state.alertConfig.sendmail_path,
    temp_warn: state.alertConfig.temp_warn,
    temp_crit: state.alertConfig.temp_crit
  };
}

function onHistoryRangeChange() {
  fetchHistory(state.currentHistoryHours)
    .then(data => {
      state.history = data;
      nextTick(() => renderChart());
    })
    .catch(err => ElMessage.error('历史数据刷新失败: ' + err.message));
}

function onEventPageChange(page) {
  state.eventPage = page;
  refresh();
}

function onEventFilterChange(filter) {
  state.eventFilter = filter;
  state.eventPage = 1;
  refresh();
}

function onDiskAction(row, action) {
  const op = state.diskOperations.find(o => o.key === action);
  if (!op) return;
  row._action = '';
  state.pendingAction = { row, action, op, label: row.label };
  state.dialogVisible = true;
}

async function confirmDiskAction() {
  const { row, action, op, label } = state.pendingAction;
  state.actionLoading = true;
  try {
    const result = await operateDisk(row.eid, row.slot, action);
    if (result.success) {
      // 立即把表格中该盘状态更新为 storcli 回读的真实值，不等下次采集；
      // 90 秒内的轮询也不会把它刷回旧值，直到 CSV 采集追上
      if (result.current_state) {
        row.state = result.current_state;
        state.pendingDiskStates[label] = { state: result.current_state, expires: Date.now() + 90000 };
      }
      ElMessage.success(`磁盘 ${label} 操作成功` + (result.current_state ? `，当前状态: ${result.current_state}` : ''));
      // 后台采集完成后再从 CSV 刷新全量数据（事件、温度等）
      setTimeout(refresh, 8000);
      setTimeout(refresh, 20000);
    } else {
      ElMessage.error(`磁盘 ${label} 操作失败: ${result.stderr || result.error || '未知错误'}`);
    }
  } catch (err) {
    ElMessage.error(`磁盘 ${label} 操作请求失败: ${err.message}`);
  } finally {
    state.actionLoading = false;
    state.dialogVisible = false;
    state.pendingAction = null;
  }
}

// ---- theme ----

function initTheme() {
  const stored = localStorage.getItem('theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  state.isDark = stored === 'dark' || (!stored && prefersDark);
  applyTheme();
}

function applyTheme() {
  if (state.isDark) {
    document.documentElement.classList.add('dark');
  } else {
    document.documentElement.classList.remove('dark');
  }
  localStorage.setItem('theme', state.isDark ? 'dark' : 'light');
}

function toggleTheme() {
  applyTheme();
  nextTick(() => {
    if (tempChart) renderChart();
  });
}

// ---- computed ----

const app = createApp({
  setup() {
    const statusType = computed(() => {
      const s = String(state.status.status || '').toUpperCase();
      if (s === 'OK') return 'success';
      if (s === 'WARN') return 'warning';
      if (s === 'ERROR') return 'danger';
      return 'info';
    });

    const statusText = computed(() => {
      const s = String(state.status.status || '').toUpperCase();
      if (s === 'OK') return '正常';
      if (s === 'WARN') return '警告';
      if (s === 'ERROR') return '错误';
      return '无数据';
    });

    const healthScore = computed(() => state.status.health_score?.overall ?? '—');
    const healthLevel = computed(() => state.status.health_score?.level || 'critical');
    const healthType = computed(() => {
      const lv = healthLevel.value;
      if (lv === 'excellent') return 'success';
      if (lv === 'good') return 'primary';
      if (lv === 'warning') return 'warning';
      return 'danger';
    });
    const healthDetails = computed(() => state.status.health_score?.details || {});
    const healthRingOffset = computed(() => {
      const score = state.status.health_score?.overall || 0;
      const circumference = 2 * Math.PI * 42;
      return circumference - (score / 100) * circumference;
    });

    const controllerInfo = computed(() => {
      const c = state.status.controller || {};
      return [c.model, c.fw].filter(Boolean).join(' / ') || '—';
    });

    const bbuInfo = computed(() => {
      const c = state.status.controller || {};
      if (!c.bbu_model) return '无 BBU 数据';
      return `${c.bbu_model} / ${c.bbu_temperature !== null ? c.bbu_temperature + '°C' : '—'}`;
    });

    const tempOverview = computed(() => {
      const t = state.status.temperature_overview || {};
      if ((t.avg === null || t.avg === undefined) && (t.max === null || t.max === undefined)) return '—';
      const warn = t.warn || 45;
      const crit = t.crit || 50;
      const avgClass = tempClass(t.avg);
      const maxClass = tempClass(t.max);
      return `<span class="${avgClass}">${t.avg !== null && t.avg !== undefined ? t.avg + '°C' : '—'}</span> / <span class="${maxClass}">${t.max !== null && t.max !== undefined ? t.max + '°C' : '—'}</span>`;
    });

    const physicalDiskMap = computed(() => {
      const disks = state.status.physical_disks || [];
      return [...disks].sort((a, b) => {
        const ae = a.eid || 0, be = b.eid || 0;
        return ae === be ? (a.slot || 0) - (b.slot || 0) : ae - be;
      });
    });

    const enclosureId = computed(() => {
      const disks = state.status.physical_disks || [];
      return disks.length > 0 ? disks[0].eid : '';
    });

    const healthLevelLabel = computed(() => healthLevelText(healthLevel.value));

    // 未启用认证时视为管理员（无限制）；启用后按登录角色判断
    const isAdmin = computed(() => !state.authRequired || state.userRole === 'admin');

    onMounted(() => {
      initTheme();
      fetchAuthStatus();
      loadDiskOperations();
      loadUsers();
      fetchAlertConfig().then(cfg => {
        state.alertConfig = cfg;
        syncAlertConfigForm();
      }).catch(() => {});
      fetchCollectionConfig().then(cfg => {
        state.collectionInterval = cfg.interval_minutes;
      }).catch(() => {});
      startPolling();
    });

    onUnmounted(() => {
      stopPolling();
    });

    return {
      ...toRefs(state),
      isAdmin,
      statusType,
      statusText,
      healthScore,
      healthLevelLabel,
      healthType,
      healthDetails,
      healthRingOffset,
      controllerInfo,
      bbuInfo,
      tempOverview,
      physicalDiskMap,
      enclosureId,
      stateClass,
      tempClass,
      healthColor,
      levelText,
      pohText,
      healthItemColor: healthColor,
      diskSlotClass,
      showDiskDetail,
      loadDiskSmart,
      copySmartOutput,
      storCounters: (row) => countersHtml(row, ['media_error', 'other_error', 'predictive_failure'], ['ME', 'OE', 'PF']),
      smartCounters: (row) => countersHtml(row, ['reallocated', 'pending', 'uncorrectable'], ['R', 'P', 'U']),
      collectNow,
      saveCollectionConfig,
      exportCsv,
      testAlert,
      saveAlertConfig,
      syncAlertConfigForm,
      toggleTheme,
      doLogin,
      doLogout,
      loadUsers,
      createUser,
      openResetPassword,
      doResetPassword,
      confirmDeleteUser,
      onHistoryRangeChange,
      onEventPageChange,
      onEventFilterChange,
      onDiskAction,
      confirmDiskAction,
      Sunny,
      Moon,
      RefreshRight,
      Download
    };
  }
});

app.use(ElementPlus, { locale: ElementPlusLocaleZhCn, zIndex: 3000 });

// 注册图标
const USED_ICONS = { Sunny, Moon, RefreshRight, Download };
for (const [key, component] of Object.entries(USED_ICONS)) {
  app.component(key, component);
}

// 注册磁盘管理面板（storage.js 中定义的全局组件）
if (window.StoragePanel) app.component('storage-panel', window.StoragePanel);

app.mount('#app');
