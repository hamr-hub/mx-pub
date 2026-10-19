"""Ban-risk monitor.

Watches the recent publish history for the kind of pattern that gets accounts
flagged:

- Sudden drop in success rate
- Repeated same content / same error (looks like a spam burst)
- Account-level warnings (already raised by errors.py → ACCOUNT class)
- Off-hours bot-like activity (every post at 03:00 ±2min)
- Long stretch of high-severity classifications

Outputs a RiskReport with:
    level: "ok" / "warn" / "critical"
    triggers: list[str]   — human-readable explanations
    recommend_action: Action   — what the orchestrator should do
    cooldown_s: float          — how long to back off

The monitor is intentionally NOT AI-callable: pure statistics, no API, no LLM.
It should always run cheaply on every batch.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from core.errors import Action, ErrorClass
from self_learn.stats import StatsStore


@dataclass
class RiskReport:
    platform: str
    level: str = "ok"           # ok / warn / critical
    triggers: list[str] = field(default_factory=list)
    recommend_action: Action = Action.RETRY
    cooldown_s: float = 0.0
    success_rate: float = 1.0
    sample_size: int = 0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "level": self.level,
            "triggers": self.triggers,
            "recommend_action": self.recommend_action.value,
            "cooldown_s": self.cooldown_s,
            "success_rate": self.success_rate,
            "sample_size": self.sample_size,
        }


class RiskMonitor:
    """Pure-stats ban-risk detector."""

    def __init__(self, store: StatsStore, *,
                 warn_rate: float = 0.7,
                 critical_rate: float = 0.4,
                 lookback: int = 30,
                 max_age_s: float = 24 * 3600):
        self.store = store
        self.warn_rate = warn_rate
        self.critical_rate = critical_rate
        self.lookback = lookback
        # History is also bounded by age. The window used to be "the last N
        # events" regardless of when they happened, so a run that failed last
        # Tuesday still vetoed today's batch — the monitor would block the
        # platform forever with no path back to publishing.
        self.max_age_s = max_age_s

    def check(self, platform: str, *,
              now: Optional[float] = None) -> RiskReport:
        now = now or time.time()
        events = self.store.for_platform(platform)
        recent = events[-self.lookback:]
        if self.max_age_s > 0:
            cutoff = now - self.max_age_s
            recent = [e for e in recent if e.ts >= cutoff]
        n = len(recent)
        report = RiskReport(platform=platform, sample_size=n)
        if n == 0:
            return report

        ok = sum(1 for e in recent if e.status == "ok")
        rate = ok / n
        report.success_rate = rate

        # 1. Success-rate thresholds
        if rate < self.critical_rate:
            report.level = "critical"
            report.triggers.append(
                f"success_rate {rate:.0%} < {self.critical_rate:.0%} "
                f"over last {n}")
            report.recommend_action = Action.ABORT_PLATFORM
            report.cooldown_s = 3600.0
        elif rate < self.warn_rate:
            report.level = "warn"
            report.triggers.append(
                f"success_rate {rate:.0%} < {self.warn_rate:.0%} "
                f"over last {n}")
            if report.recommend_action == Action.RETRY:
                report.recommend_action = Action.COOL_DOWN
            report.cooldown_s = max(report.cooldown_s, 600.0)

        # 2. Consecutive failures (3+ in a row is a strong signal)
        consecutive_fails = 0
        for e in reversed(recent):
            if e.status in ("ok", "blocked"):
                break
            consecutive_fails += 1
        if consecutive_fails >= 3:
            report.triggers.append(
                f"{consecutive_fails} consecutive failures on {platform}")
            if consecutive_fails >= 5:
                if report.level != "critical":
                    report.level = "critical"
                report.recommend_action = Action.ABORT_PLATFORM
                report.cooldown_s = max(report.cooldown_s, 1800.0)
            else:
                if report.level == "ok":
                    report.level = "warn"
                report.recommend_action = Action.COOL_DOWN
                report.cooldown_s = max(report.cooldown_s, 300.0)

        # 3. Repeated identical error class (looks like an automation burst).
        #    Only failures count: successes used to carry a class too, so this
        #    reported "30/30 recent failures" on a 100%-success platform. The
        #    denominator is the failure count for the same reason.
        failures = [e for e in recent if e.status not in ("ok", "blocked")]
        classes = Counter(e.cls for e in failures
                          if e.cls and e.cls != ErrorClass.OK.value)
        n_fail = len(failures)
        if classes and n_fail:
            top_cls, top_n = classes.most_common(1)[0]
            if top_n >= 3 and top_n / n_fail >= 0.5:
                report.triggers.append(
                    f"{top_n}/{n_fail} recent failures classed as "
                    f"'{top_cls}' (automation-burst signature)")
                if report.level == "ok":
                    report.level = "warn"
                report.cooldown_s = max(report.cooldown_s, 600.0)

        # 4. Off-hours bot pattern: >3 posts in same hour ±0 min
        #    (humans don't time things to the second).
        seconds_buckets = Counter(int(e.ts) // 60 for e in recent)  # minute buckets
        most_common_min, count_in_min = (
            seconds_buckets.most_common(1)[0] if seconds_buckets else (0, 0))
        if count_in_min >= 3:
            report.triggers.append(
                f"{count_in_min} attempts within the same minute — "
                "automation-burst signature")
            if report.level == "ok":
                report.level = "warn"
            report.cooldown_s = max(report.cooldown_s, 900.0)

        # 5. High-severity classifications accumulating
        high_sev = sum(1 for e in recent if e.severity >= 7)
        if high_sev >= 2:
            report.triggers.append(
                f"{high_sev} high-severity failures (>=7) recently on "
                f"{platform}")
            if report.level == "ok":
                report.level = "warn"
            report.cooldown_s = max(report.cooldown_s, 1800.0)

        # Cap cooldown at 6h — anything longer needs human review.
        if report.cooldown_s > 21600:
            report.cooldown_s = 21600.0
            report.recommend_action = Action.ABORT_PLATFORM

        return report
