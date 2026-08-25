#!/usr/bin/env bash
# 发布到微信视频号。
# 用法：./scripts/publish_weixin_channels.sh [--limit N] [--since YYYYMMDD] [--asset PATH] [--dry-run]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

LIMIT="1"
SINCE=""
ASSETS=""
DRY=""
for arg in "$@"; do
  case "$arg" in
    --limit=*)   LIMIT="${arg#*=}" ;;
    --limit)     shift; LIMIT="${1:-1}" ;;
    --since=*)   SINCE="${arg#*=}" ;;
    --since)     shift; SINCE="${1:-}" ;;
    --asset=*)   ASSETS="${arg#*=}" ;;
    --asset)     shift; ASSETS="${1:-}" ;;
    --dry-run)   DRY="--dry-run" ;;
    *) echo "忽略未知参数: $arg" ;;
  esac
done

ARGS=(--platform weixin_channels --limit "$LIMIT")
[[ -n "$SINCE" ]]   && ARGS+=(--since "$SINCE")
[[ -n "$ASSETS" ]]  && ARGS+=(--assets "$ASSETS")
[[ -n "$DRY" ]]     && ARGS+=("$DRY")

echo "▶ 视频号发布 workflow"
exec python "$HERE/publish_to_social.py" "${ARGS[@]}"
