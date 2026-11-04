#!/usr/bin/env bash
# DataMax-LSI 前台启动脚本（waitress）
set -Eeuo pipefail
cd -- "$(dirname -- "$0")"

export LSI_DATA_DIR="${LSI_DATA_DIR:-$PWD/data}"
export LSI_WEB_HOST="${LSI_WEB_HOST:-0.0.0.0}"
export LSI_WEB_PORT="${LSI_WEB_PORT:-5200}"

if [[ -x "$PWD/.venv/bin/python" ]]; then
    PYTHON="$PWD/.venv/bin/python"
elif python3 -c "import flask, waitress" 2>/dev/null; then
    PYTHON="$(command -v python3)"
else
    echo "Python 依赖不完整。请运行 sudo bash install.sh 或 pip3 install -r requirements.txt" >&2
    exit 1
fi

exec "$PYTHON" -m waitress \
    --host="$LSI_WEB_HOST" \
    --port="$LSI_WEB_PORT" \
    --threads="${WAITRESS_THREADS:-8}" \
    web_server:app
