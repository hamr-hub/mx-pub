#!/usr/bin/env bash
# 采集快手数据 → _benchmark/stats/kuaishou_<date>.json
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python "$HERE/stats.py" --platform kuaishou "$@"
