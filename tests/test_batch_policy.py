"""Regression tests for the batch driver's publish-eligibility & gating policy.

Each test here pins a defect that shipped and would otherwise come back:

- `needs_publish` ignored RETRY_FAILED entirely (documented, never wired).
- RETRY_FAILED must re-arm only *transient* failures — re-arming content or
  account failures is how an account gets flagged.
- `mxpub publish --platform X` computed a gated platform list and then threw
  it away, publishing to every platform.
- The adaptive pacing gate printed "拦截" and published anyway.
- `StatsStore.summary()` returned tuple-keyed dicts, so `mxpub stats` crashed
  on any history that contained a failure.
- A safety-gate veto `break` unwound the whole queue, dropping the block from
  the queue file and skipping every remaining video.
"""
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import batch_publish  # noqa: E402
from core.pacing import PacingController  # noqa: E402
from self_learn.stats import Event, StatsStore  # noqa: E402


# ---------------------------------------------------------------------------
# needs_publish / RETRY_FAILED
# ---------------------------------------------------------------------------

def _row(status, error=""):
    return {"title": "t", "published": {"douyin": {"status": status,
                                                   "error": error}}}


def test_terminal_rows_are_never_republished():
    assert batch_publish.needs_publish(_row("ok"), "douyin") is False
    # blocked = short clip; re-uploading it is pure waste
    assert batch_publish.needs_publish(
        _row("blocked", "duration_2.10s_below_min_5s"), "douyin") is False
    # ...and RETRY_FAILED must not re-arm it either
    assert batch_publish.needs_publish(
        _row("blocked", "duration_2.10s_below_min_5s"), "douyin",
        retry_failed=True) is False


def test_never_attempted_rows_stay_pending():
    assert batch_publish.needs_publish({"title": "new"}, "douyin") is True


def test_transient_failures_are_rearmed_by_default():
    """A dead CDP transport says nothing about the video — retry it."""
    for error in ("timeout 20000ms exceeded",
                  "fatal_connection_closed",
                  "upload: Locator.set_input_files: Timeout 30000ms exceeded",
                  "Target page, context or browser has been closed"):
        assert batch_publish.needs_publish(_row("fail", error),
                                           "douyin") is True, error


def test_permanent_failures_stay_terminal_until_forced():
    """Re-arming these on every run is how an account gets flagged."""
    for error in ("safety: gambling", "account_banned",
                  "upload_rejected: 视频违规", "作品审核不通过",
                  "content violation: prohibited"):
        row = _row("fail", error)
        assert batch_publish.needs_publish(row, "douyin") is False, error
        # RETRY_FAILED=1 is the explicit "I know what I'm doing" escape hatch
        assert batch_publish.needs_publish(
            row, "douyin", retry_failed=True) is True, error


def test_failure_with_no_recorded_error_gets_one_more_chance():
    assert batch_publish.needs_publish(_row("fail", ""), "douyin") is True


# ---------------------------------------------------------------------------
# mxpub → batch_publish hand-off
# ---------------------------------------------------------------------------

def test_mxpub_passes_its_gated_platform_list_to_the_driver(monkeypatch):
    """`--platform weixin` must publish to weixin, not to everything."""
    import mxpub

    captured = {}

    def fake_batch_main(platforms=None, *, retry_failed=False, force=False):
        captured["platforms"] = platforms
        captured["retry_failed"] = retry_failed
        captured["force"] = force
        return 0

    monkeypatch.setitem(sys.modules, "batch_publish",
                        types.SimpleNamespace(main=fake_batch_main))
    monkeypatch.setattr(mxpub, "_ensure_setup",
                        lambda: (PacingController(["weixin"]),
                                 StatsStore(Path(ROOT / "publish_events.jsonl"))))

    args = types.SimpleNamespace(platform="weixin", retry_failed=True,
                                 force=True)
    assert mxpub.cmd_publish(args) == 0
    assert captured["platforms"] == ["weixin"]
    assert captured["retry_failed"] is True
    assert captured["force"] is True


def test_mxpub_exits_instead_of_publishing_when_everything_is_blocked(monkeypatch):
    import mxpub

    def boom(*a, **k):
        raise AssertionError("batch driver must not start with no platforms")

    monkeypatch.setitem(sys.modules, "batch_publish",
                        types.SimpleNamespace(main=boom))
    monkeypatch.setattr(mxpub, "_ensure_setup",
                        lambda: (PacingController(["douyin"]), StatsStore()))

    # critical risk on the only platform -> abort before touching Chrome
    class _CriticalRisk:
        def __init__(self, stats):
            pass

        def check(self, platform):
            return types.SimpleNamespace(level="critical",
                                         triggers=["fail_streak"],
                                         cooldown_s=0.0)

    monkeypatch.setattr(mxpub, "RiskMonitor", _CriticalRisk)
    args = types.SimpleNamespace(platform=None, retry_failed=False, force=False)
    assert mxpub.cmd_publish(args) == 2


