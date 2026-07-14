#!/bin/bash
# ================================================
# LSI RAID 即时报告 — 采集最新数据 + 输出报告
# 用法: ./lsi_send_now.sh              → 先采集 + 过去24h报告
#       ./lsi_send_now.sh YYYY-MM-DD   → 指定日期全天报告
# 说明: 报告会直接打印到终端并保存为 HTML；邮件发送为可选项，
#       仅在配置了 SMTP_USER 等环境变量时才会发送。
# ================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${LSI_PYTHON:-python3}"
COLLECTOR="$SCRIPT_DIR/lsi_collectd.py"
REPORTER="$SCRIPT_DIR/lsi_report.py"

if [ -n "$1" ]; then
    echo "Generating report for $1 ..."
    exec "$PYTHON" "$REPORTER" "$1"
else
    echo "Collecting latest data ..."
    sudo "$PYTHON" "$COLLECTOR" 2>&1 || echo "   (collector finished)"

    echo "Generating 24h report ..."
    exec "$PYTHON" "$REPORTER"
fi
