"""Tests for the new self-healing + self-learning layer.

Covers:
- core/errors.py       classification of real-world error strings
- core/health.py       page-level health report shape
- core/recovery.py     action dispatcher
- core/behavior.py     humanizer scales work without exceptions
- core/hooks.py        register/emit lifecycle
- core/pacing.py       adaptive pacer allow/record
- self_learn/stats.py  event record + aggregations
- self_learn/optimizer recommend() shape
- self_learn/risk.py   risk detection triggers
- extension/registry.py registration / lookup
- intelligence/report.py insight generator (no LLM path)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))


def t(name):
    """Decorator that prints test name + result, returns result."""
    def wrap(fn):
        def inner(*a, **kw):
            try:
                out = fn(*a, **kw)
                print(f"  ✅  {name}")
                return out
            except Exception as e:
                print(f"  ❌  {name}: {e}")
                raise
        return inner
    return wrap


class _Tmp:
    """Tiny tmpdir context manager so we don't depend on pytest fixtures."""
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="mx_test_")
        return Path(self.dir)

    def __exit__(self, *exc):
        import shutil
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# errors.py
# ---------------------------------------------------------------------------

@t("errors.classify: ban signal → ACCOUNT+ESCALATE")
def test_classify_ban():
    from core.errors import classify, ErrorClass, Action
    c = classify("账号已被封禁，请联系客服")
    assert c.cls == ErrorClass.ACCOUNT
    assert c.action in (Action.ABORT_PLATFORM, Action.ESCALATE)
    assert c.is_ban_signal

@t("errors.classify: cookie expired → RECOVERABLE+RELOGIN")
def test_classify_cookie():
    from core.errors import classify, ErrorClass, Action
    c = classify("登录已过期，请重新登录")
    assert c.cls == ErrorClass.RECOVERABLE
    assert c.action == Action.RELOGIN

@t("errors.classify: rate limited → ACCOUNT+COOL_DOWN")
def test_classify_rate():
    from core.errors import classify, ErrorClass, Action
    c = classify("今日已达上限")
    assert c.cls == ErrorClass.ACCOUNT
    assert c.action == Action.COOL_DOWN
    assert c.cooldown_s > 0

@t("errors.classify: too short → CONTENT+SKIP_VIDEO")
def test_classify_too_short():
    from core.errors import classify, ErrorClass, Action
    c = classify("视频时长不足 5 秒")
    assert c.cls == ErrorClass.CONTENT
    assert c.action == Action.SKIP_VIDEO

@t("errors.classify: 5xx → PLATFORM+RETRY")
def test_classify_5xx():
    from core.errors import classify, ErrorClass, Action
    c = classify("502 Bad Gateway")
    assert c.cls == ErrorClass.PLATFORM
    assert c.action == Action.RETRY

@t("errors.classify: connection closed → TRANSIENT+ABORT_PLATFORM")
def test_classify_fatal_conn():
    from core.errors import classify, ErrorClass, Action
    c = classify("Target page, context or browser has been closed")
    assert c.cls == ErrorClass.TRANSIENT
    assert c.is_fatal_connection
    assert c.action == Action.ABORT_PLATFORM

@t("errors.classify: blocked status → SKIP_VIDEO")
def test_classify_blocked():
    from core.errors import classify, ErrorClass, Action
    c = classify("anything", status="blocked")
    assert c.cls == ErrorClass.CONTENT
    assert c.action == Action.SKIP_VIDEO

@t("errors.classify: ok status → no-op")
def test_classify_ok():
    """A successful publish is not a transient error.

    This used to assert TRANSIENT, which is how successes ended up recorded
    with cls="transient" and got counted by every error aggregate.
    """
    from core.errors import Action, ErrorClass, classify
    c = classify("anything", status="ok")
    assert c.cls == ErrorClass.OK
    assert c.severity == 0
    assert c.action is Action.NONE


# ---------------------------------------------------------------------------
# health.py
# ---------------------------------------------------------------------------

@t("health: PLATFORM_HOSTS has all four platforms")
def test_health_hosts():
    from core.health import PLATFORM_HOSTS
    for p in ("douyin", "kuaishou", "weixin", "xhs"):
        assert p in PLATFORM_HOSTS
        assert len(PLATFORM_HOSTS[p]) >= 1


