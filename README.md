# LSI RAID 监控系统 详细说明书

面向 LSI/Broadcom MegaRAID 控制器的存储监控平台：分钟级采集磁盘温度、状态、错误计数、SMART 关键属性，Web 仪表盘实时展示，异常自动邮件报警；并集成存储管理（分区/格式化/挂载/fstab/NFS）与 RAID 维护操作。

UI 基于 google-design 设计体系（DM Sans / JetBrains Mono，浅色主色 `#4285f4`，深色 accent `#fc2c50`），支持明暗主题切换。

<img width="3813" height="1899" alt="001" src="https://github.com/user-attachments/assets/68d031eb-41d7-471b-90de-355d99f2ca3c" />
<img width="3819" height="1824" alt="002" src="https://github.com/user-attachments/assets/1f48f036-62d2-4651-9cdb-343a31b6366e" />

---

## 目录

1. [系统概述](#1-系统概述)
2. [系统架构](#2-系统架构)
3. [功能总览](#3-功能总览)
4. [环境要求](#4-环境要求)
5. [安装部署](#5-安装部署)
6. [快速上手](#6-快速上手)
7. [Web 界面使用指南](#7-web-界面使用指南)
8. [邮件报警详解](#8-邮件报警详解)
9. [数据采集详解](#9-数据采集详解)
10. [数据文件说明](#10-数据文件说明)
11. [健康评分算法](#11-健康评分算法)
12. [磁盘故障预测规则](#12-磁盘故障预测规则)
13. [环境变量参考](#13-环境变量参考)
14. [API 接口一览](#14-api-接口一览)
15. [日常运维](#15-日常运维)
16. [故障排查 FAQ](#16-故障排查-faq)
17. [安全说明](#17-安全说明)

---

## 1. 系统概述

本系统部署在装有 LSI/Broadcom MegaRAID 阵列卡的服务器上，通过官方命令行工具 `storcli64` 与 `smartmontools` 的 `smartctl` 持续采集：

- **物理磁盘**：槽位、型号、序列号、固件、状态（Onln/UGood/JBOD/Failed…）、温度、介质错误/其他错误/预测性故障计数、SMART 告警位、重建（rebuild）/回拷（copyback）/安全擦除（erase）进度；
- **控制器**：型号、固件版本、健康状态、ROC 温度、BBU/CacheVault 状态与温度；
- **虚拟磁盘**：DG/VD、RAID 级别、容量、状态、OS 设备名、写缓存、成员盘清单；
- **SMART 关键属性**：重映射扇区(5)、待定扇区(197)、无法纠正扇区(198)、上报不可纠正(187)、命令超时(188)、通电时长(9)、温度(194)；同时兼容 SAS(SCSI) 盘；
- **NVMe 直连盘**：不经阵列卡的 PCIe/U.2 盘，采集温度、备用空间、寿命占用、介质错误、通电时长等；
- **系统层**：负载、内存、每盘 IO 计数、文件系统容量与 Inode 使用率。

所有数据按日期落盘为 CSV，Web 后端读取后渲染仪表盘；异常情况经 `sendmail` 发送邮件，并写入事件日志。

## 2. 系统架构

```
┌────────────────────────────────────────────────────────────┐
│                        浏览器 (Web UI)                      │
│        原生 JS + Chart.js，60s 刷新状态 / 5s 刷新实时性能      │
└───────────────────────────▲────────────────────────────────┘
                            │ HTTP :5200
┌───────────────────────────┴────────────────────────────────┐
│                     web_server.py (Flask)                   │
│   页面渲染 · REST API · 鉴权会话 · 磁盘预测 · storcli 操作      │
│   ┌──────────────────────────────────────────────┐          │
│   │ 内置采集线程：每分钟对齐触发 lsi_collectd.py     │          │
│   └──────────────┬───────────────────────────────┘          │
└──────────────────┼──────────────────────────────────────────┘
                   │ subprocess
       ┌───────────▼────────────┐      ┌─────────────────────┐
       │ lsi_collectd.py 采集器   │─────▶│ lsi_alert.py 报警模块 │
       │ 文件锁+分钟去重+间隔门控   │      │ 邮件 + events.jsonl  │
       └───────┬────────────────┘      └──────────┬──────────┘
               │ sudo                             │ sendmail
    ┌──────────▼──────────┐             ┌─────────▼─────────┐
    │ storcli64 / smartctl │             │ 本机 MTA → 收件人  │
    │ nvme / procfs        │             └───────────────────┘
    └──────────┬──────────┘
               ▼
     data/YYYY-MM-DD/*.csv （原始数据，页面读取）
```

| 文件 | 职责 |
| --- | --- |
| `web_server.py` | Flask 后端：页面 + 全部 REST API + 内置采集线程 |
| `lsi_collectd.py` | 采集器主程序：解析 storcli/smartctl/nvme 输出并写 CSV |
| `lsi_alert.py` | 报警引擎：策略开关、状态去重、sendmail 发信、事件日志 |
| `user_mgr.py` | 用户体系：PBKDF2 口令哈希、admin/viewer 角色 |
| `storage_mgr.py` | 存储管理：lsblk 枚举、mount/umount/mkfs、NFS exports 管理 |
| `install.sh` | 一键安装：拷贝到 `/opt`、装依赖、注册 systemd 服务 |
| `run.sh` | 生产模式启动脚本（waitress，缺依赖时回退 Flask 内置服务器） |
| `deploy/lsi-raid-web.service` | systemd 单元示例 |
| `web/` | 前端静态资源（templates + css/js/vendor） |

## 3. 功能总览

**监控**
- 综合健康评分（满分 100，5 个子项加权）与 明细健康状态徽标
- 控制器/BBU 状态卡、巡读与一致性检查状态卡
- 磁盘槽位拓扑图，点击弹出磁盘详情抽屉（含 SMART 属性表结构化展示）
- 磁盘温度 / IO 吞吐 / 文件系统 三类历史趋势图，时间窗 6h/24h/72h 可切换
- CPU/内存/负载/运行时间 实时刷新（5 秒），每盘实时读写速率与 IOPS
- NVMe 直连盘独立表格
- 规则化故障预测：正常/关注/警告/高危 四档及判定原因列表
- 控制器事件日志在线查看（100/200/500 行，关键字筛选），alilog 一键打包 zip 下载
- 事件日志中心（分级筛选 + 分页）

**报警**
- 九类报警策略，可在 Web 中逐类开关：温度警告/临界、SMART 告警、预测性故障(PF)、控制器健康、BBU 异常、磁盘状态变化、VD 状态变化、SMART 关键属性增长
- 同一故障只在发生时告一次（恢复后重新出现会再次告警），避免邮件轰炸
- Web 配置收件人、sendmail 路径、温度阈值；可被环境变量锁定防篡改
- 测试邮件一键发送

**存储管理（管理员）**
- 文件系统使用率一览（容量/已用/Inode），可按挂载点隐藏分区（全局生效）
- 块设备 挂载/卸载/格式化（ext4/xfs）
- 整盘初始化：GPT 分区 → 格式化 → 挂载 → 写 fstab，一步完成；系统盘与已挂载设备自动保护，需输入设备名二次确认
- `/etc/fstab` 持久挂载的添加/移除（每次修改自动备份到 `/etc/fstab.lsi-monitor.bak`）
- NFS 共享管理：新增/删除 export 条目，选项白名单校验，系统目录禁共享，`exportfs -ra` 自动生效

**RAID 维护（管理员）**
- 巡读（Patrol Read）：启动/暂停/恢复/停止
- VD 一致性检查（CC）与后台初始化：启动/停止
- VD 删除、外来配置（Foreign）导入
- 创建阵列：RAID 0/1/5/6/10/50，仅允许未配置盘（UGood/JBOD，JBOD 自动转 UGood）
- 磁盘操作：上线/下线/置 UGood/置 JBOD/全局热备/专用热备/移除热备/启动回拷/停止回拷/定位灯开关
- 磁盘安全擦除（Erase）：simple/normal/threepass/thorough/crypto 五种覆写模式，需输入盘位标识并勾选风险确认双重校验；擦除为后台任务，进度条实时显示在磁盘列表（红色进度条 + 预计剩余时间），可随时"停止 Erase"中止
- 阵列卡蜂鸣器报警：打开/临时关闭/永久关闭；JBOD 模式开关
- 外来配置（Foreign Configuration）：从其它机器迁移来的磁盘自带原机阵列元数据，置 UGood 后控制器会检测到外来配置——磁盘列表 DG 列显示 **F** 徽章，虚拟磁盘卡片顶部出现「载入外部配置」按钮，点击确认即执行 `/cX/fall import` 将原阵列配置导回本控制器（磁盘恢复为原 DG/VD 成员）；无外来配置时按钮自动隐藏。注意：置 UGood 本身不会让盘变成可用块设备，需要导入外来配置或手动创建阵列/置 JBOD

**平台能力**
- 用户体系：管理员（全部权限）/ 只读用户（仅查看）；PBKDF2-HMAC-SHA256 加盐哈希（12 万次迭代）
- 未创建任何用户时不启用登录认证，界面显著提示创建第一个管理员
- 采集间隔 Web 调节（1/5/15/30/60 分钟）、立即采集（绕过间隔）、CSV 导出
- 明暗主题切换、响应式布局（移动端抽屉式侧边栏）

## 4. 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Linux（需 systemd 用于一键安装；手动部署无硬性要求） |
| Python | ≥ 3.9 |
| Python 包 | `flask>=3.0`、`waitress>=3.0` |
| 必备工具 | `storcli64`（Broadcom StorCLI）—— 监控本体依赖 |
| 强烈建议 | `smartmontools`（smartctl）—— 缺失则 SMART 数据为空 |
| 邮件报警 | `sendmail`（本地 MTA，如 postfix/postfix-sendmail） |
| NVMe 功能 | `nvme-cli`（nvme list） |
| 存储管理 | `lsblk`、`parted`、`mkfs.ext4`/`mkfs.xfs`、`blkid`（按需） |

安装 storcli64（示例来源）：

```bash
wget http://amax.xyz:10001/share/select?code=WQZ8R -O "storcli64"
chmod +x storcli64
cp storcli64 /usr/local/bin/
```

> storcli 也可直接放在项目根目录下，采集器优先使用项目内的副本（或通过 `STORCLI_PATH` 指定任意路径）。

## 5. 安装部署

### 5.1 一键安装（推荐）

```bash
sudo bash install.sh
```

脚本行为：
1. 前置检查：root 权限、Python≥3.9、systemd、storcli64/smartctl/sudo 是否就绪（缺失仅告警不阻断）、端口占用检查；
2. 把项目拷贝到 `/opt/lsi-raid-monitor`（排除 `.git/data/charts/dist/__pycache__/storcli.log*` 等；目标目录已有 `data/` 时保留，即**重复执行即为无损升级**）;
3. 安装 Python 依赖（新发行版 PEP 668 环境自动加 `--break-system-packages` 重试）;
4. 生成并注册 systemd 服务 `lsi-raid-web`，开机自启、崩溃 5 秒后自动拉起;
5. 打印访问地址与服务管理命令。

安装完成后验证：

```bash
systemctl status lsi-raid-web      # 服务状态
journalctl -u lsi-raid-web -f      # 实时日志
```

### 5.2 手动部署

```bash
pip3 install -r requirements.txt
```

方式 A —— 使用内置采集线程（推荐，无需 cron）：

```bash
./run.sh                # 前台生产模式（waitress）
```

方式 B —— 注册 systemd 服务：

```bash
sudo cp deploy/lsi-raid-web.service /etc/systemd/system/
sudo systemctl enable --now lsi-raid-web
```

方式 C —— 开发调试：

```bash
python3 web_server.py   # Flask 内置服务器
```

### 5.3 sudo 免密配置

采集与 storcli 操作均以 `sudo` 执行，非 root 运行服务时请在 sudoers 放行：

```
monitor ALL=(root) NOPASSWD: /usr/local/bin/storcli64, /usr/sbin/smartctl
```

root 直接运行服务时可省略。

### 5.4 外部 cron（可选）

Web 内置采集线程默认开启；如仍想用外部 cron 触发，两者**可以并存**——采集器内有文件锁（`.collectd.lock`）与分钟标记（`.last_collect`）双保险，同一分钟不会重复采集：

```cron
* * * * * cd /opt/lsi-raid-monitor && /usr/bin/python3 lsi_collectd.py >> /var/log/lsi_collectd.log 2>&1
```

纯 cron 场景可设 `LSI_DISABLE_COLLECTOR=1` 关闭内置线程。

## 6. 快速上手

1. 浏览器访问 `http://<主机IP>:5200`。
2. 页面顶部会出现红色安全横幅“尚未创建管理员账号”；进入侧边栏 **用户管理**，创建第一个账号（选角色 管理员）。保存后即启用登录认证，之后所有访问均需登录。
3. 在监控概览页的 **邮件报警配置** 卡片填写收件人邮箱、确认 sendmail 路径与温度阈值，点击 **发送测试报警** 验证通路，然后 **保存配置**。
4. 顶栏选择采集间隔（默认 1 分钟）；点击 **导出 CSV** 可下载当天原始数据。
5. 在磁片槽位拓扑中点击任意磁盘，弹出的详情抽屉里点 **查看 SMART** 即可看到完整属性表。

> 若是把项目整体迁移到新机器：启动时会检测当天目录没有数据而自动补采一次；顶栏“更新于”超过 采集间隔×2+1 分钟未刷新 会显示“数据过期，采集中断”。

## 7. Web 界面使用指南

左侧导航共四个视图；顶栏含 主机名、总体健康徽标、数据更新时间、采集间隔选择、CSV 导出、立即采集（管理员）、主题切换、用户信息。

### 7.1 监控概览

| 区域 | 说明 |
| --- | --- |
| 综合健康评分 | 总分圆环 + 五个子分数条（控制器 30% / BBU 10% / 温度 25% / 错误计数 20% / SMART 15%），算法见 §11。同卡片下方提供 阵列卡蜂鸣器报警、JBOD 模式 的快捷开关（管理员，均有确认弹窗与行为说明） |
| 邮件报警配置 | 收件人、sendmail 路径、警告/临界阈值、九类策略勾选；被环境变量锁定的字段显示“已由环境变量锁定”且不可改。详见 §8 |
| 系统资源 | CPU%、内存用量、三次负载、运行时长，实时 5 秒刷新 |
| 文件系统 | 各挂载点容量/使用率一览（实时） |
| 四个状态卡 | 控制器（型号/固件/ROC 温度）、BBU、VD 数量、PD 数量等汇总 |
| 磁盘槽位拓扑 | 按 EID:SLOT 展示全部物理盘，颜色映射温度与健康态；点击打开详情抽屉 |
| 趋势图 | 类型切换：磁盘温度 / IO 吞吐（读写 KiB/s + IOPS） / 文件系统%；时间窗 6h/24h/72h |
| 巡读/一致性检查 | 两张卡片展示模式、当前状态、下次开始时间、已完成迭代次数 |
| 虚拟磁盘 | DG/VD、名称、RAID 级别、容量、状态、OS 设备、写缓存、进行中的后台操作；行内展开成员盘；管理员行内操作：一致性检查启停、初始化启停、删除 VD、导入外来配置（全部带二次确认） |
| 物理磁盘 | 全字段表格：槽位/型号/SN/固件/DG/状态/温度/ME OE PF/R P U(重映射·待定·无法纠正)/通电时长/告警/预测等级。管理员可多选 UGood/JBOD 盘 → 选 RAID 级别 → **创建阵列**（前端同步校验最少盘数等规则）。每行操作菜单见下文 |
| NVMe 磁盘 | 设备/型号/SN/固件/容量/已用/温度/备用空间%/寿命已用%/通电时长/介质错误/critical_warning |
| 系统信息 | 负载、内存等采集快照 |

**单盘操作菜单**（管理员，危险操作均有确认）：上线(online)、下线(offline)、置 UGood(good force)、置 JBOD、添加全局热备、添加专用热备（指定 DG）、移除热备、启动回拷（需选目标盘）、定位灯开/关。定位灯状态由 Web 端跟踪记录（storcli 无回读接口）。

**磁盘详情抽屉**：静态信息 + SMART 按钮。SATA 盘呈现标准 ATA 属性表（Value/Worst/Thresh/Raw，低于阈值或异常行高亮）；SAS 盘呈现 SCSI 摘要（温度、grown defect、通电时间等）；可查看/复制 smartctl 原始输出。对读不到 smartctl 透传的盘（如 UGood/JBOD），自动改走 storcli 原始 SMART hex 解析兜底。

### 7.2 磁盘管理

- **文件系统使用率**：容量/已用/使用率/Inode 使用率；“显示已隐藏”复选框控制视图；管理员可将某挂载点加入隐藏（跨页全局生效，用于屏蔽快照池等噪音分区）。
- **块设备**：树状枚举（过滤 loop 设备）。每行按设备状态提供：挂载（输入挂载点）、卸载、格式化（ext4/xfs，需确认且仅限未挂载设备）；系统根/boot 所在设备及已挂载设备被强制禁止格式化/卸载。
- **NFS 共享**：列出 `/etc/exports` 现有条目；表单新增共享（路径 + 客户端支持 `*`/IP/CIDR/网组 + 权限 rw/ro + 附加选项 async、no_root_squash、all_squash）。未安装 exportfs 时给出提示横幅。

### 7.3 日志中心

- **日志下载**：收集 storcli alilog、控制器事件、应用事件日志三份文本，打包 zip 下载（约数十秒）。
- **事件日志**：本系统产生的内部事件（登录成败、配置变更、每一次磁盘/RAID/存储操作、报警发送结果……），可按 错误/警告/信息 过滤，分页浏览。
- **控制器事件**：storcli 事件原文，尾行数 100/200/500 可选，关键字筛选（空格分隔多词，任一命中即保留），支持复制；结果缓存 60 秒减轻阵列卡压力。

### 7.4 用户管理（仅管理员可见）

- 用户列表 + 新建用户（用户名/口令/角色）；支持删除用户（不能删自己）与重置口令。
- 角色：`admin` 全部权限；`viewer` 仅可查看一切页面与数据，所有写接口返回 403。

## 8. 邮件报警详解

### 8.1 发送链路

`lsi_collectd` 每轮采集后调用 `lsi_alert` 进行检查 → 命中策略则记入 `events.jsonl` 并调用本地 `sendmail -t -oi` 投递。邮件由 `EmailMessage` 构建：标题 `[LSI RAID] <subject>` 按 RFC 2047 编码，正文自动 Content-Transfer-Encoding，避免中文头部导致对端退信（dsn 5.6.7）。发件人固定为 `lsi-raid-monitor@<主机名>`。

### 8.2 策略清单

| 策略键 | 含义 | 默认 |
| --- | --- | --- |
| `temp_warn` | 磁盘温度 ≥ 警告阈值（默认 45°C） | 开 |
| `temp_crit` | 磁盘温度 ≥ 临界阈值（默认 55°C），级别 error | 开 |
| `smart_alert` | 控制器报告 “S.M.A.R.T alert flagged by drive = Yes” | 开 |
| `predictive_failure` | Predictive Failure Count > 0 | 开 |
| `ctrl_health` | 控制器健康 ≠ Optimal | 开 |
| `bbu_state` | BBU/CacheVault 状态 ∉ {Optimal, Opt, OK} | 开 |
| `disk_state_change` | 任一物理盘状态变化（变为 Onln/UGood/JBOD 记 warning，其余 error） | 开 |
| `vd_state_change` | 任一 VD 状态变化（Optimal 记 warning，其余 error） | 开 |
| `smart_attr_growth` | SMART 5/187/188/197/198 任一数值较上次增长 | 开 |

### 8.3 去重机制（flag-once）

报警状态保存在 `data/.alert_state.json`：

- 每个「策略 × 对象」首次命中才发信；持续命中不再重复发。
- 状态恢复正常即清除标记，之后再次出现会重新告警。
- 手动关闭某策略时同步清除其残留标记，重新开启后按当时状态判断，不会补发积压告警。
- SMART 属性快照无论策略开关都会更新，防止长期关闭期间的增长一次性补报。

### 8.4 配置锁定

在环境变量设置 `ALERT_EMAIL_TO` / `SENDMAIL_PATH` / `ALERT_TEMP_WARN` / `ALERT_TEMP_CRIT` 后，对应字段以环境变量为准，Web 界面显示锁定提示且保存时跳过该字段——适合由配置管理系统统一管理的场景。

## 9. 数据采集详解

### 9.1 触发与调度

- 内置线程每隔一分钟整点对齐（sleep 到下一分钟的 00 秒）执行 `python3 lsi_collectd.py`，超时上限 110 秒。
- 采集脚本入口顺序：`--force` / `--quick` 判断 → 间隔门控（分钟数 % interval == 0）→ 文件锁 → 分钟去重标记 → 执行采集。任一环节被拦截则静默退出。
- Web“立即采集”即 `--force` 运行，绕过门控与去重（仍受锁保护）。
- **操作后即时刷新**：所有磁盘/阵列操作（上线/下线/置 UGood/JBOD/热备/Copyback、安全擦除、导入外来配置、巡读/CC/VD 初始化、创建/删除阵列等）成功后，后端同步触发 `python3 lsi_collectd.py --quick` —— 与 `--force` 一样绕过门控与去重，但跳过 SMART 等耗时采集，仅约 1 秒即可让页面反映最新状态，无需等待下一轮定时采集。

### 9.2 各数据项频率

| 数据 | 频率 | 落盘文件（覆盖/追加） |
| --- | --- | --- |
| 物理盘摘要+错误计数+重建/回拷进度 | 每 | `disks.csv` 追加 |
| 控制器/BBU | 每 | `controller.csv` 追加 |
| 系统 负载/内存 | 每 | `system.csv` 追加 |
| IO 原始计数器 | 每 | `io.csv` 追加 |
| 文件系统用量 | 每 | `fs.csv` 追加 |
| NVMe 直连盘 | 每 | `nvme.csv` 追加 |
| 巡读 / CC 属性 | 每 | `patrol.csv` / `consistency.csv` 覆盖写 |
| VD 清单 | 每（操作后即时） | `vds.csv` 追加，展示时取最新一轮快照 |
| 磁盘属性（SN/固件/速率） | 每天 | `attributes.csv` 当天首采写入 |
| SMART | 每 15 分钟（minute%15==0） | `smart.csv` 覆盖写；当天缺失或 --force 时立即补采 |

当整块阵列查询整体失败（例如有盘 Failed 时 storcli 返回 ErrCd 45），程序从 Command Status 的 Detailed Status 中提取失败盘并补充一条 `state=Failed` 记录，保证故障盘不从页面消失。CSV 字段升级时自动迁移旧表头（旧列保留、新列留空）。

### 9.3 SMART 采集的多级回退

1. `smartctl -a -d megaraid,<DID> <基设备>`：基设备依次尝试 `/dev/sda` → `/dev/bus/0` → 第一个存在的 `/dev/sdX`（部分机器 VD 从 sdb 起）。
2. 解析优先 SCSI 分支（SAS 盘：grown defect list、non-medium error、power on hours、drive temperature），失败再走 ATA 属性表分支。
3. 二者都拿不到通电时长视为透传失败 → 回退 `storcli .../show smart J` 读原始 hex，按 ATA SMART Read Data 结构（2 字节版本号 + 每 12 字节一个属性：ID/flags/value/worst/raw6 小端）解析。

## 10. 数据文件说明

```
data/
├── YYYY-MM-DD/            # 每日一个目录
│   ├── disks.csv           时间,EID,SLOT,DID,DG,型号,状态,容量,接口,介质,
│   │                       温度,介质错误,其他错误,PF,SMART告警,Shield,
│   │                       rebuild进度/ETA,copyback进度/ETA,erase进度/ETA
│   ├── controller.csv      时间,型号,固件,健康,VD数,PD数,ROC温度,
│   │                       BBU型号/状态/温度,各VD状态拼接
│   ├── vds.csv             时间,DG/VD,类型,状态,容量,名称
│   ├── attributes.csv      时间,EID,SLOT,DID,SN,固件,设备速率,链路速率
│   ├── smart.csv           时间,DID,重映射,待定,无法纠正,上报不可纠正,
│   │                       命令超时,通电小时,SMART温度
│   ├── system.csv          时间,load_1m/5m/15m,内存总量KB,可用KB
│   ├── io.csv              时间,设备名,读次数,读扇区,读毫秒,写次数,写扇区,写毫秒,在途IO
│   ├── fs.csv              时间,设备,挂载点,类型,总量KB,已用KB,可用KB,使用%
│   ├── nvme.csv            时间,设备,型号,SN,固件,容量B,已用B,温度,备用空间%,
│   │                       寿命%,critical_warning,通电小时,通断电次数,
│   │                       非正常断电,介质错误
│   ├── patrol.csv          PR 模式/延迟/迭代次数/下次时间/当前状态/并发盘数
│   └── consistency.csv     CC 模式/延迟/下次时间/当前状态/迭代次数/完成VD数
├── alert_config.json       报警配置（收件人/路径/阈值/策略开关）
├── collection_config.json  {"interval_minutes": 1}
├── events.jsonl            事件日志（一行一个 JSON，时间戳/级别/消息）
├── users.json              用户库（0600；salt + PBKDF2 hash）
├── .secret_key             Flask 会话密钥（32 字节随机，0600，重启不掉线）
├── .alert_state.json       报警去重状态与各对象状态快照
├── .locate_state.json      定位灯状态跟踪
├── .fs_hidden.json         隐藏的文件系统挂载点
├── .collectd.lock          采集互斥锁
└── .last_collect           分钟级去重标记
```

`/etc/fstab` 与 `/etc/exports` 修改前的备份分别为 `/etc/fstab.lsi-monitor.bak`、`/etc/exports.lsi-monitor.bak`。

## 11. 健康评分算法

总分 = Σ(子分 × 权重)，权重与计分规则如下（前端计算，随时可在 `web/static/js/app.js computeHealth()` 调整）：

| 子项 | 权重 | 计分 |
| --- | --- | --- |
| 控制器 | 30% | 健康 Optimal=100；warn/未知=60；crit=20；存在 crit 级 VD ≤30、warn 级 VD ≤70 |
| BBU | 10% | 无 BBU=80；状态 OK/Optimal/Good/Normal=100；否则 40；BBU 温度 ≥60°C 封顶 40 |
| 温度 | 25% | 全部磁盘最高温：< 警告阈值=100；≥ 警告=55；≥ 临界=10 |
| 错误计数 | 20% | 100 − PF×40 − 介质错误×10 − 其他错误×3，下限 0 |
| SMART | 15% | 有盘 SMART 告警=20；有盘 重映射/待定/无法纠正 >0 =60；否则 100 |

评级：`≥80` 健康（绿）；`60–79` 需要关注（黄）；`<60` 存在风险（红）。

顶栏的整体健康徽标另有一套严格规则（后端计算）：控制器非 Optimal、或有盘 Failed/UBad/SMART 告警、或温度 ≥ 临界 → **严重(crit)**；有盘 Offln/Missing、PF 或介质错误 >0、重映射/待定 >0、温度 ≥ 警告 → **警告(warn)**；无数据 → **未知**。

## 12. 磁盘故障预测规则

对每块物理盘输出 `ok / info / warn / crit` 四级预测及原因列表（`web_server._predict_disk()`），综合以下信号取最高级别：

| 级别 | 触发条件 |
| --- | --- |
| crit | 状态 Failed/UBad；控制器报告 PF 或 SMART 告警；待定扇区 >0；重映射扇区 ≥50；跨天对比 待定扇区 出现增长 |
| warn | 重映射扇区 1–49；无法纠正错误 >0；控制器介质错误 >0；温度 ≥ 临界；Offln/Missing；跨天对比 重映射/无法纠正 增长 |
| info | 命令超时累计 >0；温度 ≥ 警告；通电时长 ≥ 43800h（5 年） |
| ok | 无以上任何信号 |

跨天趋势比较基于每日 `smart.csv` 快照最早与最新两份。判级先于一切计数器——Failed/UBad 的盘即使读不到 SMART 也判高危，避免“读不到=没事”的假阴性。

## 13. 环境变量参考

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LSI_DATA_DIR` | `<项目>/data` | 数据与配置目录 |
| `LSI_WEB_HOST` / `LSI_WEB_PORT` | `0.0.0.0` / `5200` | Web 监听地址/端口 |
| `STORCLI_PATH` | 项目内 `storcli64` → `/usr/local/bin/storcli64` | storcli 路径 |
| `LSI_CONTROLLER` | `/c0` | 目标控制器编号（多卡机器可为 `/c1` 等；本版本主要面向单卡） |
| `SMARTCTL_PATH` | `/usr/sbin/smartctl` | smartctl 路径 |
| `LSI_DISABLE_COLLECTOR` | — | `1` 时关闭 Web 内置采集线程（纯 cron 部署） |
| `ALERT_EMAIL_TO` | — | 报警收件人（逗号分隔多个）；设置后 Web 中锁定该字段 |
| `SENDMAIL_PATH` | `/usr/sbin/sendmail` | sendmail 路径；设置后锁定 |
| `ALERT_TEMP_WARN` | `45` | 温度警告阈值 °C；设置后锁定 |
| `ALERT_TEMP_CRIT` | `55` | 温度临界阈值 °C；设置后锁定 |

## 14. API 接口一览

除标注 🔓 外均为 JSON；`GET` 读取类仅需登录，`POST/DELETE` 写操作需管理员（未建用户时开放）。

**认证**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/login` | 登录 `{username,password}` |
| POST | `/api/logout` | 退出 |
| GET | `/api/me` | 当前认证状态/用户/角色 |

**监控与数据**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 最新全量状态（缓存 15s）：控制器/VD/PD/NVMe/巡读CC/系统/健康 |
| GET | `/api/history?hours=6\|24\|72` | 每盘温度时序 |
| GET | `/api/io_history?hours=` | 每盘 IO 速率/IOPS 时序（相邻样本差分计算） |
| GET | `/api/fs_history?hours=` | 文件系统使用率时序（排除隐藏挂载点） |
| GET | `/api/events?level=&page=&page_size=` | 应用事件日志（分页） |
| GET | `/api/controller_events?lines=&q=` | 控制器事件文本（60s 缓存，关键字过滤） |
| GET | `/api/vd_detail` | VD 明细含成员盘（降级时按 DG 兜底填充） |
| GET | `/api/disk_smart?eid=&slot=` | 单盘 SMART 结构化属性 + 原始输出 |
| GET | `/api/export.csv?type=disks` | 导出当天某 CSV |
| GET | `/api/logs/download` | alilog+控制器事件+应用事件 zip |
| GET | `/api/system/realtime` | 实时 CPU/内存/负载/uptime/每盘 IO（1s 采样窗） |

**配置**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/alert_config` | 报警配置读写（POST 校验阈值范围与 warn≤crit） |
| POST | `/api/alert_test` | 发送测试邮件 |
| GET/POST | `/api/collection_config` | 采集间隔（限 1/5/15/30/60） |
| POST | `/api/collect_now` | 立即采集（--force） |
| GET/POST | `/api/controller_alarm` | 蜂鸣器 读状态 / `{mode:on\|silence\|off}` |
| GET/POST | `/api/controller_jbod` | JBOD 模式 读状态 / `{mode:on\|off}` |

**磁盘 / RAID 操作（admin）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/disk_action` | `{eid,slot,action}`，action ∈ online/offline/good/jbod/locate_start/locate_stop/hotspare_global/hotspare_dedicated(+dg)/hotspare_delete/copyback_start(+target_eid,target_slot)/copyback_stop/erase_stop |
| POST | `/api/disk_erase` | 启动安全擦除，`{eid,slot,pattern:simple\|normal\|threepass\|thorough\|crypto,confirm:"E{eid}:{slot}",acknowledge:true}`，管理员 + 双重确认 |
| POST | `/api/raid_action` | `{target,action,vd}`：patrolread start/stop/pause/resume；cc、vd_init start/stop；vd_delete delete |
| GET | `/api/foreign_config` | 查询控制器外来配置状态 `{ok,present,count,description}` |
| POST | `/api/foreign_import` | 导入外来配置 `{acknowledge:true}`，即 storcli `/cX/fall import` |
| POST | `/api/raid/create` | `{level,drives:[{eid,slot}],name}` 创建阵列 |

**存储管理**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/storage/devices` | lsblk 树（loop 过滤，标注 system/挂载点） |
| POST | `/api/storage/mount` / `umount` / `format` | 挂载/卸载/格式化(ext4,xfs) |
| POST | `/api/storage/init_disk` | 整盘初始化 GPT→mkfs→mount→fstab，返回步骤结果 |
| POST | `/api/storage/fstab` | `{action:add/remove,mountpoint,...}` fstab 管理 |
| GET | `/api/storage/usage` | 文件系统用量（含 inode） |
| POST | `/api/storage/visibility` | 隐藏/恢复挂载点 |
| GET/POST | `/api/nfs/exports`、`POST /api/nfs/exports/delete` | NFS 共享列表/新增/删除 |

**用户（admin）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/users` | 列表 / 创建 `{username,password,role}` |
| DELETE | `/api/users/<name>` | 删除（不能删自己） |
| POST | `/api/users/<name>/password` | 重置口令 |

**页面**：`GET /` 渲染单页应用。

## 15. 日常运维

```bash
# 服务管理
sudo systemctl restart lsi-raid-web
sudo systemctl stop lsi-raid-web

# 升级：拉取新代码后在源目录重复执行即可（data/ 不动）
sudo bash install.sh

# 修改端口：安装脚本读取 LSI_WEB_PORT
LSI_WEB_PORT=8080 sudo -E bash install.sh
# 已装好的可直接编辑 /etc/systemd/system/lsi-raid-web.service 后
sudo systemctl daemon-reload && sudo systemctl restart lsi-raid-web

# 备份：只需打包数据目录（含配置、事件、用户、密钥）
tar czf lsi-backup.tgz /opt/lsi-raid-monitor/data

# 只看某天数据
ls /opt/lsi-raid-monitor/data/$(date +%F)/

# 手动跑一轮采集（绕过间隔）
cd /opt/lsi-raid-monitor && python3 lsi_collectd.py --force
```

磁盘空间管理：CSV 每天一个目录，长期运行请按需清理历史日期目录（趋势图最多只查 72 小时窗口，超期数据不影响功能）。

## 16. 故障排查 FAQ

**Q1 页面一直显示“加载中…”或接口 500**
`journalctl -u lsi-raid-web -n 100` 看报错。最常见原因是 storcli 不在预期路径：`which storcli64` 核对，必要时设 `STORCLI_PATH`。

**Q2 温度/错误计数全空，日志里有 `storcli error`**
多为 sudo 权限问题。以服务运行的用户执行 `sudo /usr/local/bin/storcli64 /c0 show` 验证，按 §5.3 配置免密。

**Q3 SMART 通电时长一直是 0**
脚本有三层回退（§9.3），若三层都失败通常是老固件的透传兼容问题；确认 smartctl 版本足够新，以及 DID 正确（storcli `show all` 中的 DID）。

**Q4 收不到报警邮件**
依序检查：① 配置了收件人吗（界面 enabled 徽标）；② sendmail 是否存在可执行（界面 sendmail 徽标）；③ 日志中心搜“报警邮件发送失败”看 MTA 返回的错误；④ 用“发送测试报警”按钮复现；⑤ 本地 MTA（postfix/exim）是否正确配置了 relay。

**Q5 顶栏提示“数据过期，采集中断”**
最后采集时间距现在超过 间隔×2+1 分钟。查看服务是否存活、cron 是否还在跑、`/opt/lsi-raid-monitor/data/.last_collect` 的内容是否在推进。

**Q6 换机器部署后图表没历史数据**
历史 CSV 属于旧机器的数据目录，若未随迁则只有新数据。系统启动时会因当天无数据自动补采一次，几分钟内界面即恢复正常。

**Q7 创建阵列按钮灰色**
只有状态为 UGood/JBOD 的盘可勾选；JBOD 盘提交后会自动转 UGood 再组阵列。

**Q8 忘记管理员密码**
管理员登录另一个 admin 账号在用户管理重置；若无任何可用账号，删除 `data/users.json` 后重启服务（回到未认证状态，重新创建管理员）。

**Q9 viewer 登录后看不到某些按钮**
这是预期的权限行为；viewer 为只读角色，所有写接口在服务端同样拒绝（403）。

## 17. 安全说明

- **认证开关**：`users.json` 无任何用户时不启用登录，此时任何人都可以执行危险操作——首页常驻红色横幅提醒尽快创建管理员。
- **口令存储**：PBKDF2-HMAC-SHA256、120000 次迭代、16 字节随机盐，常数时间比较。
- **会话**：HttpOnly + SameSite=Lax Cookie，有效期 12 小时；密钥持久化于 `data/.secret_key`（0600），重启不会让在线用户掉线。
- **响应头**：统一附加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: same-origin`；请求体上限 1 MB。
- **危险操作防护**：磁盘/RAID/格式化/整盘初始化等均在前后端双重确认；系统盘识别自 `/proc/mounts`（`/`、`/boot` 所在设备）并被全面禁止格式化/卸载；fstab、exports 修改前自动备份；NFS 导出路径黑名单（/etc /boot /proc /sys /dev /run /usr /bin /sbin /lib /lib64 /root）。
- **审计**：所有登录、配置变更、磁盘/RAID/存储操作都会写入事件日志并注明操作者；可在日志中心检索，也包含在 zip 日志包内。
- **最小权限建议**：生产环境使用非 root 专用账户运行服务 + sudoers 白名单（§5.3）；通过防火墙限制 5200 端口来源；如需公网暴露，建议前置反代加 HTTPS 与额外认证。
