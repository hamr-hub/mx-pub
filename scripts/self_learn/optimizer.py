"""Strategy optimizer.

Uses the StatsStore to recommend the best parameters for the next publish
attempt:

- Best hour-of-day for this platform (where success rate is highest)
- Whether to skip the platform entirely for this content
- Pacing parameters (interval, burst limit) tuned from history

The optimizer is intentionally lightweight: it computes a few derived metrics
and exposes them via recommend(platform, video). The orchestrator decides how
to apply the recommendation.

Outputs:
    Recommendation(
        allow: bool,
        reason: str,
        best_hour: int | None,
        pacing: dict       # {min_interval_s, burst_limit, ...}
        warnings: list[str]
    )
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from self_learn.stats import StatsStore


#: How much learned breathing room we are willing to add on top of whatever
#: spacing publishing already imposes. A learned interval is a wall-clock
#: cost per video, so an unbounded value quietly stops the queue draining.
MAX_LEARNED_INTERVAL_S = 180.0
#: Fallback when there is not enough history to learn a cadence from.
DEFAULT_LEARNED_INTERVAL_S = 120.0
#: Gaps needed before we trust the observed cadence over the default.
MIN_GAP_SAMPLES = 5


def _success_gaps(ok_events: list) -> list[float]:
    """Gaps (seconds) between consecutive successful publishes.

    Sorted by time, so this is the cadence the account has actually lived
    with. Implausible gaps (>1 day, or non-monotonic timestamps from a
    resumed queue) are dropped rather than allowed to skew the median.
    """
    ordered = sorted(ok_events, key=lambda e: e.ts)
    gaps = []
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        gap = cur.ts - prev.ts
        if 0 < gap <= 86400:
            gaps.append(gap)
    return gaps


@dataclass
class Recommendation:
    platform: str
    allow: bool = True
    reason: str = "ok"
    best_hour: Optional[int] = None
    avoid_hours: list[int] = field(default_factory=list)
    pacing: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    insights: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "allow": self.allow,
            "reason": self.reason,
            "best_hour": self.best_hour,
            "avoid_hours": self.avoid_hours,
            "pacing": self.pacing,
            "warnings": self.warnings,
            "insights": self.insights,
        }


class StrategyOptimizer:
    """Reads StatsStore and produces per-platform recommendations."""

    def __init__(self, store: StatsStore):
        self.store = store

    def recommend(self, platform: str, *,
                  min_samples: int = 5,
                  lookback_days: float = 14.0) -> Recommendation:
        r = Recommendation(platform=platform)
        events = self.store.for_platform(
            platform, since_ts=time.time() - lookback_days * 86400)
        n = len(events)
        if n < min_samples:
            r.insights.append(
                f"only {n} samples for {platform} (need {min_samples}); "
                "using defaults")
            return r

        # Hour-of-day analysis
        best = self.store.best_hours(platform, min_samples=2, top_k=1)
        worst = self.store.worst_hours(platform, min_samples=2, top_k=3)
        if best:
            r.best_hour = best[0][0]
            r.insights.append(
                f"best hour for {platform}: {best[0][0]:02d}:00 "
                f"({best[0][1]:.0%} over {self.store._hour_count(platform, best[0][0])} samples)")
        if worst:
            r.avoid_hours = [h for h, rate in worst if rate < 0.5]
            if r.avoid_hours:
                r.insights.append(
                    f"avoid hours for {platform}: {r.avoid_hours} "
                    "(<50% success in history)")

        # Pacing: learn the cadence this platform has actually tolerated.
        rate = self.store.success_rate(platform)
        ok_events = [e for e in events if e.status == "ok"]

        # Learn from the gap between consecutive successful publishes — the
        # cadence the account has already demonstrated is safe.
        #
        # This deliberately does NOT use per-video publish duration. The old
        # formula was avg_dur * 1.5, which conflated "how long one upload
        # takes" with "how long to wait between uploads": douyin averages 451s
        # per publish, so it learned a 1015s *additional* gap. On a 96-video
        # queue that is ~27 hours of pure waiting on top of the ~12 hours the
        # uploads already cost, and the queue could never drain. The publish
        # duration is already self-pacing; the pacer only needs to add
        # breathing room on top of it.
        gaps = _success_gaps(ok_events)
        if len(gaps) >= MIN_GAP_SAMPLES:
            base_interval = min(statistics.median(gaps), MAX_LEARNED_INTERVAL_S)
            r.insights.append(
                f"{platform}: observed median gap between successful "
                f"publishes {statistics.median(gaps):.0f}s over {len(gaps)} "
                f"samples -> base interval {base_interval:.0f}s")
        else:
            base_interval = DEFAULT_LEARNED_INTERVAL_S
            r.insights.append(
                f"{platform}: only {len(gaps)} observed gaps (need "
                f"{MIN_GAP_SAMPLES}); interval left at {base_interval:.0f}s")

        if rate < 0.7:
            base_interval *= 1.5
            r.warnings.append(
                f"{platform} success rate {rate:.0%} < 70%; "
                f"interval bumped to {base_interval:.0f}s")
        if rate < 0.4:
            r.allow = False
            r.reason = "low_success_rate"
            r.warnings.append(
                f"{platform} success rate {rate:.0%} is critically low; "
                "manual review recommended")

        # Burst limit from worst 5-minute window
        bursts = _burst_failures(events)
        if bursts > 0:
            r.pacing["burst_rest_s"] = min(900, 60 * 5 + bursts * 60)
            r.insights.append(
                f"{platform} burst failures observed "
                f"({bursts} failures within 5 min); "
                f"recommend rest of {r.pacing['burst_rest_s']:.0f}s")

        r.pacing["min_interval_s"] = base_interval
        r.pacing["fail_rate_backoff"] = 0.6

        return r


def _burst_failures(events) -> int:
    """Max consecutive failures within any 5-minute window."""
    fails = sorted([e.ts for e in events if e.status not in ("ok", "blocked")])
    if not fails:
        return 0
    max_burst = 1
    cur = 1
    # Pairwise walk: the two lists are intentionally offset by one.
    for prev, cur_ts in zip(fails, fails[1:]):  # noqa: B905
        if cur_ts - prev <= 300:
            cur += 1
            max_burst = max(max_burst, cur)
        else:
            cur = 1
    return max_burst


# ---------------------------------------------------------------------------
# Closing the loop: turn a recommendation into pacing parameters
# ---------------------------------------------------------------------------

#: Learned values are clamped before they touch the pacer. History is noisy
#: and sometimes catastrophic (a 10-minute average publish duration, or a
#: single good hour), and a pacing value is a wall-clock commitment: a runaway
#: min_interval_s would silently stop the queue from draining, and a
#: fail_rate_backoff near 0 would block a healthy platform forever. These
#: bounds keep a bad history from turning into a stuck publisher.
PACING_BOUNDS = {
    "min_interval_s": (30.0, 600.0),
    "burst_rest_s": (60.0, 1800.0),
    "fail_rate_backoff": (0.10, 0.95),
}


def clamp_pacing(pacing: dict) -> dict:
    """Clamp a Recommendation.pacing dict to PACING_BOUNDS.

    Unknown keys are dropped; unparseable values fall back to the default.
    """
    out: dict = {}
    for key, (lo, hi) in PACING_BOUNDS.items():
        if key not in pacing:
            continue
        try:
            val = float(pacing[key])
        except (TypeError, ValueError):
            continue
        out[key] = max(lo, min(hi, val))
    return out


def apply_pacing(recommendation: Recommendation, pacing) -> dict:
    """Push a clamped recommendation into a PacingController.

    Returns the parameters actually applied, so the caller can log the change
    instead of the system silently altering its own behaviour. Never raises:
    the learning layer is advisory and must not be able to stop a publish.
    """
    try:
        params = clamp_pacing(recommendation.pacing)
        if not params:
            return {}
        pacing.update_params(recommendation.platform, **params)
        return params
    except Exception:
        return {}
