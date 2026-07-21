# LSI RAID Monitor

Minute-level LSI/Broadcom MegaRAID health monitoring with direct terminal output and optional daily HTML email reports.

Collects disk temperature, error counters, SMART attributes, virtual disk status, BBU health, patrol read progress, and system load — then prints a readable summary to the terminal and saves an HTML report. Email can be enabled by setting SMTP environment variables.

Works with SAS3108 (Invader) and similar controllers (SAS2208, SAS3008, SAS3508).

## Requirements

- **Python 3.9+**
- **`matplotlib`** — optional, only needed for the temperature chart (`pip install matplotlib`)
- **storcli64** (Broadcom/LSI MegaRAID CLI, copy to project root as `storcli64`)
- **smartmontools** (`apt install smartmontools`) — optional, for SMART attributes
- **sudo** access to `storcli64` and `smartctl`
- A CJK font for Chinese chart labels (optional; the report uses English by default)

Tested on Debian 12 with storcli v007.3405 and an MR9362-8i controller.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/Dragonrster/lsi-raid-monitor.git
cd lsi-raid-monitor

# 2. Copy storcli64 into project root (recommended for portability)
cp /usr/local/bin/storcli64 ./storcli64

# 3. Optional: install matplotlib if you want temperature charts
pip install matplotlib

# 4. Optional: set SMTP credentials to enable daily HTML email report
export SMTP_HOST=smtp.qq.com
export SMTP_PORT=465
export SMTP_USER=you@example.com
export SMTP_PASS=your_password
export SMTP_FROM=you@example.com
export SMTP_TO=you@example.com

# 5. Optional: enable immediate fault alerts via local sendmail
export ALERT_EMAIL_TO=ops@example.com,admin@example.com

# 6. Optional: set data directory (default ./data in project root)
export LSI_DATA_DIR=./data

# 6. Install cron + configure sudoers
sudo bash setup_cron.sh

# 7. Manual test
sudo python3 lsi_collectd.py          # collect one data point
python3 lsi_report.py                 # print 24h report + save HTML
bash lsi_send_now.sh                  # collect + report in one step
```

**Migration:** Since data is stored in `./data` and `storcli64` is expected in the project root, you can simply copy the entire project directory to another server and run it.

## How It Works

```
cron (every min)                    cron (10:00 daily) / manual
     │                                        │
lsi_collectd.py ──sudo──> ./storcli64      lsi_report.py
     │                      smartctl          │
     │    ├─ lsi_alert.py (sendmail)          │
     ▼    ▼                                 ▼
./data/YYYY-MM-DD/                   terminal text summary
  disks.csv                              │
  controller.csv                         ▼
  system.csv                      HTML report file
  vds.csv / patrol.csv                   │
  smart.csv / attributes.csv             ▼
                                SMTP email (HTML, optional)
                                Sendmail alert (text, immediate)
```

### `lsi_collectd.py` — Data Collector

Runs every minute via cron. Collects:

| Metric                                                      | Source                               |
| ----------------------------------------------------------- | ------------------------------------ |
| Disk temperature, state, model                              | `storcli64 /c0/eall/sall show all J` |
| Media/other/predictive errors, SMART alert                  | same                                 |
| Controller model, FW, health                                | `storcli64 /c0 show J`               |
| VD list (RAID level, size, state)                           | same                                 |
| BBU/CacheVault (model, temp, state)                         | same                                 |
| Patrol read (next run, state, progress)                     | `storcli64 /c0 show patrolread J`    |
| Consistency check (next run, state)                         | `storcli64 /c0 show cc J`            |
| Disk serial, firmware, link speed                           | `storcli64 ... show all J`           |
| SMART (reallocated, reported/command-timeout, pending, uncorrectable, power-on hours) | `smartctl -a -d megaraid,N` |
| System load, memory                                         | `/proc/loadavg`, `/proc/meminfo`     |

### Immediate Fault Alerts

If `ALERT_EMAIL_TO` is set, `lsi_collectd.py` will check for abnormal conditions after every collection and send an immediate plain-text alert via the local `sendmail` service (`/usr/sbin/sendmail` by default).

Alerts are triggered when:

- Controller health is not Optimal
- Physical disk state is not Online/Hotspare/UGood/Optimal
- Disk temperature reaches `TEMP_WARN` or `TEMP_CRIT`
- SMART alert is flagged by a drive
- Media error / other error / predictive failure counters are greater than 0
- Watched SMART attributes **5 / 187 / 188 / 197 / 198** (reallocated sectors, reported uncorrectable errors, command timeout, pending sectors, offline uncorrectable) change compared to the previous collection

To avoid spam, the same active alert is not resent every minute; a new email is sent only when a new abnormal condition appears. When all issues recover, the alert state is reset automatically.

Configuration can be done in three ways (from highest to lowest priority):

1. **Environment variables**: `ALERT_EMAIL_TO`, `SENDMAIL_PATH`, `TEMP_WARN`, `TEMP_CRIT`
2. **Web UI**: open the dashboard, edit the "邮件报警配置" card, and click **Save**. Settings are written to `$LSI_DATA_DIR/alert_config.json`.
3. **Manually edit** `$LSI_DATA_DIR/alert_config.json`.

When a value is set via environment variable, the Web UI will show it as locked and cannot override it.

Example environment setup:

```bash
export ALERT_EMAIL_TO=ops@example.com,admin@example.com
export SENDMAIL_PATH=/usr/sbin/sendmail
# optional thresholds
export TEMP_WARN=45
export TEMP_CRIT=50
```

Test sendmail delivery from command line:

```bash
ALERT_EMAIL_TO=you@example.com python3 lsi_alert.py
```

Or open the Web UI and click **测试报警**.

### `lsi_report.py` — Report Generator

Runs daily at 10:00 (or manually). Reads the last 24 hours of CSV data and:

- Prints a readable text summary directly to the terminal
- Saves an HTML report containing:
  - Controller summary (model, FW, health, BBU, patrol read, CC, system load/memory)
  - Virtual disk table (RAID level, size, state)
  - Physical disk table (model, SN, firmware, state, temperature range, error counters, SMART sector stats, power-on hours)
  - Temperature line chart (one line per disk, warn/crit threshold lines, BBU reference)
- Sends the HTML report by email **only if** `SMTP_USER` is configured

### `lsi_send_now.sh` — One-Shot Report

Runs the collector once, then generates the report immediately. `./lsi_send_now.sh 2026-05-30` generates a full-day report for a specific date.

## Web UI

A modern, real-time web dashboard is included under `web/`.

### Quick Start

```bash
bash start_web.sh
```

Then open `http://127.0.0.1:5200` in your browser.

