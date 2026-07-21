#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="${1:-install}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
VENV_DIR="$PROJECT_DIR/.venv"
SUDOERS_FILE="/etc/sudoers.d/lsi-monitor"
CRON_BEGIN="LSI RAID Monitor BEGIN"
CRON_END="LSI RAID Monitor END"
DEFAULT_DATA_DIR="/var/lib/lsi-monitor/data"
DEFAULT_STORCLI_PATH="/usr/local/bin/storcli64"
DEFAULT_SMARTCTL_PATH="/usr/sbin/smartctl"
DEFAULT_CONTROLLER="/c0"

log() { printf '\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
err() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
die() { err "$*"; exit 1; }

need_root() {
  [ "${EUID:-$(id -u)}" -eq 0 ] || die "请使用 root 权限执行：sudo bash $0 $ACTION"
}

resolve_user() {
  if [ -n "${LSI_USER:-}" ]; then
    TARGET_USER="$LSI_USER"
  elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    TARGET_USER="$SUDO_USER"
  else
    TARGET_USER="$(id -un)"
  fi
}

load_env() {
  if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
  fi
}

prompt_value() {
  local name="$1" label="$2" default="$3" value=""
  read -r -p "$label [$default]: " value
  printf -v "$name" '%s' "${value:-$default}"
}

prompt_secret() {
  local name="$1" label="$2" current="${3:-}" value=""
  if [ -n "$current" ]; then
    read -r -s -p "$label [已配置，回车保持不变]: " value
    printf '\n'
    printf -v "$name" '%s' "${value:-$current}"
  else
    while [ -z "$value" ]; do
      read -r -s -p "$label: " value
      printf '\n'
    done
    printf -v "$name" '%s' "$value"
  fi
}

prompt_secret_optional() {
  local name="$1" label="$2" current="${3:-}" value=""
  if [ -n "$current" ]; then
    read -r -s -p "$label [已配置，回车保持不变，输入空格后回车可清空]: " value
    printf '\n'
    if [ "$value" = " " ]; then
      printf -v "$name" '%s' ""
    else
      printf -v "$name" '%s' "${value:-$current}"
    fi
  else
    read -r -s -p "$label: " value
    printf '\n'
    printf -v "$name" '%s' "$value"
  fi
}

check_project_files() {
  [ -f "$PROJECT_DIR/lsi_collectd.py" ] || die "未找到 lsi_collectd.py，请把本脚本放到项目根目录。"
  [ -f "$PROJECT_DIR/lsi_report.py" ] || die "未找到 lsi_report.py，请把本脚本放到项目根目录。"
  [ -f "$PROJECT_DIR/lsi_send_now.sh" ] || die "未找到 lsi_send_now.sh，请确认项目文件完整。"
}

install_system_dependencies() {
  log "检查并安装系统依赖"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip smartmontools cron
    systemctl enable --now cron >/dev/null 2>&1 || service cron start >/dev/null 2>&1 || true
  else
    warn "未检测到 apt-get，请手动确认 python3、python3-venv、pip、smartmontools、cron 已安装。"
  fi
}

find_binary() {
  local configured="$1" name="$2"
  if [ -x "$configured" ]; then
    printf '%s' "$configured"
  elif command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
  else
    printf '%s' "$configured"
  fi
}

configure_env() {
  STORCLI_PATH="$(find_binary "${STORCLI_PATH:-$DEFAULT_STORCLI_PATH}" storcli64)"
  SMARTCTL_PATH="$(find_binary "${SMARTCTL_PATH:-$DEFAULT_SMARTCTL_PATH}" smartctl)"
  prompt_value SMTP_HOST "SMTP 服务器" "${SMTP_HOST:-smtp.qq.com}"
  prompt_value SMTP_PORT "SMTP SSL 端口" "${SMTP_PORT:-465}"
  prompt_value SMTP_USER "SMTP 用户名/发件邮箱" "${SMTP_USER:-}"
  [ -n "$SMTP_USER" ] || die "SMTP 用户名不能为空。"
  prompt_secret SMTP_PASS "SMTP 授权码或密码" "${SMTP_PASS:-}"
  prompt_value SMTP_FROM "发件邮箱" "${SMTP_FROM:-$SMTP_USER}"
  prompt_value SMTP_TO "收件邮箱" "${SMTP_TO:-$SMTP_FROM}"
  prompt_value ALERT_EMAIL_TO "报警收件邮箱（多个用逗号分隔，留空则关闭即时报警）" "${ALERT_EMAIL_TO:-}"
  prompt_value SENDMAIL_PATH "sendmail 路径（留空使用 /usr/sbin/sendmail）" "${SENDMAIL_PATH:-/usr/sbin/sendmail}"
  prompt_value LSI_DATA_DIR "数据目录" "${LSI_DATA_DIR:-$DEFAULT_DATA_DIR}"
  prompt_value LSI_CONTROLLER "RAID 控制器编号" "${LSI_CONTROLLER:-$DEFAULT_CONTROLLER}"
  prompt_value STORCLI_PATH "storcli64 路径" "$STORCLI_PATH"
  prompt_value SMARTCTL_PATH "smartctl 路径" "$SMARTCTL_PATH"
  prompt_value TEMP_WARN "硬盘温度警告阈值" "${TEMP_WARN:-45}"
  prompt_value TEMP_CRIT "硬盘温度严重阈值" "${TEMP_CRIT:-50}"
  prompt_value REPORT_HOUR "每日报告小时" "${REPORT_HOUR:-10}"
  prompt_value REPORT_MINUTE "每日报告分钟" "${REPORT_MINUTE:-0}"
  prompt_secret_optional WEB_PASSWORD "Web 管理口令（留空=不启用认证，不启用时 Web 写操作无保护，请勿暴露到公网）" "${WEB_PASSWORD:-}"
}

write_env() {
  cat > "$ENV_FILE" <<ENVEOF
LSI_DATA_DIR="$LSI_DATA_DIR"
STORCLI_PATH="$STORCLI_PATH"
SMARTCTL_PATH="$SMARTCTL_PATH"
LSI_CONTROLLER="$LSI_CONTROLLER"
LSI_USER="$TARGET_USER"
SMTP_HOST="$SMTP_HOST"
SMTP_PORT="$SMTP_PORT"
SMTP_USER="$SMTP_USER"
SMTP_PASS="$SMTP_PASS"
SMTP_FROM="$SMTP_FROM"
SMTP_TO="$SMTP_TO"
ALERT_EMAIL_TO="$ALERT_EMAIL_TO"
SENDMAIL_PATH="$SENDMAIL_PATH"
TEMP_WARN="$TEMP_WARN"
TEMP_CRIT="$TEMP_CRIT"
REPORT_HOUR="$REPORT_HOUR"
REPORT_MINUTE="$REPORT_MINUTE"
WEB_PASSWORD="$WEB_PASSWORD"
ENVEOF
  chmod 600 "$ENV_FILE"
  chown "$TARGET_USER":"$TARGET_USER" "$ENV_FILE" 2>/dev/null || true
}

install_python_dependencies() {
  log "安装 Python 依赖"
  if [ ! -d "$VENV_DIR" ]; then
    sudo -u "$TARGET_USER" python3 -m venv "$VENV_DIR"
  fi
  sudo -u "$TARGET_USER" "$VENV_DIR/bin/python" -m pip install --upgrade pip
  if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    sudo -u "$TARGET_USER" "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
  else
    sudo -u "$TARGET_USER" "$VENV_DIR/bin/python" -m pip install matplotlib
  fi
}

install_sudoers() {
  log "配置免密 sudo"
  [ -x "$STORCLI_PATH" ] || warn "未确认 storcli64 可执行：$STORCLI_PATH"
  [ -x "$SMARTCTL_PATH" ] || warn "未确认 smartctl 可执行：$SMARTCTL_PATH"
  # 磁盘管理所需命令，路径随发行版不同，用 command -v 解析，找不到则回退到常见路径
  local mount_bin umount_bin mkfs_ext4_bin mkfs_xfs_bin mkdir_bin lsblk_bin
  mount_bin="$(find_binary /bin/mount mount)"
  umount_bin="$(find_binary /bin/umount umount)"
  mkfs_ext4_bin="$(find_binary /sbin/mkfs.ext4 mkfs.ext4)"
  mkfs_xfs_bin="$(find_binary /sbin/mkfs.xfs mkfs.xfs)"
  mkdir_bin="$(find_binary /bin/mkdir mkdir)"
  lsblk_bin="$(find_binary /bin/lsblk lsblk)"
  cat > "$SUDOERS_FILE" <<SUDOEOF
$TARGET_USER ALL=(root) NOPASSWD: $STORCLI_PATH
$TARGET_USER ALL=(root) NOPASSWD: $SMARTCTL_PATH
$TARGET_USER ALL=(root) NOPASSWD: $mount_bin
$TARGET_USER ALL=(root) NOPASSWD: $umount_bin
$TARGET_USER ALL=(root) NOPASSWD: $mkfs_ext4_bin
$TARGET_USER ALL=(root) NOPASSWD: $mkfs_xfs_bin
$TARGET_USER ALL=(root) NOPASSWD: $mkdir_bin
$TARGET_USER ALL=(root) NOPASSWD: $lsblk_bin
SUDOEOF
  chmod 440 "$SUDOERS_FILE"
  visudo -cf "$SUDOERS_FILE" >/dev/null
}

prepare_directories() {
  mkdir -p "$LSI_DATA_DIR" "$(dirname "$LSI_DATA_DIR")"
  chown -R "$TARGET_USER":"$TARGET_USER" "$(dirname "$LSI_DATA_DIR")" 2>/dev/null || true
  chmod +x "$PROJECT_DIR/lsi_send_now.sh" 2>/dev/null || true
}

install_cron() {
  log "安装定时任务"
  local python_bin="$VENV_DIR/bin/python"
  local collect_log="$(dirname "$LSI_DATA_DIR")/collectd.log"
  local report_log="$(dirname "$LSI_DATA_DIR")/report.log"
  local existing filtered cron_block
  existing="$(crontab -u "$TARGET_USER" -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | sed "/$CRON_BEGIN/,/$CRON_END/d")"
  cron_block="#$CRON_BEGIN
* * * * * bash -lc 'set -a; . \"$ENV_FILE\"; set +a; \"$python_bin\" \"$PROJECT_DIR/lsi_collectd.py\" >> \"$collect_log\" 2>&1'
$REPORT_MINUTE $REPORT_HOUR * * * bash -lc 'set -a; . \"$ENV_FILE\"; set +a; \"$python_bin\" \"$PROJECT_DIR/lsi_report.py\" >> \"$report_log\" 2>&1'
#$CRON_END"
  printf '%s\n%s\n' "$filtered" "$cron_block" | crontab -u "$TARGET_USER" -
}

remove_cron() {
  log "移除定时任务"
  local existing filtered
  existing="$(crontab -u "$TARGET_USER" -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | sed "/$CRON_BEGIN/,/$CRON_END/d")"
  printf '%s\n' "$filtered" | crontab -u "$TARGET_USER" -
}

show_status() {
  resolve_user
  load_env
  printf '项目目录: %s\n' "$PROJECT_DIR"
  printf '运行用户: %s\n' "$TARGET_USER"
  printf '配置文件: %s\n' "$ENV_FILE"
  printf '数据目录: %s\n' "${LSI_DATA_DIR:-$DEFAULT_DATA_DIR}"
  printf 'Python: %s\n' "$VENV_DIR/bin/python"
  printf '\n定时任务:\n'
  crontab -u "$TARGET_USER" -l 2>/dev/null | sed -n "/$CRON_BEGIN/,/$CRON_END/p" || true
  printf '\n日志文件:\n%s\n%s\n' "$(dirname "${LSI_DATA_DIR:-$DEFAULT_DATA_DIR}")/collectd.log" "$(dirname "${LSI_DATA_DIR:-$DEFAULT_DATA_DIR}")/report.log"
}

send_now() {
  need_root
  resolve_user
  load_env
  [ -f "$ENV_FILE" ] || die "未找到 .env，请先执行：sudo bash $0 install"
  log "立即采集并发送报告"
  sudo -u "$TARGET_USER" bash -lc "set -a; . '$ENV_FILE'; set +a; '$VENV_DIR/bin/python' '$PROJECT_DIR/lsi_collectd.py' || true; '$VENV_DIR/bin/python' '$PROJECT_DIR/lsi_report.py'"
}

run_install() {
  need_root
  resolve_user
  check_project_files
  load_env
  install_system_dependencies
  configure_env
  write_env
  install_python_dependencies
  prepare_directories
  install_sudoers
  install_cron
  log "安装完成。可执行 sudo bash $0 send-now 立即发送测试报告。"
}

run_uninstall() {
  need_root
  resolve_user
  load_env
  remove_cron
  rm -f "$SUDOERS_FILE"
  log "已卸载定时任务和 sudoers。数据目录、.env 和虚拟环境已保留。"
}

case "$ACTION" in
  install|start) run_install ;;
  stop) need_root; resolve_user; remove_cron ;;
  status) show_status ;;
  send-now) send_now ;;
  uninstall) run_uninstall ;;
  *) die "用法：sudo bash $0 {install|start|stop|status|send-now|uninstall}" ;;
esac
