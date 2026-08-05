#!/usr/bin/env bash
# LSI RAID 监控一键启动脚本（生产模式，使用 waitress）
set -euo pipefail
cd "$(dirname "$0")"

export LSI_DATA_DIR="${LSI_DATA_DIR:-$PWD/data}"
export LSI_WEB_HOST="${LSI_WEB_HOST:-0.0.0.0}"
export LSI_WEB_PORT="${LSI_WEB_PORT:-5200}"

if python3 -c "import waitress" 2>/dev/null; then
    exec python3 -m waitress --host="$LSI_WEB_HOST" --port="$LSI_WEB_PORT" web_server:app
else
    echo "waitress 未安装，回退到 Flask 内置服务器（建议 pip3 install -r requirements.txt）" >&2
    exec python3 web_server.py
fi