@t("health: check_network returns bool")
def test_health_network():
    from core.health import check_network
    r = check_network("douyin", timeout_s=1.5)
    assert isinstance(r, bool)


# ---------------------------------------------------------------------------
# recovery.py
# ---------------------------------------------------------------------------

@t("recovery: perform_recovery returns RecoveryResult")
def test_recovery_dispatch():
    from core.recovery import perform_recovery
    from core.errors import Action
    r = perform_recovery(Action.SKIP_VIDEO, detail="test")
    assert r.succeeded
    assert r.action == Action.SKIP_VIDEO

@t("recovery: perform_recovery ESCALATE → requires_human")
def test_recovery_escalate():
    from core.recovery import perform_recovery
    from core.errors import Action
    r = perform_recovery(Action.ESCALATE, detail="captcha")
    assert r.requires_human


# ---------------------------------------------------------------------------
# behavior.py
# ---------------------------------------------------------------------------

@t("behavior: free helpers no-op under MX_HUMAN=0")
def test_behavior_no_page():
    os.environ["MX_HUMAN"] = "0"
    from core.behavior import jitter_sleep, micro_pause, between_platforms_sleep
    jitter_sleep(0.1)
    micro_pause(10, 20)
    between_platforms_sleep(0.01, 0.02)


# ---------------------------------------------------------------------------
# hooks.py
# ---------------------------------------------------------------------------

@t("hooks: register + emit (function form)")
def test_hooks_register():
    from core.hooks import Hooks
    Hooks.clear()

    def listener(**kw):
        return kw.get("x")

    Hooks.register("test:event", listener)
    results = Hooks.emit("test:event", x=42)
    assert results == [42]
    Hooks.clear("test:event")


@t("hooks: failing listener doesn't break the chain")
def test_hooks_failure_isolation():
    from core.hooks import Hooks
    Hooks.clear()

    def bad(**kw):
        raise RuntimeError("boom")

    def good(**kw):
        return "ok"

    Hooks.register("test:event2", bad)
    Hooks.register("test:event2", good)
    results = Hooks.emit("test:event2")
    assert "ok" in results
    Hooks.clear("test:event2")


# ---------------------------------------------------------------------------
# pacing.py
# ---------------------------------------------------------------------------

@t("media: missing_media_reason detects unusable sources")
def test_missing_media_reason():
    from core.media import missing_media_reason
    with _Tmp() as tmp:
        good = tmp / "v.mp4"
        good.write_bytes(b"x" * 16)
        assert missing_media_reason(str(good)) == ""
        assert missing_media_reason("") == "empty_path"
        assert missing_media_reason(str(tmp / "nope.mp4")) == "not_a_file"
        assert missing_media_reason(str(tmp)) == "not_a_file"
        empty = tmp / "empty.mp4"
        empty.write_bytes(b"")
        assert missing_media_reason(str(empty)) == "zero_bytes"

@t("errors: unreadable media is transient, not terminal")
def test_media_unreadable_is_transient():
    """A path the browser cannot open says nothing about the video.

    A long-lived browser with a stale view of an external volume produced
    ENOENT for files the shell could open. Scoring that unknown/retry caused a
    5s retry loop; scoring it terminal would permanently skip 25 good videos.
    """
    from core.errors import ErrorClass, classify
    for text in ("upload: [Errno 2] No such file or directory: '/v/a.mp4'",
                 "file_missing:not_a_file",
                 "FileNotFoundError",
                 "视频文件不存在"):
        c = classify(text, status="fail")
        assert c.cls is ErrorClass.TRANSIENT, text
        assert c.label == "media_unreadable", text
        assert c.cooldown_s >= 30, text

    # It must not swallow the neighbouring diagnoses.
    assert classify("upload: Locator.set_input_files: Timeout 30000ms exceeded",
                    status="fail").label == "timeout"

@t("pacing: allow on fresh state")
def test_pacing_fresh():
    from core.pacing import PacingController
    p = PacingController(["douyin"])
    allow, reason = p.allow("douyin")
    assert allow is True

@t("pacing: record+cool_down blocks next publish")
def test_pacing_cool_down():
    from core.pacing import PacingController
    p = PacingController(["douyin"])
    p.cool_down("douyin", 60.0)
    allow, reason = p.allow("douyin")
    assert allow is False
    assert "cool" in reason

