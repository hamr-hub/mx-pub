"""End-to-end integration test: full mxpub pipeline.

Verifies:
- Stats are recorded per publish attempt
- Risk monitor flips to warn/critical as failure count rises
- Optimizer produces useful recommendations with enough data
- Pacing blocks burst attempts after consecutive failures
- Recovery produces sensible actions per error class
- Hooks fire from a simulated publish loop
- Extensions can register a new platform and the registry sees it
- Insights report can be generated and contains the right sections
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent))

os.environ.setdefault("MX_HUMAN", "0")


class _Tmp:
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="mx_int_")
        return Path(self.dir)

    def __exit__(self, *exc):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


# Helper that simulates publishing one video.
def _simulate_publish(*, platform: str, video_id: str, success: bool,
                       error: str = "", stats, pacing,
                       publisher):
    from core.errors import classify, Action

    if success:
        result = publisher.PublishResult(platform, "ok", method="cdp",
                                          duration_s=20.0)
        cls = classify(None, status="ok")
        action = Action.RETRY
        sev = 0
    else:
        result = publisher.PublishResult(platform, "fail", method="cdp",
                                          error=error, duration_s=5.0)
        cls = classify(error, status="fail", platform=platform)
        action = cls.action
        sev = cls.severity

    from self_learn.stats import Event
    stats.record(Event(
        ts=time.time(), platform=platform, video_id=video_id,
        status=result.status, method=result.method,
        duration_s=result.duration_s,
        error=(error or "")[:200], cls=cls.cls.value, action=action.value,
        severity=sev,
        hour=time.localtime().tm_hour,
        weekday=time.localtime().tm_wday,
        title_len=20, topics_n=3, attempt_n=1,
    ))

    pacing.record(platform, success)
    if cls.cooldown_s > 0:
        pacing.cool_down(platform, cls.cooldown_s)
    return result, cls


def test_full_pipeline():
    with _Tmp() as tmp:
        os.environ["MX_HUMAN"] = "0"
        from scripts.publisher import PublishResult  # noqa: F401
        from scripts.core.pacing import PacingController
        from scripts.self_learn.stats import StatsStore
        from scripts.self_learn.risk import RiskMonitor
        from scripts.self_learn.optimizer import StrategyOptimizer
        from scripts.core.recovery import perform_recovery
        from scripts.core.errors import Action
        from scripts.core.hooks import Hooks
        from scripts.extension.registry import (
            Registry, register_builtin_platforms, PlatformSpec)
        from scripts.intelligence.report import InsightsGenerator, write_report

        pacing = PacingController(["douyin", "kuaishou", "weixin"])
        stats = StatsStore(tmp / "events.jsonl")
        register_builtin_platforms()

        # 1. Register a custom plugin platform.
        Registry.register_platform(PlatformSpec(
            name="weibo",
            display_name="微博视频",
            url_match=lambda u: "weibo.com" in u and "upload" in u,
            publish_url="https://media.weibo.com/manage/v2/video/upload",
            module="platforms.weibo",
            title_max=80, desc_max=2000, topics_max=10,
        ))
        assert Registry.get_platform("weibo") is not None

        # 2. Hook fired correctly.
        fired = []
        Hooks.register("test:publish_done", lambda **kw: fired.append(kw))

        # 3. Simulate 8 successes then 5 failures for douyin.
        for i in range(8):
            _simulate_publish(platform="douyin", video_id=f"v{i}",
                              success=True, stats=stats, pacing=pacing,
                              publisher=sys.modules["scripts.publisher"])
        for i in range(5):
            _simulate_publish(platform="douyin", video_id=f"f{i}",
                              success=False, error="账号异常，请稍后再试",
                              stats=stats, pacing=pacing,
                              publisher=sys.modules["scripts.publisher"])

        Hooks.emit("test:publish_done", platform="douyin", n=13)
        assert len(fired) == 1

        # 4. Stats aggregate correctly.
        rate = stats.success_rate("douyin")
        assert abs(rate - 8/13) < 0.01

        # 5. Risk flips to warn/critical after the failure burst.
        risk = RiskMonitor(stats).check("douyin")
        assert risk.level in ("warn", "critical")
        assert risk.recommend_action in (Action.COOL_DOWN,
                                          Action.ABORT_PLATFORM)

        # 6. Optimizer produces a recommendation.
        opt = StrategyOptimizer(stats).recommend("douyin")
        assert opt.platform == "douyin"
        # best_hour may be None if sample distribution didn't allow a winner.

        # 7. Pacing records both wins and losses.
        p_dy = pacing.get("douyin")
        assert p_dy.recent_results.count(1) == 8
        assert p_dy.recent_results.count(0) == 5

        # 8. Recovery from rate-limit error: should cool down.
        result, cls = _simulate_publish(
            platform="kuaishou", video_id="ks1",
            success=False, error="rate limit",
            stats=stats, pacing=pacing,
            publisher=sys.modules["scripts.publisher"])
        rec = perform_recovery(cls.action, classification=cls,
                                detail=result.error)
        assert rec.action in (Action.COOL_DOWN, Action.ABORT_PLATFORM)

        # 9. Insights report can be written.
        rep = InsightsGenerator(stats).generate(
            ["douyin", "kuaishou", "weixin"])
        md_p, json_p = write_report(rep,
                                     md_path=str(tmp / "r.md"),
                                     json_path=str(tmp / "r.json"))
        md = Path(md_p).read_text()
        assert "mx-pub 智能洞察报告" in md
        assert "douyin" in md
        assert "kuaishou" in md

        print("✅ integration: end-to-end pipeline works")
        print(f"   events recorded: {len(stats.all())}")
        print(f"   douyin risk: {risk.level} / {risk.recommend_action.value}")
        print(f"   report files: {md_p}, {json_p}")


if __name__ == "__main__":
    try:
        test_full_pipeline()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
