#!/bin/bash
# ================================================
# LSI RAID Monitor — Cron 安装脚本（自包含版本）
# 用法: sudo bash setup_cron.sh
#
# 环境变量 (可提前 export 覆盖默认值):
#   LSI_DATA_DIR   - 数据存储目录 (默认 ./data)
#   LSI_PYTHON     - Python 解释器路径 (默认 python3)
#   LSI_USER       - 运行 cron 的系统用户 (默认当前用户)
#
# 邮件报警 (可选):
#   ALERT_EMAIL_TO - 报警收件人，多个地址用逗号分隔，依赖本地 sendmail
#   SENDMAIL_PATH  - sendmail 路径 (默认 /usr/sbin/sendmail)
# ================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${LSI_DATA_DIR:-$SCRIPT_DIR/data}"
PYTHON="${LSI_PYTHON:-python3}"
LSI_USER="${LSI_USER:-$SUDO_USER}"
LSI_USER="${LSI_USER:-$USER}"

COLLECTOR="$SCRIPT_DIR/lsi_collectd.py"
REPORTER="$SCRIPT_DIR/lsi_report.py"
STORCLI="${STORCLI_PATH:-$SCRIPT_DIR/storcli64}"

echo "=== LSI RAID Monitor Setup ==="
echo "  User:   $LSI_USER"
echo "  Data:   $DATA_DIR"
echo "  Storcli: $STORCLI"
echo "  Python: $PYTHON"
echo ""

# 1. 数据目录
mkdir -p "$DATA_DIR"
chown "$LSI_USER" "$DATA_DIR"
echo "[1/2] Data directory ready: $DATA_DIR"

# 2. sudoers 配置说明
SUDOERS_FILE="/etc/sudoers.d/lsi-monitor"
echo ""
echo "[2/2] sudoers configuration"
if [ -f "$STORCLI" ]; then
    echo ""
    echo "Since storcli64 is located at: $STORCLI"
    echo "You need to allow passwordless sudo for it."
    echo "Please run the following command as root to create the sudoers file:"
    echo ""
    echo "  echo \"$LSI_USER ALL=(root) NOPASSWD: $(realpath -m \"$STORCLI\")\" > $SUDOERS_FILE"
    echo "  echo \"$LSI_USER ALL=(root) NOPASSWD: /usr/sbin/smartctl\" >> $SUDOERS_FILE"
    echo "  chmod 440 $SUDOERS_FILE"
else
    echo ""
    echo "WARNING: storcli64 not found at $STORCLI"
    echo "Please copy storcli64 into $SCRIPT_DIR/ or set STORCLI_PATH."
fi

echo ""
echo "Then create the sudoers file manually, or run:"
echo ""
echo "  sudo visudo -f $SUDOERS_FILE"
echo ""
echo "Add these lines:"
echo "  $LSI_USER ALL=(root) NOPASSWD: $(realpath -m "$STORCLI")"
echo "  $LSI_USER ALL=(root) NOPASSWD: /usr/sbin/smartctl"

# 3. 安装 crontab
CRON_ENV=""
[ -n "$ALERT_EMAIL_TO" ] && CRON_ENV="${CRON_ENV}ALERT_EMAIL_TO=$ALERT_EMAIL_TO "
[ -n "$SENDMAIL_PATH" ] && CRON_ENV="${CRON_ENV}SENDMAIL_PATH=$SENDMAIL_PATH "

CRON_ENTRIES="
# LSI RAID Monitor — per-minute data collection
* * * * * LSI_DATA_DIR=$DATA_DIR ${CRON_ENV}cd $SCRIPT_DIR && $PYTHON $COLLECTOR >> $DATA_DIR/../collectd.log 2>&1

# LSI RAID Monitor — daily 10:00 report
0 10 * * * LSI_DATA_DIR=$DATA_DIR cd $SCRIPT_DIR && $PYTHON $REPORTER >> $DATA_DIR/../report.log 2>&1
"

echo ""
echo "[3/3] Installing crontab ..."
EXISTING=$(crontab -u "$LSI_USER" -l 2>/dev/null || true)
if echo "$EXISTING" | grep -q "lsi_collectd\|lsi_report"; then
    echo "$EXISTING" | grep -v "lsi_collectd\|lsi_report" | crontab -u "$LSI_USER" -
    EXISTING=$(crontab -u "$LSI_USER" -l 2>/dev/null || true)
fi
printf "%s\n%s\n" "$EXISTING" "$CRON_ENTRIES" | crontab -u "$LSI_USER" -

echo "  -> Crontab updated"
echo ""
echo "=== Done ==="
echo "Verify:"
echo "  sudo LSI_DATA_DIR=$DATA_DIR $PYTHON $COLLECTOR    # manual collection"
echo "  LSI_DATA_DIR=$DATA_DIR $PYTHON $REPORTER          # manual report"
echo "  crontab -l                                          # view cron jobs"