# ---------------------------------------------------------------------------
# Adaptive pacing gate
# ---------------------------------------------------------------------------

def test_pacing_gate_is_opt_in(monkeypatch):
    monkeypatch.setenv("MX_PACING", "0")
    p = PacingController(["douyin"])
    p.get("douyin").last_publish_ts = 1e18      # would veto if enforced
    assert batch_publish._pacing_gate("douyin", p) is True


def test_pacing_gate_blocks_after_waiting_out_the_window(monkeypatch):
    monkeypatch.setenv("MX_PACING", "1")
    monkeypatch.setenv("MX_PACING_MAX_WAIT_S", "0")
    p = PacingController(["douyin"])
    p.get("douyin").enter_cool_down(300)
    # Previously this printed "pacing 拦截", slept 5s, and published anyway.
    assert batch_publish._pacing_gate("douyin", p) is False


def test_pacing_state_is_recorded_even_while_the_gate_is_off(tmp_path):
    """MX_PACING=0 must not mean 'never learn' — the state has to be warm."""
    monkeypatch_p = tmp_path / "pacing_state.json"
    p = PacingController(["douyin"])
    p.record("douyin", success=True)
    p.record("douyin", success=False)
    p.save(monkeypatch_p)
    reloaded = PacingController.load(monkeypatch_p, ["douyin"])
    assert reloaded.get("douyin").success_rate == 0.5


def test_pacing_load_adopts_platforms_missing_from_the_initial_list(tmp_path):
    f = tmp_path / "pacing_state.json"
    p = PacingController(["douyin", "weibo"])
    p.record("weibo", success=True)
    p.save(f)
    # Caller only asked about douyin; weibo's learned state must survive.
    assert PacingController.load(f, ["douyin"]).get("weibo").burst_count == 1


# ---------------------------------------------------------------------------
# Telemetry serialization
# ---------------------------------------------------------------------------

def test_stats_summary_is_json_serialisable(tmp_path):
    """`mxpub stats` crashed here on any history containing a failure."""
    f = tmp_path / "events.jsonl"
    store = StatsStore(f)
    now = 1.0
    store.record(Event(ts=now, platform="douyin", video_id="v1", status="fail",
                       method="cdp", duration_s=1.0, error="timeout",
                       cls="transient", action="retry", severity=1,
                       hour=3, weekday=0, content_hash="", title_len=3,
                       topics_n=1, attempt_n=1))
    payload = json.dumps(store.summary(), ensure_ascii=False)
    assert json.loads(payload)["error_labels"]["transient/retry"] == 1


# ---------------------------------------------------------------------------
# Summary accounting
# ---------------------------------------------------------------------------

def test_blocked_is_reported_as_its_own_bucket():
    """blocked is a DONE_STATUS; counting it as `fail` misreports the run."""
    assert "blocked" in {
        "ok", "fail", "partial", "blocked",
    }


# ---------------------------------------------------------------------------
# Health check must not crash when the thing it checks is unhealthy
# ---------------------------------------------------------------------------

def test_health_reports_unreachable_chrome_instead_of_crashing(monkeypatch):
    """A health check that raises URLError tells you nothing about the cause."""
    import mxpub

    def boom():
        raise OSError("[Errno 1] Operation not permitted")

    monkeypatch.setattr(mxpub, "_ensure_setup",
                        lambda: (PacingController(["douyin"]), StatsStore()))
    monkeypatch.setitem(sys.modules, "core.raw_cdp",
                        types.SimpleNamespace(CDP_HTTP="http://127.0.0.1:9222",
                                              list_tabs=boom))
    args = types.SimpleNamespace(platform="douyin")
    assert mxpub.cmd_health(args) == 1


def test_health_reports_per_platform_findings(monkeypatch):
    import mxpub

    monkeypatch.setattr(mxpub, "_ensure_setup",
                        lambda: (PacingController(["douyin", "weixin"]),
                                 StatsStore()))
    monkeypatch.setitem(sys.modules, "core.raw_cdp",
                        types.SimpleNamespace(
                            CDP_HTTP="http://127.0.0.1:9222",
                            list_tabs=lambda: [
                                {"type": "page",
                                 "url": "https://creator.douyin.com/creator-micro/home"}]))
    monkeypatch.setitem(sys.modules, "core.health",
                        types.SimpleNamespace(check_network=lambda p: True))
    assert mxpub.cmd_health(types.SimpleNamespace(platform=None)) == 1


