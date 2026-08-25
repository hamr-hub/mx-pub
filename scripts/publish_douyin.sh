#!/usr/bin/env bash
# 发布到抖音。cookie 过期时会暂停，让你手登。
# 用法：./scripts/publish_douyin.sh [--limit N] [--since YYYYMMDD] [--asset PATH] [--dry-run]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

# 解析 --limit / --since / --asset / --dry-run 透传给 publish_to_social.py
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

ARGS=(--platform douyin --limit "$LIMIT")
[[ -n "$SINCE" ]]   && ARGS+=(--since "$SINCE")
[[ -n "$ASSETS" ]]  && ARGS+=(--assets "$ASSETS")
[[ -n "$DRY" ]]     && ARGS+=("$DRY")

echo "▶ 抖音发布 workflow"
echo "  limit=${LIMIT} since=${SINCE:-<none>} assets=${ASSETS:-<scan>} dry=${DRY:-<no>}"
exec python "$HERE/publish_to_social.py" "${ARGS[@]}" "$@"