The page auto-refreshes every 5 seconds and shows:

- Overall controller health and status badge
- **Health score dashboard** with a ring chart and per-dimension progress bars
- **Dark / light theme toggle** with `localStorage` persistence
- Key metrics cards (disks / VDs, temperature overview, BBU state)
- Interactive temperature trend chart (Chart.js, 6h / 24h / 3d ranges)
- Patrol Read and Consistency Check schedules
- Virtual Disks and Physical Disks tables with SMART / error counters
- Click any physical disk (table row or topology slot) to view full `smartctl -a` output
- **Event log timeline** for status changes, temperature alerts, and SMART issues
- System load and memory usage
- **Collect Now** button to trigger an immediate data refresh
- **Export CSV** button to download historical disk data
- **Alert configuration** form to set recipients, sendmail path, and temperature thresholds
- **Test Alert** button to send a test alert email via local sendmail
- Alert configuration status card showing recipients, sendmail path, and availability

### Configuration

| Variable           | Default                     | Description                          |
| ------------------ | --------------------------- | ------------------------------------ |
| `FLASK_RUN_HOST`   | `127.0.0.1`                 | Web server listen address            |
| `FLASK_RUN_PORT`   | `5200`                      | Web server listen port               |
| `FLASK_DEBUG`      | `0`                         | Enable Flask debug mode (`1`)        |
| `WEB_PASSWORD`     | —                           | Bootstrap password for the `admin` user on first start (optional) |
| `WEB_SECRET_KEY`   | auto-generated              | Flask session secret key (optional)  |

Example: listen on all interfaces on port 8080

```bash
FLASK_RUN_HOST=0.0.0.0 FLASK_RUN_PORT=8080 bash start_web.sh
```

### API Endpoints

| Endpoint                 | Method | Description                                  |
| ------------------------ | ------ | -------------------------------------------- |
| `/`                      | GET    | Dashboard HTML page                          |
| `/api/status`            | GET    | Current RAID / controller / disk status JSON |
| `/api/history?hours=24`  | GET    | Temperature time series for Chart.js         |
| `/api/events?limit=50`   | GET    | Event log (status changes, alerts)           |
| `/api/export/csv?hours=24` | GET  | Download historical disk data as CSV         |
| `/api/collect`           | POST   | Trigger one manual data collection           |
| `/api/storage/disks`     | GET    | List all disks enumerated via `lsblk`        |
| `/api/storage/disk/<name>/smart` | GET | SMART info for a disk                  |
| `/api/storage/mount`     | POST   | Mount a filesystem                           |
| `/api/storage/umount`    | POST   | Unmount a filesystem                         |
| `/api/storage/format`    | POST   | Format a disk/partition (mkfs.ext4 / mkfs.xfs) |
| `/api/login`             | POST   | Log in with username + password              |
| `/api/logout`            | POST   | Log out                                      |
| `/api/auth/status`       | GET    | Current authentication status (incl. role)   |
| `/api/users`             | GET    | List users (admin only)                      |
| `/api/users`             | POST   | Create a user (admin only)                   |
| `/api/users/password`    | POST   | Reset a user's password (admin only)         |
| `/api/users/delete`      | POST   | Delete a user (admin only)                   |

### Docker Deployment

```bash
# Build and run with docker-compose
docker-compose up -d

# Or build manually
docker build -t lsi-raid-monitor .
docker run -d -p 5200:5200 -v ./data:/var/lib/lsi-monitor/data lsi-raid-monitor
```

