#!/usr/bin/env python3
"""mxpub — AI-driven multi-platform publisher (single entrypoint).

This is the next-generation entrypoint that wraps the existing
batch_publish.py logic with the new self-healing, self-learning and
extensibility layers:

  • core/errors.py      — error classification & recommended action
  • core/recovery.py    — auto-recovery (refresh, re-login, cool-down)
  • core/health.py      — pre-flight health gate (cookie, captcha, limits)
  • core/behavior.py    — human-like behavior (anti-ban jitter)
  • core/hooks.py       — extension hook system
  • core/pacing.py      — adaptive pacing (avoid bursts, learn from history)
  • self_learn/*        — record every attempt, learn best strategies
  • extension/registry  — pluggable platform / content providers

CLI:
    python3 mxpub.py publish                # run the full batch
    python3 mxpub.py publish --platform douyin --retry-failed
    python3 mxpub.py insights               # generate insights report
    python3 mxpub.py health                 # one-shot health check
    python3 mxpub.py stats                  # print recent stats summary
    python3 mxpub.py extensions             # list registered platforms

Environment:
    MX_HUMAN=0            — disable anti-ban jitter (CI mode)
    MX_INSIGHTS_MD=path   — custom insights markdown output path
    MX_INSIGHTS_MODEL=... — LLM model for insights
    MX_MIN_INTERVAL_S=... — minimum seconds between posts per platform
    MX_BURST_LIMIT=...    — max posts in a row before mandatory rest
    MX_FAIL_BACKOFF=...   — success-rate threshold for back-off
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from core.hooks import load_plugin_modules
from core.pacing import PacingController
from extension.registry import Registry, register_builtin_platforms, load_extensions
from self_learn.stats import StatsStore
from self_learn.risk import RiskMonitor
from intelligence.report import InsightsGenerator, write_report


QUEUE_FILE = HERE / "publish_queue.json"


def _ensure_setup() -> tuple[PacingController, StatsStore]:
    """Wire up the registries; called once per CLI invocation."""
    register_builtin_platforms()
    load_extensions()                # picks up scripts/extension/*.py
    load_plugin_modules(             # picks up *.py plugins from plugin dir
        os.environ.get("MX_PLUGINS_DIR",
                        str(HERE / "plugins")))

    pacing = PacingController.load()
    stats = StatsStore()
    return pacing, stats


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_publish(args) -> int:
    """Run the publish batch with the self-healing + self-learning wrapper.

    This is a thin, honest gate: it decides the platform list, then HANDS THAT
    LIST to the batch driver. It used to compute a gated list, print warnings
    about it, and then call batch_main() with no arguments — so the driver
    re-resolved platforms from the environment and `--platform douyin` quietly
    published to every platform. The risk gate and the batch hooks also ran
    here *and* inside the driver, double-firing every plugin event.
    """
    from batch_publish import main as batch_main
    pacing, stats = _ensure_setup()

    platforms = _resolve_platforms(args)
    if not platforms:
        print("❌ No platforms to publish to (after filters / skip).")
        return 1

    # Risk gate. batch_publish re-checks per platform (it must, since it is the
    # batch entrypoint on its own); doing it here too only duplicated work.
    if not args.force:
        risk = RiskMonitor(stats)
        blocked = []
        for p in platforms:
            rep = risk.check(p)
            if rep.level == "critical":
                print(f"⚠️  {p} 风险等级 critical：{'; '.join(rep.triggers)}")
                print("   -> 跳过（使用 --force 强制运行）")
                blocked.append(p)
            elif rep.cooldown_s > 0:
                print(f"⏳ {p} 建议冷却 {rep.cooldown_s:.0f}s（{rep.level}）")
        platforms = [p for p in platforms if p not in blocked]
    if not platforms:
        print("❌ 所有平台都被风控拦截。")
        return 2

    print(f"🎯 目标平台：{', '.join(platforms)}")
    try:
        # Pass the gated list down; batch_publish owns risk re-check, hooks,
        # pacing and telemetry so each concern fires exactly once.
        return batch_main(platforms, retry_failed=args.retry_failed,
                          force=args.force)
    finally:
        pacing.save()


def cmd_insights(args) -> int:
    pacing, stats = _ensure_setup()
    platforms = _resolve_platforms(args)
    rep = InsightsGenerator(stats).generate(platforms)
    md, jsonp = write_report(rep)
    print(f"📊 报告已写入：\n  md:   {md}\n  json: {jsonp}\n")
    print(rep.to_markdown())
    return 0


def cmd_health(args) -> int:
    """Probe each platform tab via raw CDP — works without owning the browser ws.

    A health check must never crash when the thing it checks is unhealthy, so
    an unreachable Chrome is reported as a finding (exit 1) instead of a raw
    URLError traceback.
    """
    from core.health import check_network
    from core.raw_cdp import CDP_HTTP, list_tabs

    pacing, stats = _ensure_setup()
    platforms = _resolve_platforms(args)
    print(f"🌍 健康检查 {platforms}  (cdp={CDP_HTTP})\n")

    try:
        tabs = {t.get("url", ""): t for t in list_tabs()}
    except Exception as e:
        print(f"  ❌ 无法连接 Chrome CDP ({CDP_HTTP}): {str(e)[:120]}")
        print("     请先以调试端口启动 Chrome，例如：")
        print('     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"'
              ' --remote-debugging-port=9222')
        return 1

    unhealthy = 0
    for plat in platforms:
        net = check_network(plat)
        spec = Registry.get_platform(plat)
        # Bind plat now: a late-bound closure over the loop variable would
        # bind the LAST platform for every earlier iteration.
        url_match = spec.url_match if spec else (lambda u, _p=plat: _p in u)
        target_url = spec.publish_url if spec else ""
        tab_url = next((u for u in tabs if url_match(u)), None)
        if not (net and tab_url):
            unhealthy += 1
        print(f"  {plat:9s} net={'✅' if net else '❌':2s}  "
              f"tab={'✅ ' + tab_url[:50] if tab_url else '❌ not open':50s}  "
              f"target={target_url[:50]}")
    return 1 if unhealthy else 0


def cmd_stats(args) -> int:
    pacing, stats = _ensure_setup()
    summary = stats.summary()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_extensions(args) -> int:
    pacing, stats = _ensure_setup()
    print("已注册平台：")
    for spec in Registry.list_platforms():
        skip = " (force_skip)" if spec.force_skip else ""
        print(f"  - {spec.name:10s} {spec.display_name:8s} → "
              f"{spec.module}{skip}")
    print("\n已注册内容生成器：")
    for p in Registry.list_content_providers():
        print(f"  - {p.name:20s} (priority={p.priority}, module={p.module})")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_platforms(args) -> list[str]:
    """Honor CLI flags, env, and registry defaults in priority order."""
    if getattr(args, "platform", None):
        return [args.platform]
    only = os.environ.get("ONLY_PLATFORMS", "").strip()
    if only:
        return [p.strip() for p in only.split(",") if p.strip()]
    skip = {p.strip() for p in os.environ.get("SKIP_PLATFORMS", "").split(",")
            if p.strip()}
    return Registry.active_platforms(extra_skip=skip)


def main():
    ap = argparse.ArgumentParser(
        description="mxpub — AI-driven multi-platform video publisher",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish", help="发布队列中的视频")
    p_pub.add_argument("--platform", help="只发到指定平台")
    p_pub.add_argument("--retry-failed", action="store_true",
                        help="重试上次失败的任务")
    p_pub.add_argument("--force", action="store_true",
                        help="忽略风控拦截")
    p_pub.set_defaults(fn=cmd_publish)

    p_ins = sub.add_parser("insights", help="生成 AI 洞察报告")
    p_ins.add_argument("--platform", help="只分析指定平台")
    p_ins.set_defaults(fn=cmd_insights)

    p_h = sub.add_parser("health", help="检查平台连通性")
    p_h.add_argument("--platform", help="只检查指定平台")
    p_h.set_defaults(fn=cmd_health)

    p_s = sub.add_parser("stats", help="显示历史统计")
    p_s.set_defaults(fn=cmd_stats)

    p_e = sub.add_parser("extensions", help="列出已注册的扩展")
    p_e.set_defaults(fn=cmd_extensions)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
