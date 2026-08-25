#!/usr/bin/env bash
# 发布到小红书。cookie 过期时会暂停，让你手登。
# 用法：./scripts/publish_xiaohongshu.sh [--limit N] [--since YYYYMMDD] [--asset PATH] [--dry-run]
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

ARGS=(--platform xiaohongshu --limit "$LIMIT")
[[ -n "$SINCE" ]]   && ARGS+=(--since "$SINCE")
[[ -n "$ASSETS" ]]  && ARGS+=(--assets "$ASSETS")
[[ -n "$DRY" ]]     && ARGS+=("$DRY")

echo "▶ 小红书发布 workflow"
exec python "$HERE/publish_to_social.py" "${ARGS[@]}"