@t("pacing: burst limit rests then resumes instead of locking out")
def test_pacing_burst_limit_resumes_after_rest():
    """Regression: hitting the burst limit must arm a rest, not veto forever.

    force_burst_rest() had no caller, so burst_count was never cleared and a
    platform that reached its burst limit stayed blocked for the whole run.
    """
    from core.pacing import PacingController
    p = PacingController(["douyin"])
    pp = p.get("douyin")
    pp.min_interval_s = 0.0      # isolate the burst rule
    pp.burst_rest_s = 60.0

    for _ in range(pp.burst_limit):
        allow, _reason = p.allow("douyin")
        assert allow is True
        p.record("douyin", True)

    # The next attempt is refused, but it arms the rest rather than deadlocking.
    allow, reason = p.allow("douyin")
    assert allow is False
    assert "burst_limit_reached" in reason
    assert pp.burst_count == 0
    assert pp.cool_down_until > time.time()

    # While resting, every further attempt stays refused.
    allow, reason = p.allow("douyin")
    assert allow is False
    assert "cool_down" in reason

    # Once the rest window has elapsed, publishing resumes.
    pp.cool_down_until = 0.0
    pp.last_publish_ts = 0.0
    allow, reason = p.allow("douyin")
    assert allow is True
    assert reason == "ok"

@t("pacing: tuned parameters survive a save/load roundtrip")
def test_pacing_tuned_params_persist():
    """Regression: burst_limit was persisted but never restored on load."""
    import tempfile as _tf
    from core.pacing import PacingController
    with _tf.TemporaryDirectory() as d:
        f = Path(d) / "pacing.json"
        p1 = PacingController(["douyin"])
        p1.update_params("douyin", min_interval_s=42.0, burst_limit=17,
                         burst_rest_s=99.0, fail_rate_backoff=0.25,
                         low_rate_rest_s=123.0)
        p1.save(f)

        pp2 = PacingController.load(f, ["douyin"]).get("douyin")
        assert pp2.min_interval_s == 42.0
        assert pp2.burst_limit == 17
        assert pp2.burst_rest_s == 99.0
        assert pp2.fail_rate_backoff == 0.25
        assert pp2.low_rate_rest_s == 123.0

@t("pacing: low success rate rests then probes instead of locking out")
def test_pacing_low_success_rate_probes_after_rest():
    """Regression: a tripped success rate used to veto the platform forever.

    The rolling window only changes when a publish is recorded, and the rule
    prevented any publish from being recorded, so the queue could never drain.
    """
    from core.pacing import PacingController
    p = PacingController(["douyin"])
    pp = p.get("douyin")
    pp.min_interval_s = 0.0
    pp.burst_limit = 999
    pp.low_rate_rest_s = 1800.0
    for _ in range(6):
        p.record("douyin", False)

    # Tripped, and it arms a real cool-down rather than silently refusing.
    allow, reason = p.allow("douyin")
    assert allow is False
    assert "success_rate_low" in reason
    assert pp.cool_down_until > time.time()

    # Still resting.
    allow, reason = p.allow("douyin")
    assert allow is False
    assert "cool_down" in reason

    # Once the window closes exactly one probe is granted...
    pp.cool_down_until = 0.0
    allow, reason = p.allow("douyin")
    assert allow is True
    assert "low_rate_probe" in reason

    # ...and a second attempt in the same window is refused again, so a broken
    # platform cannot be hammered between probes.
    pp.cool_down_until = 0.0
    allow, reason = p.allow("douyin")
    assert allow is False
    assert "success_rate_low" in reason

@t("pacing: low-success state survives a save/load roundtrip")
def test_pacing_low_rate_state_persists(tmp_path=None):
    import tempfile as _tf
    from core.pacing import PacingController
    with _tf.TemporaryDirectory() as d:
        f = Path(d) / "pacing.json"
        p1 = PacingController(["douyin"])
        pp1 = p1.get("douyin")
        pp1.min_interval_s = 0.0
        pp1.burst_limit = 999
        for _ in range(6):
            p1.record("douyin", False)
        p1.allow("douyin")          # arms the cool-down
        p1.save(f)

        p2 = PacingController.load(f, ["douyin"])
        pp2 = p2.get("douyin")
        assert pp2._low_rate_armed is True
        assert pp2.cool_down_until > 0
        # Window elapsed -> the reloaded controller grants the probe.
        pp2.cool_down_until = 0.0
        allow, _ = p2.allow("douyin")
        assert allow is True

