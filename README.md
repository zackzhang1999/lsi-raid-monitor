本系统依赖lsi的官方管理工具storcli64,请先把storcli64可执行文件放到 /usr/local/bin 目录下
wget http://amax.xyz:10001/share/select?code=WQZ8R -O "storcli64"
chmod +x storcli64 
cp storcli64  /usr/local/bin/


# LSI RAID 监控（LSI MegaRAID Monitor）

面向 LSI/Broadcom MegaRAID 控制器的存储监控平台：分钟级采集磁盘温度、状态、错误计数、SMART 关键属性，Web 仪表盘实时展示，异常自动邮件报警。

UI 基于 google-design 设计体系（DM Sans / JetBrains Mono，浅色主色 `#4285f4`，深色 accent `#fc2c50`）。

## 功能

- **监控概览**：综合健康评分、控制器/BBU 状态、磁盘槽位拓扑、温度趋势图（6h/24h/72h）、巡读与一致性检查、虚拟/物理磁盘清单（VD 可展开查看成员磁盘）、事件日志
- **实时性能**：CPU/内存/负载/运行时间实时刷新，每盘 IO 读写速率与 IOPS；IO 吞吐与文件系统使用率历史趋势图
- **文件系统**：各挂载点容量与使用率一览，可按挂载点隐藏/恢复展示指定分区（管理员，全局生效）；挂载/卸载、格式化（ext4/xfs）、`/etc/fstab` 持久挂载管理
- **整盘初始化**（管理员）：一键 GPT 分区 + 格式化 + 挂载 + 写 fstab，系统盘与已挂载设备自动保护，需输入设备名确认
- **RAID 维护**（管理员）：巡读启动/暂停/恢复/停止，VD 一致性检查启停，VD 初始化启停，均带二次确认；阵列卡蜂鸣器报警 打开/临时关闭/永久关闭、JBOD 模式开关（健康评分卡片内）
- **SMART 详情**：ATA 属性表结构化解析（SAS 盘显示 SCSI 摘要），异常行高亮，可查看原始输出
- **故障预测**：基于 PF/SMART 告警、重映射/待定/无法纠正扇区、介质错误、温度、通电时长及跨天增长趋势的规则化评估，物理磁盘表格与详情抽屉展示 正常/关注/警告/高危 等级及判定原因
- **控制器事件**：storcli 事件日志在线查看（100/200/500 行）；一键收集 alilog、控制器事件与平台事件日志打包 zip 下载
- **邮件报警**：温度阈值、SMART 告警、预测性故障、控制器/BBU 异常、磁盘/VD 状态变化、SMART 关键属性增长；每类报警均可在 Web 单独开关（报警策略）；Web 可配置收件人与阈值，支持环境变量锁定
- **磁盘操作**（管理员）：上线/下线/置为 UGood/置为 JBOD/定位灯开关
- **用户体系**：管理员 / 只读用户两种角色，PBKDF2 加盐哈希存储，创建第一个管理员后自动启用登录认证
- **采集控制**：Web 调整采集间隔（1/5/15/30/60 分钟）、手动立即采集（绕过间隔门控）、CSV 导出

<img width="3840" height="1907" alt="0c8d851e-f343-4a88-aaaf-15b403e50de5" src="https://github.com/user-attachments/assets/1b41ec9a-9876-487a-92e3-f718a5e01647" />
<img width="3840" height="1907" alt="4e488084-b163-4b02-aa79-d2f062cf1e3f" src="https://github.com/user-attachments/assets/39d8d582-741b-4514-8f89-5e7853c2283e" />






## 目录结构

```
lsi_collectd.py     数据采集器（Web 内置线程每分钟触发，也可用 cron）
install.sh          一键安装脚本（安装到 /opt 并注册 systemd 服务）
lsi_alert.py        邮件报警与事件日志模块
web_server.py       Flask Web 后端（API + 页面）
user_mgr.py         用户管理（PBKDF2）
storage_mgr.py      块设备挂载/卸载/格式化
web/                前端（google-design 体系，原生 JS + Chart.js）
data/               采集数据（按日期分目录 CSV）、配置、事件日志
deploy/             systemd 单元示例
run.sh              生产模式启动脚本
```

## 部署

依赖：Python ≥ 3.9、systemd、`storcli64`、`smartmontools`、`sendmail`（可选，用于邮件报警）。

**一键安装（推荐）**：安装到 `/opt/lsi-raid-monitor` 并注册 systemd 服务（重复执行即为升级，保留 `data/` 数据）：

```bash
sudo bash install.sh
```

安装完成后：

```bash
systemctl status lsi-raid-web     # 查看状态
journalctl -u lsi-raid-web -f     # 查看日志
```

**手动部署**：

```bash
pip3 install -r requirements.txt
```

1. **Web 服务**（内置采集线程，启动后自动每分钟采集，无需 cron）：

   ```bash
   ./run.sh                     # 前台生产模式（waitress）
   # 或 systemd：
   sudo cp deploy/lsi-raid-web.service /etc/systemd/system/
   sudo systemctl enable --now lsi-raid-web
   ```

   采集需要 `sudo storcli64` 与 `sudo smartctl` 权限，建议在 sudoers 中配置免密：

   ```
   monitor ALL=(root) NOPASSWD: /usr/local/bin/storcli64, /usr/sbin/smartctl
   ```

   如仍希望使用外部 cron 触发采集（与内置线程并存不会重复采集，采集器内置文件锁与分钟级去重）：

   ```cron
   * * * * * cd /opt/lsi-raid-monitor && /usr/bin/python3 lsi_collectd.py >> /var/log/lsi_collectd.log 2>&1
   ```

   > 注意：把项目拷贝到新机器部署时，`data/` 里是旧机器的历史数据。Web 启动时会检测当天无数据则立即补采一次；顶栏"更新于"超过采集间隔 2 倍未刷新会显示"数据过期"警告。

2. 浏览器访问 `http://<host>:5200`，在“用户管理”页创建第一个管理员账号后即启用登录认证。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LSI_DATA_DIR` | `./data` | 数据与配置目录 |
| `LSI_WEB_HOST` / `LSI_WEB_PORT` | `0.0.0.0` / `5200` | Web 监听地址 |
| `STORCLI_PATH` | 项目内 `storcli64` 或 `/usr/local/bin/storcli64` | storcli 路径 |
| `LSI_CONTROLLER` | `/c0` | 控制器 |
| `SMARTCTL_PATH` | `/usr/sbin/smartctl` | smartctl 路径 |
| `LSI_DISABLE_COLLECTOR` | — | 设为 `1` 关闭 Web 内置采集线程（仅用外部 cron 时） |
| `ALERT_EMAIL_TO` | — | 报警收件人（设置后 Web 中锁定） |
| `SENDMAIL_PATH` | `/usr/sbin/sendmail` | sendmail 路径（设置后锁定） |
| `ALERT_TEMP_WARN` / `ALERT_TEMP_CRIT` | `45` / `55` | 温度阈值 °C（设置后锁定） |

## 数据说明

- `data/YYYY-MM-DD/*.csv`：每分钟追加的磁盘/控制器/系统/IO 计数/文件系统用量数据，当日一次性的 VD/属性/SMART 快照
- `data/alert_config.json`：报警配置；`data/collection_config.json`：采集间隔
- `data/events.jsonl`：事件日志（Web 事件页读取）；`data/users.json`：用户（0600 权限）
