#!/usr/bin/env bash
# 备份 LSI RAID 监控台的数据目录（配置、用户、密钥、事件、CSV）。
# 用法: sudo bash scripts/backup.sh [输出目录]
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${LSI_DATA_DIR:-$PROJECT_ROOT/data}"
OUT_DIR="${1:-$PROJECT_ROOT/backups}"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/lsi-backup-$TS"

mkdir -p "$OUT"

echo "备份数据目录: $DATA_DIR"
tar -C "$PROJECT_ROOT" -czf "$OUT/data.tgz" "${DATA_DIR#"$PROJECT_ROOT"/}"

{
  echo "created_at: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "host: $(hostname)"
  echo "data_dir: $DATA_DIR"
  if command -v git >/dev/null 2>&1 && git -C "$PROJECT_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
    echo "commit: $(git -C "$PROJECT_ROOT" rev-parse --short HEAD)"
  fi
} > "$OUT/metadata.txt"

echo "备份完成: $OUT"
echo "恢复方法: sudo bash scripts/restore.sh '$OUT/data.tgz'"
