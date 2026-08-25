#!/usr/bin/env bash
# 采集小红书数据 → _benchmark/stats/xiaohongshu_<date>.json
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python "$HERE/stats.py" --platform xiaohongshu "$@"
