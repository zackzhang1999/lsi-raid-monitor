/**
 * 磁盘管理面板 — 全局组件 window.StoragePanel
 * 依赖全局 Vue / ElementPlus（vue.global.js 含模板编译器，可直接用 template 字符串）
 * 数据接口：/api/storage/*
 * 在 app.js 之前加载，由 app.js 通过 app.component('storage-panel', window.StoragePanel) 注册
 */
(function () {
  const { ElMessage, ElMessageBox } = ElementPlus;

  // ---- 本地小工具（与 app.js 同款 fetch 包装风格：!res.ok → throw → json）----
  async function api(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.success === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  function post(url, body) {
    return api(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  function formatBytes(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    if (n === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
    return (n / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  }

  const CATEGORY_TAGS = {
    'raid-member': { label: 'RAID', type: 'warning' },
    'raid-vd': { label: 'RAID VD', type: 'warning' },
    'jbod': { label: 'JBOD', type: 'info' },
    'nvme': { label: 'NVMe', type: 'success' },
    'direct-sata': { label: '直连', type: 'primary' }
  };

  window.StoragePanel = {
    name: 'StoragePanel',
    template: `
<div class="storage-panel">
  <div class="fm-toolbar">
    <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
    <span class="text-xs text-slate-400">每 10 秒自动刷新；格式化等高危操作需输入设备名确认</span>
  </div>

  <el-table
    :data="treeData"
    row-key="name"
    :tree-props="{ children: 'children' }"
    v-loading="loading"
    border
    stripe
    size="small"
    default-expand-all
    empty-text="未检测到磁盘"
  >
    <el-table-column label="设备" min-width="200">
      <template #default="scope">
        <span class="font-mono">{{ scope.row.path || scope.row.name }}</span>
        <el-tag
          v-if="categoryTag(scope.row)"
          :type="categoryTag(scope.row).type"
          size="small"
          effect="plain"
          class="ml-1"
        >{{ categoryTag(scope.row).label }}</el-tag>
        <el-tag v-if="rowIsSystem(scope.row)" type="danger" size="small" effect="plain" class="ml-1">系统盘</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="型号 / 序列号" min-width="180">
      <template #default="scope">
        <div>{{ scope.row.model || '—' }}</div>
        <div v-if="scope.row.serial" class="text-xs text-slate-400">{{ scope.row.serial }}</div>
      </template>
    </el-table-column>
    <el-table-column label="容量" min-width="90">
      <template #default="scope">{{ formatBytes(scope.row.size_bytes) }}</template>
    </el-table-column>
    <el-table-column label="文件系统 / 标签" min-width="120">
      <template #default="scope">
        <span v-if="scope.row.fstype">
          {{ scope.row.fstype }}
          <span v-if="scope.row.label" class="text-xs text-slate-400">({{ scope.row.label }})</span>
        </span>
        <span v-else>—</span>
      </template>
    </el-table-column>
    <el-table-column label="挂载点" min-width="140" show-overflow-tooltip>
      <template #default="scope">
        {{ (scope.row.mountpoints && scope.row.mountpoints.length) ? scope.row.mountpoints.join(', ') : '—' }}
      </template>
    </el-table-column>
    <el-table-column label="用量" min-width="140">
      <template #default="scope">
        <el-progress
          v-if="scope.row.usage"
          :percentage="Math.round(scope.row.usage.percent || 0)"
          :stroke-width="10"
          :status="scope.row.usage.percent >= 90 ? 'exception' : ''"
        ></el-progress>
        <span v-else>—</span>
      </template>
    </el-table-column>
    <el-table-column label="SMART" min-width="110">
      <template #default="scope">
        <span v-if="scope.row.smart_health">
          <span v-if="scope.row.smart_health.healthy === true" class="text-emerald-600">正常</span>
          <span v-else-if="scope.row.smart_health.healthy === false" class="text-red-600 font-bold">异常</span>
          <span v-else>—</span>
          <span v-if="scope.row.smart_health.temperature !== null && scope.row.smart_health.temperature !== undefined">
            / {{ scope.row.smart_health.temperature }}°C
          </span>
        </span>
        <span v-else>—</span>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="250" fixed="right">
      <template #default="scope">
        <el-button
          v-if="canMount(scope.row)"
          type="primary"
          link
          size="small"
          @click="openMount(scope.row)"
        >挂载</el-button>
        <el-button
          v-if="canUmount(scope.row)"
          type="warning"
          link
          size="small"
          @click="confirmUmount(scope.row)"
        >卸载</el-button>
        <el-button
          v-if="canFormat(scope.row)"
          type="danger"
          link
          size="small"
          @click="openFormat(scope.row)"
        >格式化</el-button>
        <el-button type="info" link size="small" @click="openSmart(scope.row)">SMART</el-button>
      </template>
    </el-table-column>
  </el-table>

  <!-- 挂载对话框 -->
  <el-dialog v-model="mountDialog.visible" title="挂载分区" width="420px" align-center>
    <p class="mb-3">设备：<strong class="font-mono">{{ mountDialog.device }}</strong></p>
    <el-input
      v-model="mountDialog.mountpoint"
      placeholder="挂载点（留空由系统自动分配）"
      @keyup.enter="doMount"
    ></el-input>
    <template #footer>
      <el-button @click="mountDialog.visible = false">取消</el-button>
      <el-button type="primary" :loading="mountDialog.loading" @click="doMount">挂载</el-button>
    </template>
  </el-dialog>

  <!-- 格式化对话框 -->
  <el-dialog v-model="formatDialog.visible" title="格式化设备" width="480px" align-center>
    <el-alert
      type="error"
      show-icon
      :closable="false"
      title="危险操作：格式化将清除该设备上的全部数据，且不可恢复！"
      class="mb-4"
    ></el-alert>
    <el-form label-width="90px">
      <el-form-item label="设备">
        <strong class="font-mono">{{ formatDialog.device }}</strong>
      </el-form-item>
      <el-form-item label="文件系统">
        <el-radio-group v-model="formatDialog.fstype">
          <el-radio-button label="ext4">ext4</el-radio-button>
          <el-radio-button label="xfs">xfs</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="卷标">
        <el-input v-model="formatDialog.label" placeholder="可选"></el-input>
      </el-form-item>
      <el-form-item label="确认输入">
        <el-input
          v-model="formatDialog.confirmName"
          :placeholder="'请输入 ' + formatDialog.name + ' 以确认'"
        ></el-input>
        <p class="text-xs text-red-600 mt-1">请输入完整设备名「{{ formatDialog.name }}」确认格式化</p>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="formatDialog.visible = false">取消</el-button>
      <el-button
        type="danger"
        :disabled="formatDialog.confirmName !== formatDialog.name"
        :loading="formatDialog.loading"
        @click="doFormat"
      >确认格式化</el-button>
    </template>
  </el-dialog>

  <!-- SMART 原文抽屉 -->
  <el-drawer
    v-model="smartDrawer.visible"
    :title="'SMART 信息 — ' + smartDrawer.name"
    size="70%"
    destroy-on-close
  >
    <el-alert
      v-if="smartDrawer.error"
      :title="smartDrawer.error"
      type="error"
      show-icon
      :closable="false"
      class="mb-3"
    ></el-alert>
    <div v-loading="smartDrawer.loading">
      <div v-if="smartDrawer.output" class="smart-output">
        <pre class="smart-pre">{{ smartDrawer.output }}</pre>
      </div>
      <el-empty v-else-if="!smartDrawer.loading && !smartDrawer.error" description="无 SMART 数据"></el-empty>
    </div>
  </el-drawer>
</div>
`,
    data() {
      return {
        disks: [],
        loading: false,
        timer: null,
        mountDialog: { visible: false, device: '', name: '', mountpoint: '', loading: false },
        formatDialog: { visible: false, device: '', name: '', fstype: 'ext4', label: '', confirmName: '', loading: false },
        smartDrawer: { visible: false, name: '', output: '', loading: false, error: '' }
      };
    },
    computed: {
      // 给分区行补充父盘信息（类别 / 系统盘标记 / SMART 目标盘名），供操作按钮判断
      treeData() {
        return (this.disks || []).map(d => ({
          ...d,
          children: (d.children || []).map(c => ({
            ...c,
            _parentCategory: d.category,
            _parentSystem: d.is_system,
            _smartName: d.name
          }))
        }));
      }
    },
    methods: {
      formatBytes,
      // 危险操作（挂载/卸载/格式化）仅管理员可见；未启用认证时无限制
      isAdmin() {
        const s = window.LSI_STATE;
        return !s || !s.authRequired || s.userRole === 'admin';
      },
      isPartition(row) {
        return row._smartName !== undefined;
      },
      rowIsSystem(row) {
        return !!(row.is_system || row._parentSystem);
      },
      rowCategory(row) {
        return row.category || row._parentCategory || '';
      },
      rowIsMounted(row) {
        return (row.mountpoints || []).length > 0;
      },
      categoryTag(row) {
        if (this.isPartition(row)) return null;
        return CATEGORY_TAGS[this.rowCategory(row)] || null;
      },
      canMount(row) {
        return this.isAdmin() && this.isPartition(row) && !this.rowIsMounted(row);
      },
      canUmount(row) {
        return this.isAdmin() && this.rowIsMounted(row) && !this.rowIsSystem(row);
      },
      canFormat(row) {
        return this.isAdmin() && !!row.path && !this.rowIsSystem(row) && !this.rowIsMounted(row) && this.rowCategory(row) !== 'raid-member';
      },
      async load(showError) {
        if (this.disks.length === 0) this.loading = true;
        try {
          const data = await api('/api/storage/disks');
          this.disks = data.disks || [];
        } catch (err) {
          if (showError) ElMessage.error('加载磁盘列表失败: ' + err.message);
          else console.error('磁盘列表轮询失败:', err);
        } finally {
          this.loading = false;
        }
      },
      openMount(row) {
        this.mountDialog = { visible: true, device: row.path, name: row.name, mountpoint: '', loading: false };
      },
      async doMount() {
        const body = { device: this.mountDialog.device };
        const mp = (this.mountDialog.mountpoint || '').trim();
        if (mp) body.mountpoint = mp;
        this.mountDialog.loading = true;
        try {
          const data = await post('/api/storage/mount', body);
          ElMessage.success('已挂载到 ' + (data.mountpoint || mp || '系统默认位置'));
          this.mountDialog.visible = false;
          this.load(true);
        } catch (err) {
          ElMessage.error('挂载失败: ' + err.message);
        } finally {
          this.mountDialog.loading = false;
        }
      },
      confirmUmount(row) {
        ElMessageBox.confirm(
          `确定要卸载 ${row.path}（挂载于 ${(row.mountpoints || []).join(', ')}）吗？`,
          '卸载确认',
          { confirmButtonText: '卸载', cancelButtonText: '取消', type: 'warning' }
        ).then(async () => {
          try {
            await post('/api/storage/umount', { device: row.path });
            ElMessage.success('已卸载 ' + row.path);
            this.load(true);
          } catch (err) {
            ElMessage.error('卸载失败: ' + err.message);
          }
        }).catch(() => {});
      },
      openFormat(row) {
        this.formatDialog = { visible: true, device: row.path, name: row.name, fstype: 'ext4', label: '', confirmName: '', loading: false };
      },
      async doFormat() {
        const body = {
          device: this.formatDialog.device,
          fstype: this.formatDialog.fstype,
          confirm_name: this.formatDialog.confirmName
        };
        const label = (this.formatDialog.label || '').trim();
        if (label) body.label = label;
        this.formatDialog.loading = true;
        try {
          await post('/api/storage/format', body);
          ElMessage.success('格式化完成: ' + this.formatDialog.device);
          this.formatDialog.visible = false;
          this.load(true);
        } catch (err) {
          ElMessage.error('格式化失败: ' + err.message);
        } finally {
          this.formatDialog.loading = false;
        }
      },
      async openSmart(row) {
        const name = row._smartName || row.name;
        this.smartDrawer = { visible: true, name, output: '', loading: true, error: '' };
        try {
          const data = await api('/api/storage/disk/' + encodeURIComponent(name) + '/smart');
          this.smartDrawer.output = data.output || '(无输出)';
        } catch (err) {
          this.smartDrawer.error = '获取 SMART 失败: ' + err.message;
        } finally {
          this.smartDrawer.loading = false;
        }
      }
    },
    mounted() {
      this.load(true);
      this.timer = setInterval(() => this.load(false), 10000);
    },
    unmounted() {
      if (this.timer) {
        clearInterval(this.timer);
        this.timer = null;
      }
    }
  };
})();