@t("pacing: save+load roundtrip")
def test_pacing_persist():
    with _Tmp() as tmp:
        from core.pacing import PacingController
        f = tmp / "pacing.json"
        p1 = PacingController(["douyin"])
        p1.record("douyin", True)
        p1.record("douyin", True)
        p1.cool_down("douyin", 30)
        p1.save(f)
        p2 = PacingController.load(f, ["douyin"])
        assert p2.get("douyin").recent_results.count(1) == 2


# ---------------------------------------------------------------------------
# self_learn/stats.py
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path):
    from self_learn.stats import StatsStore
    return StatsStore(tmp_path / "events.jsonl")


@t("stats: record + summary")
def test_stats_record():
    with _Tmp() as tmp:
        s = _make_store(tmp)
        from self_learn.stats import Event
        for i in range(10):
            s.record(Event(
                ts=time.time(), platform="douyin", video_id=f"v{i}",
                status="ok" if i < 7 else "fail", method="cdp",
                duration_s=20.0, hour=12, weekday=0,
            ))
        rate = s.success_rate("douyin")
        assert abs(rate - 0.7) < 0.01


@t("stats: best_hours / worst_hours")
def test_stats_hours():
    with _Tmp() as tmp:
        s = _make_store(tmp)
        from self_learn.stats import Event
        for h, ok in [(10, True)] * 8 + [(22, False)] * 8:
            s.record(Event(
                ts=time.time(), platform="douyin", video_id="x",
                status="ok" if ok else "fail", method="cdp",
                duration_s=10, hour=h, weekday=0,
            ))
        best = s.best_hours("douyin", min_samples=2)
        worst = s.worst_hours("douyin", min_samples=2)
        assert best and best[0][0] == 10
        assert worst and worst[0][0] == 22


# ---------------------------------------------------------------------------
# self_learn/optimizer.py
# ---------------------------------------------------------------------------

@t("optimizer: recommend with insufficient samples returns defaults")
def test_optimizer_no_data():
    with _Tmp() as tmp:
        from self_learn.stats import StatsStore
        from self_learn.optimizer import StrategyOptimizer
        s = StatsStore(tmp / "events.jsonl")
        s.path.unlink(missing_ok=True)
        rec = StrategyOptimizer(s).recommend("douyin")
        assert rec.allow is True


# ---------------------------------------------------------------------------
# self_learn/risk.py
# ---------------------------------------------------------------------------

@t("risk: critical when success_rate low")
def test_risk_critical():
    with _Tmp() as tmp:
        s = _make_store(tmp)
        from self_learn.stats import Event
        for i in range(20):
            s.record(Event(
                ts=time.time(), platform="douyin", video_id=f"v{i}",
                status="fail", method="cdp", duration_s=10, hour=12,
                severity=4,
            ))
        from self_learn.risk import RiskMonitor
        rep = RiskMonitor(s).check("douyin")
        assert rep.level == "critical"
        assert rep.recommend_action.value in ("abort_platform", "cool_down")


@t("risk: triggers consecutive failures")
def test_risk_consecutive():
    with _Tmp() as tmp:
        s = _make_store(tmp)
        from self_learn.stats import Event
        s.record(Event(ts=time.time(), platform="douyin", video_id="v0",
                        status="ok", method="cdp", duration_s=10, hour=12))
        for i in range(4):
            s.record(Event(ts=time.time() + i, platform="douyin", video_id=f"v{i+1}",
                            status="fail", method="cdp", duration_s=10, hour=12))
        from self_learn.risk import RiskMonitor
        rep = RiskMonitor(s).check("douyin")
        assert rep.level in ("warn", "critical")


# ---------------------------------------------------------------------------
# extension/registry.py
# ---------------------------------------------------------------------------

