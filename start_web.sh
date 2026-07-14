#!/bin/bash
# ================================================
# LSI RAID Monitor Web UI — 一键启动脚本（自包含版本）
# 用法: bash start_web.sh
#
# 环境变量 (可提前 export 覆盖默认值):
#   LSI_DATA_DIR     - 数据存储目录 (默认 ./data)
#   FLASK_RUN_HOST   - 监听地址 (默认 127.0.0.1)
#   FLASK_RUN_PORT   - 监听端口 (默认 5200)
#   FLASK_DEBUG      - 调试模式 (默认 0)
#   LSI_PYTHON       - Python 解释器路径 (默认 python3)
# ================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${LSI_PYTHON:-python3}"
APP="$SCRIPT_DIR/web/app.py"

HOST="${FLASK_RUN_HOST:-127.0.0.1}"
PORT="${FLASK_RUN_PORT:-5200}"
DEBUG="${FLASK_DEBUG:-0}"

# 默认使用项目目录下的 data 和 storcli64
LSI_DATA_DIR="${LSI_DATA_DIR:-$SCRIPT_DIR/data}"
STORCLI_PATH="${STORCLI_PATH:-$SCRIPT_DIR/storcli64}"

echo "=== LSI RAID Monitor Web UI ==="
echo "  Python:   $PYTHON"
echo "  App:      $APP"
echo "  Data:     $LSI_DATA_DIR"
echo "  Storcli:  $STORCLI_PATH"
echo "  Listen:   http://${HOST}:${PORT}"
echo ""

# 1. 检查 Python
if ! command -v "$PYTHON" > /dev/null 2>&1; then
    echo "[ERROR] Python interpreter not found: $PYTHON"
    exit 1
fi

# 2. 创建数据目录
mkdir -p "$LSI_DATA_DIR"

# 3. 如果项目目录下有 storcli64，确保可执行
if [ -f "$STORCLI_PATH" ]; then
    chmod +x "$STORCLI_PATH"
else
    echo "[WARNING] storcli64 not found at $STORCLI_PATH"
    echo "          Please copy storcli64 into $SCRIPT_DIR/ or set STORCLI_PATH"
fi

# 4. 安装依赖
echo "[1/2] Checking dependencies ..."
"$PYTHON" -m pip install -q -r "$SCRIPT_DIR/requirements.txt"

# 5. 检查 Flask 是否可用
if ! "$PYTHON" -c "import flask" 2> /dev/null; then
    echo "[ERROR] Flask installation failed"
    exit 1
fi

# 6. 启动 Flask
echo "[2/2] Starting Flask server ..."
echo ""
export LSI_DATA_DIR="$LSI_DATA_DIR"
export STORCLI_PATH="$STORCLI_PATH"
export FLASK_APP="$APP"
export FLASK_RUN_HOST="$HOST"
export FLASK_RUN_PORT="$PORT"
export FLASK_DEBUG="$DEBUG"

exec "$PYTHON" "$APP"