Access the dashboard at `http://<host>:5200`.

**Note:** To actually collect data from the host RAID controller inside the container, you need to map `storcli64` (and optionally `/dev`) into the container and possibly run it with `privileged: true`. See `docker-compose.yml` comments.

| `/api/alert/config`     | GET    | Alert configuration and sendmail status        |
| `/api/alert/config`     | POST   | Update and save alert configuration            |
| `/api/alert/test`       | POST   | Send a test alert email                        |
| `/api/disk/operations`     | GET    | Supported disk operations                    |
| `/api/disk/<eid>/<slot>/operate` | POST | Execute disk operation (good/online/offline/jbod) |
| `/api/disk/<eid>/<slot>/smart` | GET | Full SMART output from smartctl for the disk |

### Disk Operations

The Web UI allows changing the state of individual physical disks via `storcli64`:

| Action | storcli command | Description |
| ------ | --------------- | ----------- |
| `good`   | `set good`   | Mark disk as Unconfigured Good |
| `online` | `set online` | Bring disk Online |
| `offline`| `set offline`| Take disk Offline |
| `jbod`   | `set jbod`   | Enable JBOD pass-through mode |

**Warning:** These operations directly affect the RAID array. Make sure you understand the consequences before executing them. Always confirm in the dialog.

## Disk Management

The Web UI includes a disk management page that enumerates all block devices via `lsblk` (independent of the RAID controller) and shows size, filesystem, and mount point for each disk and partition.

Supported operations, all executed via `sudo` (see the sudoers entries installed by `install_and_start.sh`):

- **Mount / unmount** a filesystem on a partition
- **Format** a disk or partition with `mkfs.ext4` or `mkfs.xfs`
- View SMART information for any disk

**Warning:** Formatting destroys all data on the target. The UI requires a second confirmation, and disks that contain a mounted system partition are protected and cannot be formatted or unmounted from the UI.

## User Management

The Web UI supports role-based users, stored in `$LSI_DATA_DIR/users.json` (passwords are PBKDF2-hashed):

- **admin** — full access, including dangerous operations (disk state changes, mount/umount, format, collection config, alert config, user management)
- **viewer** — read-only; all write APIs return `403`, and operation buttons are hidden in the UI

Bootstrap / migration:

- If no user exists and `WEB_PASSWORD` is set, the first startup automatically migrates it to an `admin` user (username `admin`, password = `WEB_PASSWORD`).
- If no user exists and `WEB_PASSWORD` is not set, authentication stays disabled and everything is unrestricted (as before).

Admins can create users, reset passwords, and delete users from the **用户管理** button in the header (or via the `/api/users` APIs). You cannot delete yourself or the last remaining admin.

## Security

- **No authentication by default.** If no user exists in `users.json`, anyone who can reach the web port can use the dashboard and all APIs. Only run it on a trusted internal network and keep `FLASK_RUN_HOST` at `127.0.0.1` or behind a firewall.
- **Create an admin user** (or set `WEB_PASSWORD`, which is migrated to the `admin` user on first start) to enable authentication. Write operations (disk operations, format, mount, collect, alert config, user management) then require an admin login; viewer accounts can only view. `WEB_SECRET_KEY` can optionally pin the Flask session secret.
- **Destructive disk operations:** formatting requires a second confirmation in the UI, and system disks are protected from format/umount. The sudoers file grants only the specific commands needed (`storcli64`, `smartctl`, `mount`, `umount`, `mkfs.ext4`, `mkfs.xfs`, `mkdir`, `lsblk`) — review `/etc/sudoers.d/lsi-monitor` after install.

## Environment Variables

| Variable         | Default                     | Description                            |
| ---------------- | --------------------------- | -------------------------------------- |
| `LSI_DATA_DIR`   | `./data`                    | CSV data directory                     |
| `STORCLI_PATH`   | `./storcli64`               | storcli64 binary path                  |
| `LSI_CONTROLLER` | `/c0`                       | Controller ID                          |
| `LSI_PYTHON`     | `python3`                   | Python interpreter (for shell scripts) |
| `LSI_USER`       | current user                | User for cron/sudoers                  |
| `ALERT_EMAIL_TO` | —                           | Immediate alert recipients (comma-separated, optional) |
| `SENDMAIL_PATH`  | `/usr/sbin/sendmail`        | Local sendmail binary path (optional)  |
| `SMTP_HOST`      | `smtp.example.com`          | SMTP server for daily report (optional)|
| `SMTP_PORT`      | `465`                       | SMTP port (SSL, optional)              |
| `SMTP_USER`      | —                           | SMTP login (optional)                  |
| `SMTP_PASS`      | —                           | SMTP password (optional)               |
| `SMTP_FROM`      | —                           | Sender address (optional)              |
| `SMTP_TO`        | same as FROM                | Recipient (optional)                   |
| `TEMP_WARN`      | `45`                        | Temperature warning threshold (°C)     |
| `TEMP_CRIT`      | `50`                        | Temperature critical threshold (°C)    |

## License

MIT