@t("registry: register + lookup")
def test_registry_register():
    from extension.registry import Registry, register_builtin_platforms
    register_builtin_platforms()
    spec = Registry.get_platform("douyin")
    assert spec is not None
    assert spec.title_max == 30
    assert spec.module == "platforms.douyin"


@t("registry: active_platforms skips xhs by default")
def test_registry_active():
    from extension.registry import Registry, register_builtin_platforms
    register_builtin_platforms()
    active = Registry.active_platforms()
    assert "xhs" not in active
    assert "douyin" in active
    assert "kuaishou" in active
    assert "weixin" in active


# ---------------------------------------------------------------------------
# intelligence/report.py
# ---------------------------------------------------------------------------

@t("insights: generate report with no data")
def test_insights_empty():
    with _Tmp() as tmp:
        from self_learn.stats import StatsStore
        s = StatsStore(tmp / "events.jsonl")
        s.path.unlink(missing_ok=True)
        from intelligence.report import InsightsGenerator
        rep = InsightsGenerator(s).generate(["douyin", "kuaishou", "weixin"])
        assert rep.platforms
        md = rep.to_markdown()
        assert "mx-pub 智能洞察报告" in md


@t("insights: write_report persists both md and json")
def test_insights_write():
    with _Tmp() as tmp:
        from self_learn.stats import StatsStore
        s = StatsStore(tmp / "events.jsonl")
        s.path.unlink(missing_ok=True)
        from intelligence.report import InsightsGenerator, write_report
        rep = InsightsGenerator(s).generate(["douyin"])
        md_p, json_p = write_report(rep,
                                     md_path=str(tmp / "r.md"),
                                     json_path=str(tmp / "r.json"))
        assert Path(md_p).exists()
        assert Path(json_p).exists()
        assert Path(json_p).stat().st_size > 0


# ---------------------------------------------------------------------------
# intelligence/safety.py
# ---------------------------------------------------------------------------

@t("safety: heuristic blocks medical claim")
def test_safety_medical():
    from intelligence.safety import heuristic_score
    v = heuristic_score("神药根治高血压", "本药100%有效")
    assert v.allow is False
    assert any("medical" in f for f in v.flags)

@t("safety: heuristic blocks gambling content")
def test_safety_gambling():
    from intelligence.safety import heuristic_score
    v = heuristic_score("百家乐技巧", "稳赚不赔")
    assert v.allow is False
    assert any("gambling" in f for f in v.flags)

@t("safety: heuristic allows normal content")
def test_safety_ok():
    from intelligence.safety import heuristic_score
    v = heuristic_score("今天做了饭", "分享一个简单的菜谱")
    assert v.allow is True
    assert v.flags == []

@t("safety: predict() returns verdict with layer info")
def test_safety_predict():
    from intelligence.safety import predict
    # Don't use LLM since CI may have no key
    v = predict("测试标题", "测试描述", platform="douyin", use_llm=False)
    assert v.layer == "heuristic"
    assert 0.0 <= v.score <= 1.0


# ---------------------------------------------------------------------------
# browser.is_transient_error_smart
# ---------------------------------------------------------------------------

@t("browser: is_transient_error_smart agrees with legacy on simple cases")
def test_smart_transient():
    from core.browser import is_transient_error_smart
    # Both should be True for clear transient
    assert is_transient_error_smart("timeout 20000ms exceeded")
    # Ban signal should NOT be transient
    assert not is_transient_error_smart("账号已被封禁")


# ---------------------------------------------------------------------------
# batch_publish.py: smoke import (don't actually run)
# ---------------------------------------------------------------------------

@t("batch_publish: imports cleanly with next-gen enabled")
def test_batch_import():
    import importlib
    import importlib.util
    # batch_publish.py lives at the project root, not under scripts/.
    here = Path(__file__).resolve().parent.parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    spec = importlib.util.spec_from_file_location(
        "batch_publish", str(here / "batch_publish.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_HAS_NEXT_GEN")
    assert mod._HAS_NEXT_GEN is True
    assert hasattr(mod, "_record_event")
    assert hasattr(mod, "_maybe_risk_block")

if __name__ == "__main__":
    import traceback
    tests = [(name, obj) for name, obj in globals().items()
              if name.startswith("test_") and callable(obj)]
    failed = 0
    for _name, fn in tests:
        try:
            fn()
        except Exception:
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*60}\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
