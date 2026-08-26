"""Cross-platform analytics report generator.

Reads JSON files written by stats.py from ``_benchmark/stats/`` and emits a
single markdown report at ``_benchmark/stats/REPORT_<date>.md`` that aggregates
per-platform metrics plus publish-attempt counters from ``publish_state.json``.

Usage
-----
    python scripts/report.py                            # today's snapshot
    python scripts/report.py --date 2026-08-26          # historical snapshot
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
STATS_DIR = PROJECT_ROOT / "_benchmark" / "stats"
STATE_FILE = PROJECT_ROOT / "publish_state.json"
PLATFORMS_JSON = HERE / "platforms.json"


def load_platforms(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_snapshot(date_str: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not STATS_DIR.exists():
        return out
    for p in STATS_DIR.glob(f"*_{date_str}.json"):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            out[payload.get("platform", p.stem.split("_")[0])] = payload
        except json.JSONDecodeError:
            continue
    return out


def load_state_summary() -> dict[str, dict[str, int]]:
    """Per-platform ok/fail counters from publish_state.json."""
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict[str, int]] = {}
    for entry in state.get("history", []):
        p = entry.get("platform")
        s = entry.get("status")
        if not p or not s:
            continue
        out.setdefault(p, {})[s] = out.get(p, {}).get(s, 0) + 1
    return out


def fmt_num(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if abs(v) >= 10_000:
            return f"{v/10_000:.1f}w"
        if abs(v) >= 1_000:
            return f"{v/1_000:.1f}k"
        if v == int(v):
            return str(int(v))
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def render_markdown(date_str: str, snapshot: dict[str, dict], state: dict[str, dict],
                    platforms_cfg: dict) -> str:
    lines: list[str] = []
    lines.append(f"# 跨平台发布数据报告 · {date_str}\n")
    lines.append(f"_Generated: {dt.datetime.now().isoformat(timespec='seconds')}_\n")

    # 1. Publish attempts
    lines.append("\n## 1. 发布尝试统计（来自 publish_state.json）\n")
    lines.append("| 平台 | ok | partial | fail | blocked | 总计 |")
    lines.append("|------|---:|--------:|-----:|--------:|-----:|")
    all_platforms = sorted(set(list(state.keys()) + list(snapshot.keys())))
    for p in all_platforms:
        s = state.get(p, {})
        total = sum(s.values())
        lines.append(f"| {platforms_cfg.get(p, {}).get('display_name', p)} "
                     f"| {s.get('ok', 0)} | {s.get('partial', 0)} "
                     f"| {s.get('fail', 0)} | {s.get('blocked', 0)} | {total} |")

    # 2. Per-platform metrics
    lines.append("\n## 2. 平台数据快照（来自 _benchmark/stats/）\n")
    if not snapshot:
        lines.append("_本日期无快照。先运行 `python scripts/stats.py --platform all` 采集。_\n")
    else:
        # Union of metric keys across platforms with data
        metric_keys: list[str] = []
        for payload in snapshot.values():
            for k in (payload.get("metrics") or {}).keys():
                if k not in metric_keys:
                    metric_keys.append(k)

        lines.append("| 指标 | " + " | ".join(
            platforms_cfg.get(p, {}).get("display_name", p) for p in snapshot) + " |")
        lines.append("|------|" + "|".join(":---:" for _ in snapshot) + "|")
        for k in metric_keys:
            row = [k]
            for p in snapshot:
                v = (snapshot[p].get("metrics") or {}).get(k)
                row.append(fmt_num(v))
            lines.append("| " + " | ".join(row) + " |")

    # 3. Per-platform details
    lines.append("\n## 3. 单平台详情\n")
    for p, payload in snapshot.items():
        lines.append(f"\n### {platforms_cfg.get(p, {}).get('display_name', p)} (`{p}`)\n")
        lines.append(f"- 采集时间: `{payload.get('captured_at')}`")
        lines.append(f"- 页面 URL: `{payload.get('page_url')}`")
        if payload.get("skipped"):
            lines.append("- ⚠ 未配置 analytics 块，跳过")
            continue
        if payload.get("error"):
            lines.append(f"- ⚠ 采集失败: `{payload['error']}`")
            continue
        m = payload.get("metrics") or {}
        if not m or all(v is None for v in m.values()):
            lines.append("- ⚠ 页面已加载但未匹配到任何字段（URL 可能已失效或页面结构变更）")
            continue
        lines.append("\n| 字段 | 值 | 原始 |")
        lines.append("|------|----:|------|")
        raw = payload.get("matched_raw") or {}
        for k in m.keys():
            lines.append(f"| `{k}` | {fmt_num(m[k])} | {raw.get(k, '')} |")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Aggregate analytics snapshots into one markdown report")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--platforms-json", default=str(PLATFORMS_JSON))
    args = ap.parse_args(argv)

    date_str = args.date or dt.date.today().isoformat()
    platforms_cfg = load_platforms(Path(args.platforms_json))
    snapshot = load_snapshot(date_str)
    state = load_state_summary()

    md = render_markdown(date_str, snapshot, state, platforms_cfg)
    out_path = STATS_DIR / f"REPORT_{date_str}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote → {out_path}\n")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())