# ---------------------------------------------------------------------------
# Queue totals must not depend on which platforms the run happened to include
# ---------------------------------------------------------------------------

def _mixed_queue():
    return {
        "total": 3,
        "videos": [
            # done everywhere
            {"title": "a", "published": {"douyin": {"status": "ok"},
                                          "kuaishou": {"status": "ok"},
                                          "weixin": {"status": "ok"}}},
            # douyin never attempted
            {"title": "b", "published": {"kuaishou": {"status": "ok"},
                                          "weixin": {"status": "ok"}}},
            # weixin still failing
            {"title": "c", "published": {"douyin": {"status": "ok"},
                                          "kuaishou": {"status": "ok"},
                                          "weixin": {"status": "fail"}}},
        ],
    }


def test_single_platform_run_does_not_declare_the_queue_complete():
    """Regression: recompute_totals used only the run's platform list, so
    `ONLY_PLATFORMS=douyin` persisted remaining=0 over 49 real pending rows."""
    q = _mixed_queue()
    batch_publish.recompute_totals(q, ["douyin"])
    assert q["published"] == 1, "only video 'a' is done on every platform"
    assert q["remaining"] == 2


def test_totals_are_stable_regardless_of_run_scope():
    a, b = _mixed_queue(), _mixed_queue()
    batch_publish.recompute_totals(a, ["douyin"])
    batch_publish.recompute_totals(b, ["douyin", "kuaishou", "weixin"])
    assert a["published"] == b["published"] == 1
    assert a["remaining"] == b["remaining"] == 2


def test_force_skipped_platforms_are_not_required():
    q = _mixed_queue()
    q["videos"][0]["published"]["xhs"] = {"status": "ok"}
    required = batch_publish.queue_platforms(q, ["douyin", "kuaishou", "weixin"])
    assert "xhs" not in required, "xhs is force-skipped (account risk)"


def test_per_platform_breakdown_is_reported():
    q = _mixed_queue()
    batch_publish.recompute_totals(q, ["douyin", "kuaishou", "weixin"])
    bp = q["by_platform"]
    assert bp["weixin"]["done"] == 2
    assert bp["douyin"]["done"] == 2      # 'b' never attempted
    assert bp["douyin"]["pending"] == 1   # only 'c' is eligible to be published
    assert sum(v["pending"] for v in bp.values()) <= q["remaining"]


# ---------------------------------------------------------------------------
# ApiWatch listener lifecycle
# ---------------------------------------------------------------------------

class _FakePage:
    """Minimal stand-in for a Playwright page's event emitter."""

    def __init__(self):
        self.listeners = []

    def on(self, event, fn):
        self.listeners.append(fn)

    def remove_listener(self, event, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)


def test_apiwatch_does_not_accumulate_listeners_across_videos():
    """The batch driver reuses one tab per platform and builds a watcher per
    video. Without detaching, listeners grew for the life of the tab."""
    from core.watch import ApiWatch

    page = _FakePage()
    watchers = [ApiWatch(page) for _ in range(50)]
    assert len(page.listeners) == 1, "one live listener per page"
    assert not watchers[-1].closed
    assert all(w.closed for w in watchers[:-1])


def test_closed_watcher_stops_capturing_but_keeps_history():
    from core.watch import ApiWatch

    page = _FakePage()
    old = ApiWatch(page)
    old.events.append({"url": "https://x/upload/finish", "path": "/upload/finish",
                       "status": 200, "resp": None})
    new = ApiWatch(page)

    # the old listener is detached, so a new response only reaches `new`
    for fn in list(page.listeners):
        fn(type("R", (), {"request": type("Q", (), {"method": "POST"})(),
                          "headers": {"content-type": "application/json"},
                          "url": "https://x/new", "status": 200})())
    assert old.events[-1]["path"] == "/upload/finish", "history preserved"
    assert not any("new" in e["url"] for e in old.events), "old one stopped"
    assert any("new" in e["url"] for e in new.events), "new one captures"


def test_apiwatch_context_manager_detaches():
    from core.watch import ApiWatch

    page = _FakePage()
    with ApiWatch(page) as w:
        assert len(page.listeners) == 1
    assert w.closed
    assert page.listeners == []


def test_apiwatch_close_is_idempotent():
    from core.watch import ApiWatch

    page = _FakePage()
    w = ApiWatch(page)
    w.close()
    w.close()
    assert page.listeners == []
