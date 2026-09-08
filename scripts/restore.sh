#!/usr/bin/env bash
# 从备份恢复数据目录。恢复前会自动把当前 data/ 打包保存为
# data.pre-restore-<时间戳>.tgz，便于误操作时回退。
# 用法: sudo bash scripts/restore.sh /path/to/data.tgz
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${LSI_DATA_DIR:-$PROJECT_ROOT/data}"
BACKUP="${1:-}"

if [ -z "$BACKUP" ]; then
  echo "用法: sudo bash scripts/restore.sh /path/to/data.tgz" >&2
  exit 2
fi
if [ ! -f "$BACKUP" ]; then
  echo "找不到备份文件: $BACKUP" >&2
  exit 2
fi

TS="$(date +%Y%m%d-%H%M%S)"
PRE="$PROJECT_ROOT/data.pre-restore-$TS.tgz"
if [ -d "$DATA_DIR" ]; then
  echo "先备份当前数据目录到: $PRE"
  tar -C "$PROJECT_ROOT" -czf "$PRE" "${DATA_DIR#"$PROJECT_ROOT"/}"
fi

mkdir -p "$DATA_DIR"
tar -C "$PROJECT_ROOT" -xzf "$BACKUP"
chmod 600 "$DATA_DIR/.secret_key" 2>/dev/null || true
chmod 600 "$DATA_DIR/users.json" 2>/dev/null || true

echo "恢复完成。请重启服务使配置生效。"
echo "如需回退: sudo bash scripts/restore.sh '$PRE'"
