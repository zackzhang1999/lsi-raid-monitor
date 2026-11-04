#!/usr/bin/env bash
# DataMax-LSI 一键安装 / 升级脚本
#
# 默认行为：
#   - 安装系统运行依赖（Debian/Ubuntu、RHEL 系和 openSUSE）
#   - 部署到 /opt/lsi-raid-monitor
#   - 在项目内创建独立 Python 虚拟环境
#   - 注册并启动 datamax-lsi.service
#   - 保留已有 data/、环境配置与用户数据
#
# 最简单的用法：
#   sudo bash install.sh
#
# 发布方可将 storcli64 放在本脚本同级、tools/ 下，或预装到系统，
# 安装器会自动接管；也支持 --storcli-source / STORCLI_SOURCE 指定文件。

set -Eeuo pipefail
IFS=$'\n\t'

APP_NAME="DataMax-LSI"
SERVICE_NAME="${DATAMAX_SERVICE_NAME:-datamax-lsi}"
INSTALL_DIR="${DATAMAX_INSTALL_DIR:-/opt/lsi-raid-monitor}"
DATA_DIR="${DATAMAX_DATA_DIR:-}"
ENV_FILE="${DATAMAX_ENV_FILE:-/etc/datamax-lsi.env}"
WEB_PORT="${DATAMAX_WEB_PORT:-${LSI_WEB_PORT:-5200}}"
WEB_HOST="${DATAMAX_WEB_HOST:-${LSI_WEB_HOST:-0.0.0.0}}"
COOKIE_SECURE="${DATAMAX_COOKIE_SECURE:-${LSI_COOKIE_SECURE:-0}}"
STORCLI_SOURCE="${DATAMAX_STORCLI_SOURCE:-${STORCLI_SOURCE:-}}"
STORCLI_PATH="${DATAMAX_STORCLI_PATH:-${STORCLI_PATH:-}}"
SKIP_SYSTEM_DEPS=0
SKIP_PIP=0
ALLOW_MISSING_STORCLI=0
INSTALL_EXTRAS="${DATAMAX_INSTALL_EXTRAS:-0}"
NO_START=0
CHECK_ONLY=0
INSTALL_DIR_EXPLICIT=0
DATA_DIR_EXPLICIT=0
WEB_PORT_EXPLICIT=0
WEB_HOST_EXPLICIT=0
COOKIE_SECURE_EXPLICIT=0
STORCLI_PATH_EXPLICIT=0

[[ -n "${DATAMAX_INSTALL_DIR:-}" ]] && INSTALL_DIR_EXPLICIT=1
[[ -n "${DATAMAX_DATA_DIR:-}" ]] && DATA_DIR_EXPLICIT=1
[[ -n "${DATAMAX_WEB_PORT:-}${LSI_WEB_PORT:-}" ]] && WEB_PORT_EXPLICIT=1
[[ -n "${DATAMAX_WEB_HOST:-}${LSI_WEB_HOST:-}" ]] && WEB_HOST_EXPLICIT=1
[[ -n "${DATAMAX_COOKIE_SECURE:-}${LSI_COOKIE_SECURE:-}" ]] && COOKIE_SECURE_EXPLICIT=1
[[ -n "${DATAMAX_STORCLI_PATH:-}${STORCLI_PATH:-}" ]] && STORCLI_PATH_EXPLICIT=1

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LEGACY_SERVICE="lsi-raid-web.service"
SERVICE_WAS_ACTIVE=0
LEGACY_WAS_ACTIVE=0
DEPLOY_STARTED=0
INSTALL_COMPLETE=0
STAGE_DIR=""
BACKUP_DIR=""
UNIT_BACKUP=""
ENV_BACKUP=""

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'
  C_INFO=$'\033[1;32m'
  C_WARN=$'\033[1;33m'
  C_ERROR=$'\033[1;31m'
  C_DIM=$'\033[2m'
else
  C_RESET=""
  C_INFO=""
  C_WARN=""
  C_ERROR=""
  C_DIM=""
fi

