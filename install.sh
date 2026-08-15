#!/usr/bin/env bash
# ================================================
# LSI RAID 监控 一键安装脚本
#   - 安装到 /opt/lsi-raid-monitor（已存在则升级代码，保留 data/ 数据）
#   - 安装 Python 依赖（flask / waitress）
#   - 注册并启动 systemd 服务 lsi-raid-web.service
# 用法: sudo bash install.sh
# ================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/lsi-raid-monitor"
SERVICE_NAME="lsi-raid-web.service"
WEB_PORT="${LSI_WEB_PORT:-5200}"
PYTHON="$(command -v python3 || true)"

info() { echo -e "\033[1;32m[+]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }
fail() { echo -e "\033[1;31m[x]\033[0m $*" >&2; exit 1; }

# ---- 前置检查 ----
[ "$(id -u)" -eq 0 ] || fail "请使用 root 或 sudo 运行: sudo bash install.sh"
[ -n "$PYTHON" ] || fail "未找到 python3，请先安装 Python >= 3.9"
PY_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "Python 版本过低（当前 $PY_VER），需要 >= 3.9"
info "Python $PY_VER ($PYTHON)"

command -v systemctl >/dev/null || fail "未找到 systemctl，本脚本需要 systemd 系统"

if command -v storcli64 >/dev/null || [ -x /usr/local/bin/storcli64 ]; then
    info "storcli64 已就绪"
else
    warn "未找到 storcli64，请先安装 Broadcom StorCLI（默认路径 /usr/local/bin/storcli64，或用 STORCLI_PATH 指定）"
fi
command -v smartctl >/dev/null || warn "未找到 smartctl（smartmontools），SMART 数据将缺失"
command -v sudo >/dev/null || warn "未找到 sudo，采集器调用 storcli/smartctl 需要 sudo（root 下通常可用）"

# ---- 端口检查 ----
if command -v ss >/dev/null && ss -tln | awk '{print $4}' | grep -q ":${WEB_PORT}$"; then
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "服务 $SERVICE_NAME 已在运行，将执行升级并重启"
    else
        fail "端口 $WEB_PORT 已被其他程序占用，请先释放或设置 LSI_WEB_PORT 后重试"
    fi
fi

# ---- 拷贝项目文件（排除数据/日志/版本控制，保留已有 data/）----
info "安装项目到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
tar -C "$SRC_DIR" \
    --exclude='.git' \
    --exclude='./data' \
    --exclude='./charts' \
    --exclude='./dist' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='storcli.log*' \
    --exclude='web_server.out' \
    -cf - . | tar -C "$INSTALL_DIR" -xf -
mkdir -p "$INSTALL_DIR/data"
chmod 755 "$INSTALL_DIR/run.sh" 2>/dev/null || true

# ---- Python 依赖 ----
info "安装 Python 依赖（flask / waitress）"
PIP="$PYTHON -m pip"
if ! $PIP install -q -r "$INSTALL_DIR/requirements.txt"; then
    # 新发行版（PEP 668）禁止全局 pip 安装，使用 --break-system-packages 重试
    $PIP install -q --break-system-packages -r "$INSTALL_DIR/requirements.txt" \
        || fail "Python 依赖安装失败，请手动执行: $PIP install -r $INSTALL_DIR/requirements.txt"
fi

# ---- systemd 服务 ----
info "注册 systemd 服务 $SERVICE_NAME"
cat > "/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=LSI MegaRAID Monitor Web
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=LSI_DATA_DIR=$INSTALL_DIR/data
Environment=LSI_WEB_HOST=0.0.0.0
Environment=LSI_WEB_PORT=$WEB_PORT
ExecStart=$PYTHON -m waitress --host=0.0.0.0 --port=$WEB_PORT web_server:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
sleep 2
systemctl is-active --quiet "$SERVICE_NAME" || {
    systemctl status "$SERVICE_NAME" --no-pager -n 20 || true
    fail "服务启动失败，请检查: journalctl -u $SERVICE_NAME"
}

# ---- 完成 ----
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
info "安装完成，服务已启动并设为开机自启"
echo "    访问地址:  http://${IP:-<主机IP>}:$WEB_PORT"
echo "    服务管理:  systemctl status|restart|stop $SERVICE_NAME"
echo "    运行日志:  journalctl -u $SERVICE_NAME -f"
echo "    数据目录:  $INSTALL_DIR/data"
echo
echo "  提示: 首次访问请在“用户管理”页创建管理员账号。"
echo "        采集器由 Web 服务内置线程自动运行，无需配置 cron。"
