#!/usr/bin/env bash
# ================================================
# 启动 LSI RAID Monitor Web 界面
#   bash start_web.sh
# 可选环境变量（web/app.py 中读取）：
#   FLASK_RUN_HOST  默认 127.0.0.1
#   FLASK_RUN_PORT  默认 5200
#   FLASK_DEBUG     默认 0，设为 1 开启调试
# ================================================
set -euo pipefail

cd "$(dirname "$0")"

exec python3 web/app.py "$@"
