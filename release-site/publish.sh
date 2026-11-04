#!/usr/bin/env bash
# Build a package and optionally sync the static release site to the hub.
set -Eeuo pipefail

SITE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="${DATAMAX_PROJECT_ROOT:-$(dirname -- "$SITE_DIR")}"
TARGET="${DATAMAX_RELEASE_TARGET:-}"
SSH_KEY="${DATAMAX_RELEASE_SSH_KEY:-}"
NOTES="${DATAMAX_RELEASE_NOTES:-正式发布}"

python3 "$SITE_DIR/build_release.py" \
  --source "$PROJECT_ROOT" \
  --site "$SITE_DIR" \
  --notes "$NOTES"

if [[ -z "$TARGET" ]]; then
  echo "发布包已生成。设置 DATAMAX_RELEASE_TARGET 后可自动同步，例如："
  echo "  DATAMAX_RELEASE_TARGET=root@hub.amax.xyz:/opt/lsi-full $0"
  exit 0
fi

SSH_ARGS=(-o StrictHostKeyChecking=accept-new)
if [[ -n "$SSH_KEY" ]]; then
  SSH_ARGS+=(-i "$SSH_KEY")
fi

rsync -az --chmod=D755,F644 \
  -e "ssh ${SSH_ARGS[*]}" \
  "$SITE_DIR/site/" "$TARGET/site/"
rsync -az --chmod=F644 \
  -e "ssh ${SSH_ARGS[*]}" \
  "$SITE_DIR/server.py" "$SITE_DIR/releases.json" \
  "$SITE_DIR"/datamax-lsi-*.tar.gz \
  "$TARGET/"