info() { printf '%s[+ ]%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
warn() { printf '%s[! ]%s %s\n' "$C_WARN" "$C_RESET" "$*" >&2; }
step() { printf '%s[· ]%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
fail() { printf '%s[x ]%s %s\n' "$C_ERROR" "$C_RESET" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
DataMax-LSI 一键安装脚本

用法:
  sudo bash install.sh [选项]

选项:
  --install-dir <路径>       安装目录，默认 /opt/lsi-raid-monitor
  --data-dir <路径>          数据目录，默认 <安装目录>/data
  --port <端口>              Web 端口，默认 5200
  --host <地址>              监听地址，默认 0.0.0.0
  --service-name <名称>      systemd 服务名，默认 datamax-lsi
  --storcli-source <文件>    安装指定 storcli64 文件
  --no-system-deps           不自动安装系统软件包
  --skip-pip                 跳过 Python 包安装（仅用于已有依赖的镜像）
  --allow-missing-storcli    未找到 storcli64 时仍继续安装
  --install-extras           同时安装 fio、bcache、NVMe、IPMI、XFS 等可选工具
  --no-start                 安装但不启动服务（发布镜像制作）
  --check                    只做环境和脚本检查，不修改系统
  -h, --help                 显示帮助

常用环境变量:
  DATAMAX_INSTALL_DIR, DATAMAX_DATA_DIR, DATAMAX_WEB_PORT
  DATAMAX_WEB_HOST, DATAMAX_SERVICE_NAME, STORCLI_PATH
  DATAMAX_ALLOW_MISSING_STORCLI=1
  DATAMAX_SKIP_SYSTEM_DEPS=1
  DATAMAX_INSTALL_EXTRAS=1

说明:
  storcli64 是 Broadcom 的专有工具，不由本项目重新分发。请将合法取得的
  storcli64 放在脚本目录、tools/ 下，或通过 --storcli-source 指定。
EOF
}

need_value() {
  [[ $# -ge 2 && -n "$2" ]] || fail "$1 缺少参数值"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      need_value "$1" "${2:-}"
      INSTALL_DIR="$2"
      INSTALL_DIR_EXPLICIT=1
      shift 2
      ;;
    --data-dir)
      need_value "$1" "${2:-}"
      DATA_DIR="$2"
      DATA_DIR_EXPLICIT=1
      shift 2
      ;;
    --port)
      need_value "$1" "${2:-}"
      WEB_PORT="$2"
      WEB_PORT_EXPLICIT=1
      shift 2
      ;;
    --host)
      need_value "$1" "${2:-}"
      WEB_HOST="$2"
      WEB_HOST_EXPLICIT=1
      shift 2
      ;;
    --service-name)
      need_value "$1" "${2:-}"
      SERVICE_NAME="$2"
      shift 2
      ;;
    --storcli-source)
      need_value "$1" "${2:-}"
      STORCLI_SOURCE="$2"
      shift 2
      ;;
    --no-system-deps)
      SKIP_SYSTEM_DEPS=1
      shift
      ;;
    --skip-pip)
      SKIP_PIP=1
      shift
      ;;
    --allow-missing-storcli)
      ALLOW_MISSING_STORCLI=1
      shift
      ;;
    --install-extras)
      INSTALL_EXTRAS=1
      shift
      ;;
    --no-start)
      NO_START=1
      shift
      ;;
    --check)
      CHECK_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "未知参数: $1（使用 --help 查看用法）"
      ;;
  esac
done

read_env_value() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1 | sed 's/^"//; s/"$//'
}

if [[ -f "$ENV_FILE" ]]; then
  if [[ "$DATA_DIR_EXPLICIT" -eq 0 ]]; then
    value="$(read_env_value LSI_DATA_DIR)"
    [[ -n "$value" ]] && DATA_DIR="$value"
  fi
  if [[ "$WEB_PORT_EXPLICIT" -eq 0 ]]; then
    value="$(read_env_value LSI_WEB_PORT)"
    [[ -n "$value" ]] && WEB_PORT="$value"
  fi
  if [[ "$WEB_HOST_EXPLICIT" -eq 0 ]]; then
    value="$(read_env_value LSI_WEB_HOST)"
    [[ -n "$value" ]] && WEB_HOST="$value"
  fi
  if [[ "$COOKIE_SECURE_EXPLICIT" -eq 0 ]]; then
    value="$(read_env_value LSI_COOKIE_SECURE)"
    [[ -n "$value" ]] && COOKIE_SECURE="$value"
  fi
  if [[ "$STORCLI_PATH_EXPLICIT" -eq 0 ]]; then
    value="$(read_env_value STORCLI_PATH)"
    [[ -n "$value" ]] && STORCLI_PATH="$value"
  fi
