"""The risk monitor gates whether the batch publishes at all.

Two defects made its verdicts untrustworthy: successful publishes were
classified `transient`, so the "repeated error class" signal counted them and
reported "30/30 recent failures" for a platform with a 100% success rate; and
the window was "the last N events" with no age bound, so a bad run last Tuesday
still vetoed today's batch.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from core.errors import Action, ErrorClass, classify  # noqa: E402
from self_learn.risk import RiskMonitor  # noqa: E402
from self_learn.stats import Event, StatsStore  # noqa: E402


def _store(tmp_path, events):
    store = StatsStore(tmp_path / "events.jsonl")
    now = time.time()
    for i, (status, cls, sev) in enumerate(events):
        store.record(Event(ts=now - i * 60, platform="douyin", video_id=f"v{i}",
                           status=status, method="cdp", duration_s=1.0,
                           error="" if status == "ok" else "boom",
                           cls=cls, action="retry", severity=sev,
                           hour=12, weekday=0, content_hash="", title_len=3,
                           topics_n=1, attempt_n=1))
    return store


def test_successful_publish_is_not_an_error_class():
    c = classify(None, status="ok")
    assert c.cls is ErrorClass.OK
    assert c.severity == 0
    assert c.action is Action.NONE
    # the old value, which poisoned every aggregate
    assert c.cls is not ErrorClass.TRANSIENT


def test_healthy_platform_reports_no_failure_signature(tmp_path):
    store = _store(tmp_path, [("ok", "ok", 0)] * 30)
    report = RiskMonitor(store).check("douyin")
    assert not any("recent failures" in t for t in report.triggers), \
        report.triggers
    assert report.success_rate == 1.0


def test_error_class_signal_only_counts_failures(tmp_path):
    """The denominator must be the failure count, not the event count."""
    # 4 ok + 4 fail -> failures are half the events, all one class
    store = _store(tmp_path, [("ok", "ok", 0)] * 4 + [("fail", "transient", 2)] * 4)
    triggers = RiskMonitor(store).check("douyin").triggers
    burst = [t for t in triggers if "recent failures classed" in t]
    assert burst, triggers
    assert "4/4" in burst[0], f"wrong denominator: {burst[0]}"


def test_stale_history_cannot_veto_a_fresh_run(tmp_path):
    """A bad run last week must not block publishing today."""
    store = tmp_path / "events.jsonl"
    st = StatsStore(store)
    old = time.time() - 10 * 86400
    for i in range(20):
        st.record(Event(ts=old + i, platform="douyin", video_id=f"v{i}",
                        status="fail", method="cdp", duration_s=1.0,
                        error="timeout", cls="transient", action="retry",
                        severity=2, hour=3, weekday=0, content_hash="",
                        title_len=3, topics_n=1, attempt_n=1))
    # with the age window disabled the stale failures would veto the run
    assert RiskMonitor(st, max_age_s=0).check("douyin").level == "critical"
    # by default they are outside the window, so publishing is not blocked
    assert RiskMonitor(st).check("douyin").level == "ok"


def test_error_label_counts_exclude_successes(tmp_path):
    store = _store(tmp_path, [("ok", "ok", 0)] * 5 + [("fail", "transient", 2)])
    labels = store.summary()["error_labels"]
    assert labels == {"transient/retry": 1}, labels


def test_consecutive_failure_streak_is_detected(tmp_path):
    store = _store(tmp_path, [("fail", "transient", 2)] * 5)
    report = RiskMonitor(store).check("douyin")
    assert report.level == "critical"
    assert any("consecutive failures" in t for t in report.triggers)


def test_cooldown_is_capped(tmp_path):
    store = _store(tmp_path, [("fail", "account", 9)] * 8)
    report = RiskMonitor(store).check("douyin")
    assert report.cooldown_s <= 21600
