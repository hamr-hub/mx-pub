#!/usr/bin/env bash
# 采集视频号数据 → _benchmark/stats/weixin_channels_<date>.json
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python "$HERE/stats.py" --platform weixin_channels "$@"