fi
[[ -n "$DATA_DIR" ]] || DATA_DIR="${INSTALL_DIR}/data"
[[ "$INSTALL_DIR" != "/" ]] && INSTALL_DIR="${INSTALL_DIR%/}"
[[ "$DATA_DIR" != "/" ]] && DATA_DIR="${DATA_DIR%/}"

[[ "$SERVICE_NAME" == *.service ]] || SERVICE_NAME="${SERVICE_NAME}.service"
[[ "$WEB_PORT" =~ ^[0-9]+$ ]] && (( WEB_PORT >= 1 && WEB_PORT <= 65535 )) \
  || fail "Web 端口无效: $WEB_PORT"
[[ "$WEB_HOST" =~ ^[A-Za-z0-9:.-]+$ ]] \
  || fail "Web 监听地址无效: $WEB_HOST"
[[ "$INSTALL_DIR" = /* && "$INSTALL_DIR" != *" "* ]] \
  || fail "安装目录必须是绝对路径且不能包含空格: $INSTALL_DIR"
[[ "$DATA_DIR" = /* && "$DATA_DIR" != *" "* ]] \
  || fail "数据目录必须是绝对路径且不能包含空格: $DATA_DIR"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] \
  || fail "systemd 服务名无效: $SERVICE_NAME"

[[ "$(id -u)" -eq 0 ]] || fail "请使用 root 或 sudo 运行: sudo bash install.sh"
command -v systemctl >/dev/null 2>&1 || fail "未找到 systemctl；一键安装需要 systemd"
[[ -d /run/systemd/system ]] || fail "当前系统没有运行 systemd"
command -v flock >/dev/null 2>&1 || fail "未找到 flock（通常由 util-linux 提供）"
if command -v mountpoint >/dev/null 2>&1 && mountpoint -q "$INSTALL_DIR" 2>/dev/null; then
  fail "安装目录是挂载点，无法执行安全切换: $INSTALL_DIR"
fi

exec 9>/run/datamax-lsi-install.lock
flock -n 9 || fail "另一个安装进程正在运行"

case "$(uname -m)" in
  x86_64|amd64) ;;
  *)
    warn "当前架构 $(uname -m) 可能没有可用的 Broadcom StorCLI；请确认兼容性"
    ;;
esac

has_pkg_manager() {
  command -v "$1" >/dev/null 2>&1
}

apt_install() {
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --no-upgrade \
    -o Acquire::Retries=3 "$@"
}

install_system_dependencies() {
  [[ "$SKIP_SYSTEM_DEPS" -eq 1 ]] && {
    info "已跳过系统软件包安装"
    return
  }

  info "检查并安装系统运行依赖"
  if has_pkg_manager apt-get; then
    if python3 -m venv --help >/dev/null 2>&1 \
      && command -v smartctl >/dev/null 2>&1 \
      && command -v curl >/dev/null 2>&1 \
      && python3 -c 'import ctypes.util; raise SystemExit(0 if ctypes.util.find_library("pam") else 1)' 2>/dev/null; then
      info "Python venv、smartctl、curl 与 PAM 已就绪"
    else
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq || warn "APT 索引更新失败，将尝试使用现有索引"
      apt_install python3 python3-venv python3-pip smartmontools pciutils util-linux \
        ca-certificates curl sudo libpam0g \
        || fail "基础系统依赖安装失败"
    fi
    if [[ "$INSTALL_EXTRAS" -eq 1 ]]; then
      apt-get update -qq || warn "APT 索引更新失败，可选依赖可能无法安装"
      for pkg in fio bcache-tools nvme-cli ipmitool e2fsprogs xfsprogs rsync zstd nfs-common; do
        apt_install "$pkg" >/dev/null 2>&1 || warn "可选软件包 $pkg 未安装，相关功能会有限制"
      done
    else
      info "可选扩展未安装；需要时使用 --install-extras 重跑，或在对应功能页按需安装"
    fi
  elif has_pkg_manager dnf; then
    dnf install -y --setopt=install_weak_deps=False python3 python3-pip smartmontools pciutils \
      util-linux ca-certificates curl sudo pam \
      || fail "基础系统依赖安装失败"
    if [[ "$INSTALL_EXTRAS" -eq 1 ]]; then
      for pkg in fio bcache-tools nvme-cli ipmitool e2fsprogs xfsprogs rsync zstd nfs-utils; do
        dnf install -y "$pkg" >/dev/null 2>&1 || warn "可选软件包 $pkg 未安装，相关功能会有限制"
      done
    fi
  elif has_pkg_manager yum; then
    yum install -y python3 python3-pip smartmontools pciutils util-linux ca-certificates \
      curl sudo pam \
      || fail "基础系统依赖安装失败"
    if [[ "$INSTALL_EXTRAS" -eq 1 ]]; then
      for pkg in fio bcache-tools nvme-cli ipmitool e2fsprogs xfsprogs rsync zstd nfs-utils; do
        yum install -y "$pkg" >/dev/null 2>&1 || warn "可选软件包 $pkg 未安装，相关功能会有限制"
      done
    fi
  elif has_pkg_manager zypper; then
    zypper --non-interactive install python3 python3-pip smartmontools pciutils util-linux \
      ca-certificates curl sudo pam \
      || fail "基础系统依赖安装失败"
    if [[ "$INSTALL_EXTRAS" -eq 1 ]]; then
      for pkg in fio bcache-tools nvme-cli ipmitool e2fsprogs xfsprogs rsync zstd nfs-client; do
        zypper --non-interactive install "$pkg" >/dev/null 2>&1 || warn "可选软件包 $pkg 未安装，相关功能会有限制"
      done
    fi
  else
    fail "无法识别系统软件包管理器；请先安装 Python 3.9+ 和依赖后使用 --no-system-deps"
  fi
}

resolve_storcli() {
  local candidate=""
  if [[ -n "$STORCLI_SOURCE" ]]; then
    [[ -f "$STORCLI_SOURCE" ]] || fail "storcli 源文件不存在: $STORCLI_SOURCE"
    if [[ "$CHECK_ONLY" -eq 1 ]]; then
      STORCLI_PATH="$STORCLI_SOURCE"
      info "StorCLI 源文件检查通过: $STORCLI_PATH"
    else
      install -m 755 "$STORCLI_SOURCE" /usr/local/bin/storcli64
      STORCLI_PATH="/usr/local/bin/storcli64"
      info "已安装 StorCLI: $STORCLI_PATH"
    fi
    return
  fi

  if [[ -n "$STORCLI_PATH" && -x "$STORCLI_PATH" ]]; then
    STORCLI_PATH="$(readlink -f "$STORCLI_PATH")"
  elif [[ -x "$INSTALL_DIR/storcli64" ]]; then
    STORCLI_PATH="$INSTALL_DIR/storcli64"
  elif [[ -x "$SRC_DIR/storcli64" ]]; then
    STORCLI_PATH="$INSTALL_DIR/storcli64"
    info "使用安装包内置 StorCLI: $STORCLI_PATH"
    return
  elif [[ -x "$SRC_DIR/tools/storcli64" ]]; then
    STORCLI_PATH="$INSTALL_DIR/tools/storcli64"
    info "使用安装包内置 StorCLI: $STORCLI_PATH"
    return
  else
    candidate="$(command -v storcli64 2>/dev/null || true)"
    [[ -n "$candidate" ]] && STORCLI_PATH="$(readlink -f "$candidate")"
  fi

  if [[ -z "$STORCLI_PATH" || ! -x "$STORCLI_PATH" ]]; then
    STORCLI_PATH="/usr/local/bin/storcli64"
    if [[ "$ALLOW_MISSING_STORCLI" -eq 1 ]]; then
      warn "未找到 storcli64，继续安装；RAID 采集与操作将不可用"
    else
      fail "未找到 storcli64。请将合法取得的 storcli64 放在项目根目录/tools，或使用 --storcli-source；确实需要先安装界面时可加 --allow-missing-storcli"
    fi
  else
    info "StorCLI: $STORCLI_PATH"
  fi
}

find_smartctl() {
  local candidate
  candidate="$(command -v smartctl 2>/dev/null || true)"
  [[ -n "$candidate" ]] || candidate="/usr/sbin/smartctl"
  [[ -x "$candidate" ]] || fail "未找到 smartctl；请安装 smartmontools 或使用 --no-system-deps 前先自行安装"
  printf '%s\n' "$(readlink -f "$candidate")"
}

port_owned_by_service() {
  systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null \
    || systemctl is-active --quiet "$LEGACY_SERVICE" 2>/dev/null
}

check_port() {
  command -v ss >/dev/null 2>&1 || return 0
  if ss -H -ltn "sport = :$WEB_PORT" 2>/dev/null | grep -q .; then
    port_owned_by_service \
      || fail "端口 $WEB_PORT 已被其他程序占用，请释放端口或使用 --port 指定新端口"
  fi
}

prepare_source() {
  local required
  for required in web_server.py requirements.txt web/templates/index.html; do
    [[ -e "$SRC_DIR/$required" ]] || fail "安装包缺少必要文件: $required"
  done
  if [[ "$SRC_DIR" == "$INSTALL_DIR" ]]; then
    warn "正在安装目录内执行，将从当前文件构建新版本快照"
  fi
}

validate_python() {
  local python_bin="$1"
  "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "Python 版本过低，需要 3.9 或更高"
  info "Python $("$python_bin" -c 'import platform; print(platform.python_version())')"
}

create_stage() {
  local parent
  parent="$(dirname -- "$INSTALL_DIR")"
  mkdir -p "$parent"
  STAGE_DIR="$(mktemp -d "${INSTALL_DIR}.install.XXXXXX")"
  info "构建安装快照: $STAGE_DIR"
  tar -C "$SRC_DIR" \
    --exclude='./.git' \
    --exclude='./.git/*' \
    --exclude='./.venv' \
    --exclude='./.venv/*' \
    --exclude='./data' \
    --exclude='./data/*' \
    --exclude='./charts' \
    --exclude='./charts/*' \
    --exclude='./dist' \
    --exclude='./dist/*' \
    --exclude='./.deploy-backups' \
    --exclude='./.deploy-backups/*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='storcli.log*' \
    --exclude='web_server.out' \
    --exclude='*.bak-*' \
    -cf - . | tar -C "$STAGE_DIR" -xf -
  chmod 755 "$STAGE_DIR/install.sh" "$STAGE_DIR/run.sh" 2>/dev/null || true
}

build_python_env() {
  local python_bin="$1"
  local pip_args="${DATAMAX_PIP_ARGS:-}"
  local -a pip_extra=()
  [[ -n "$pip_args" ]] && read -r -a pip_extra <<<"$pip_args"
  if [[ "$SKIP_PIP" -eq 1 ]]; then
    mkdir -p "$STAGE_DIR/.venv"
    warn "已跳过 Python 包安装；发布镜像必须确保 .venv 或系统 Python 依赖完整"
    return
  fi

  info "创建独立 Python 虚拟环境"
  "$python_bin" -m venv "$STAGE_DIR/.venv"
  [[ -x "$STAGE_DIR/.venv/bin/python" ]] || fail "Python venv 创建失败"
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
    "$STAGE_DIR/.venv/bin/python" -m pip install --no-cache-dir "${pip_extra[@]}" \
      -r "$STAGE_DIR/requirements.txt" \
    || fail "Python 依赖安装失败；请检查网络/PyPI，或通过 DATAMAX_PIP_ARGS 指定镜像"
  "$STAGE_DIR/.venv/bin/python" -c 'import flask, waitress' \
    || fail "Python 依赖校验失败（flask/waitress）"
}

validate_stage() {
  "$STAGE_DIR/.venv/bin/python" -m compileall -q "$STAGE_DIR" \
    || fail "Python 源码编译检查失败"
  [[ -f "$STAGE_DIR/web/templates/index.html" ]] || fail "页面模板缺失"
  [[ -f "$STAGE_DIR/web/static/js/app.js" ]] || fail "前端脚本缺失"
}

stop_existing_services() {
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    SERVICE_WAS_ACTIVE=1
    info "停止旧服务 $SERVICE_NAME"
    systemctl stop "$SERVICE_NAME"
  fi
  if [[ "$LEGACY_SERVICE" != "$SERVICE_NAME" ]] \
    && systemctl is-active --quiet "$LEGACY_SERVICE" 2>/dev/null; then
    LEGACY_WAS_ACTIVE=1
    info "停止旧版服务 $LEGACY_SERVICE"
    systemctl stop "$LEGACY_SERVICE"
  fi
}

rollback() {
  local rc=$?
  trap - EXIT ERR
  if [[ "$INSTALL_COMPLETE" -eq 1 ]]; then
    exit "$rc"
  fi
  if [[ "$DEPLOY_STARTED" -eq 1 ]]; then
    warn "安装失败，开始回滚"
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    if [[ -n "$UNIT_BACKUP" && -f "$UNIT_BACKUP" ]]; then
      install -m 644 "$UNIT_BACKUP" "/etc/systemd/system/$SERVICE_NAME"
    else
      rm -f "/etc/systemd/system/$SERVICE_NAME"
    fi
    if [[ -n "$ENV_BACKUP" && -f "$ENV_BACKUP" ]]; then
      install -m 600 "$ENV_BACKUP" "$ENV_FILE"
    else
      rm -f "$ENV_FILE"
    fi
    if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
      if [[ -d "$INSTALL_DIR/data" ]]; then
        rm -rf "$BACKUP_DIR/data"
        mv "$INSTALL_DIR/data" "$BACKUP_DIR/data"
      fi
      rm -rf "$INSTALL_DIR"
      mv "$BACKUP_DIR" "$INSTALL_DIR"
      BACKUP_DIR=""
    elif [[ -d "$INSTALL_DIR" ]]; then
      rm -rf "$INSTALL_DIR"
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
      systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$LEGACY_WAS_ACTIVE" -eq 1 ]]; then
      systemctl start "$LEGACY_SERVICE" >/dev/null 2>&1 || true
    fi
    warn "已恢复旧版本；数据目录未删除"
  fi
  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
    rm -rf "$STAGE_DIR"
  fi
  exit "$rc"
}
trap rollback EXIT

set_env_value() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  grep -v "^${key}=" "$ENV_FILE" >"$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  install -m 600 "$tmp" "$ENV_FILE"
  rm -f "$tmp"
}

deploy_release() {
  local timestamp previous_data_dir install_existed
  timestamp="$(date +%Y%m%d-%H%M%S)"
  previous_data_dir="$INSTALL_DIR/data"
  install_existed=0
  [[ -e "$INSTALL_DIR" ]] && install_existed=1

  if [[ "$DATA_DIR" != "$previous_data_dir" ]]; then
    mkdir -p "$DATA_DIR"
    chmod 700 "$DATA_DIR"
    chown root:root "$DATA_DIR"
  fi

  info "创建环境配置 $ENV_FILE"
  if [[ -f "$ENV_FILE" ]]; then
    ENV_BACKUP="/run/datamax-lsi-env.${timestamp}.bak"
    cp -a "$ENV_FILE" "$ENV_BACKUP"
    info "保留已有环境配置中的自定义项"
  fi
  if [[ ! -f "$ENV_FILE" || "${DATAMAX_OVERWRITE_ENV:-0}" == "1" ]]; then
    install -m 600 /dev/null "$ENV_FILE"
  fi
  set_env_value LSI_DATA_DIR "$DATA_DIR"
  set_env_value LSI_WEB_HOST "$WEB_HOST"
  set_env_value LSI_WEB_PORT "$WEB_PORT"
  set_env_value LSI_COOKIE_SECURE "$COOKIE_SECURE"
  set_env_value STORCLI_PATH "$STORCLI_PATH"
  set_env_value SMARTCTL_PATH "$SMARTCTL_PATH"
  set_env_value PATH "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

  info "切换应用文件"
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl stop "$LEGACY_SERVICE" >/dev/null 2>&1 || true
  if [[ "$install_existed" -eq 1 ]]; then
    BACKUP_DIR="${INSTALL_DIR}.backup-${timestamp}"
    mv "$INSTALL_DIR" "$BACKUP_DIR"
  fi
  mv "$STAGE_DIR" "$INSTALL_DIR"
  STAGE_DIR=""
  DEPLOY_STARTED=1

  if [[ "$DATA_DIR" == "$previous_data_dir" && -n "$BACKUP_DIR" && -d "$BACKUP_DIR/data" ]]; then
    rm -rf "$DATA_DIR"
    mv "$BACKUP_DIR/data" "$DATA_DIR"
  else
    mkdir -p "$DATA_DIR"
  fi
  chmod 700 "$DATA_DIR"
  chown -R root:root "$DATA_DIR"
}

write_unit() {
  local unit_path="/etc/systemd/system/$SERVICE_NAME"
  local unit_tmp
  unit_tmp="$(mktemp "/etc/systemd/system/.${SERVICE_NAME}.XXXXXX")"
  if [[ -f "$unit_path" ]]; then
    UNIT_BACKUP="/run/${SERVICE_NAME}.bak.$$"
    cp -a "$unit_path" "$UNIT_BACKUP"
  fi
  cat >"$unit_tmp" <<EOF
[Unit]
Description=$APP_NAME 存储管理系统
Documentation=file://$INSTALL_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=-$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/run.sh
Restart=always
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
LimitNOFILE=65536
TasksMax=512
UMask=0077
SyslogIdentifier=datamax-lsi

# 服务需要 root 访问 /dev、/sys、挂载、PAM 和 storcli。不要盲目添加
# NoNewPrivileges/ProtectSystem/ProtectDevices，以免控制面操作失效。

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "$unit_tmp"
  mv "$unit_tmp" "$unit_path"
  systemctl daemon-reload
}

health_check() {
  local probe_host="$WEB_HOST"
  local i
  [[ "$probe_host" == "0.0.0.0" ]] && probe_host="127.0.0.1"
  [[ "$probe_host" == "::" || "$probe_host" == "[::]" ]] && probe_host="::1"

  info "执行启动健康检查"
  for i in $(seq 1 120); do
    if systemctl is-active --quiet "$SERVICE_NAME" \
      && curl -fsS --max-time 3 "http://${probe_host}:${WEB_PORT}/" >/dev/null 2>&1; then
      info "Web 服务响应正常"
      return 0
    fi
    sleep 1
  done
  systemctl status "$SERVICE_NAME" --no-pager -n 40 || true
  journalctl -u "$SERVICE_NAME" --no-pager -n 60 || true
  return 1
}

show_summary() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  info "$APP_NAME 安装完成"
  printf '    访问地址:  http://%s:%s\n' "${ip:-<本机IP>}" "$WEB_PORT"
  printf '    安装目录:  %s\n' "$INSTALL_DIR"
  printf '    数据目录:  %s\n' "$DATA_DIR"
  printf '    环境配置:  %s\n' "$ENV_FILE"
  printf '    服务管理:  systemctl status|restart|stop %s\n' "$SERVICE_NAME"
  printf '    运行日志:  journalctl -u %s -f\n' "$SERVICE_NAME"
  if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
    printf '    旧版备份:  %s\n' "$BACKUP_DIR"
  fi
  echo
  echo "  首次访问请使用服务器本地 PAM 账号登录；root 默认具有管理员权限。"
}

info "$APP_NAME 安装器"
info "源目录: $SRC_DIR"
prepare_source
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  info "只读检查模式：不安装系统软件包"
  SKIP_SYSTEM_DEPS=1
else
  install_system_dependencies
fi

PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
[[ -n "$PYTHON_BIN" ]] || fail "未找到 python3"
validate_python "$PYTHON_BIN"
SMARTCTL_PATH="$(find_smartctl)"
resolve_storcli
check_port

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  info "环境检查通过（--check 未修改系统）"
  exit 0
fi

create_stage
build_python_env "$PYTHON_BIN"
validate_stage
stop_existing_services
deploy_release
write_unit

if [[ "$LEGACY_WAS_ACTIVE" -eq 1 ]]; then
  systemctl disable "$LEGACY_SERVICE" >/dev/null 2>&1 || true
fi
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1

if [[ "$NO_START" -eq 1 ]]; then
  info "已安装但未启动服务（--no-start）"
else
  systemctl start "$SERVICE_NAME"
  health_check || fail "服务健康检查失败"
fi

INSTALL_COMPLETE=1
trap - EXIT ERR
show_summary
