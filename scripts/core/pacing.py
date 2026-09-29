"""Adaptive pacing — protect the account, not just the queue.

The platforms penalize bursts. The naive "publish 30 videos back-to-back" loop
gets accounts flagged. This module makes the batch driver slow itself down
based on observable signals:

- Per-platform success rate (rolling window)
- Time of day (some windows are noisier — learned from history)
- Recent consecutive failures (back off after errors)

Design:
- Pure data class; no I/O, no Playwright. Easy to unit-test.
- State is persisted to a JSON file so a restart inherits learned pacing.
- The orchestrator asks "is this platform healthy enough to publish?" via
  should_publish_now(); the answer accounts for cool-down windows and
  rolling success rate.

This is the lightweight sibling of self_learn/stats.py + risk.py. We keep it
small and dependency-free so a failing risk-monitor never blocks publishing.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Where learned state lives. Default colocated with the queue state.
# Sits beside publish_queue.json / publish_state.json, not under scripts/.
DEFAULT_STATE_PATH = Path(
    os.environ.get("MX_PACING_STATE",
                   str(Path(__file__).resolve().parent.parent.parent
                       / "pacing_state.json")))

# Defaults — overridable via env.
DEFAULT_MIN_INTERVAL_S = float(os.environ.get("MX_MIN_INTERVAL_S", "120"))     # 2 min
DEFAULT_BURST_LIMIT = int(os.environ.get("MX_BURST_LIMIT", "5"))                # 5 in a row
DEFAULT_BURST_REST_S = float(os.environ.get("MX_BURST_REST_S", "300"))          # 5 min rest
DEFAULT_FAIL_RATE_BACKOFF = float(os.environ.get("MX_FAIL_BACKOFF", "0.6"))      # 60% success threshold
DEFAULT_LOW_RATE_REST_S = float(os.environ.get("MX_LOW_RATE_REST_S", "1800"))  # 30 min before a low-success probe


@dataclass
class PlatformPacing:
    platform: str
    last_publish_ts: float = 0.0
    recent_results: deque = field(default_factory=lambda: deque(maxlen=20))
    burst_count: int = 0
    burst_window_start: float = 0.0
    cool_down_until: float = 0.0

    # Learned parameters (overridable via env or via self_learn insights).
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    burst_limit: int = DEFAULT_BURST_LIMIT
    burst_rest_s: float = DEFAULT_BURST_REST_S
    fail_rate_backoff: float = DEFAULT_FAIL_RATE_BACKOFF

    # How long to sit out after the success rate trips, before the single
    # probe attempt that decides whether the platform recovered.
    low_rate_rest_s: float = DEFAULT_LOW_RATE_REST_S

    # True once a low-success-rate cool-down has been armed and is counting
    # down. The probe is granted when the window closes, not before.
    _low_rate_armed: bool = False

    # Last computed recommendation (for telemetry / logs).
    last_reason: str = ""

    def record(self, success: bool) -> None:
        now = time.time()
        self.last_publish_ts = now
        self.recent_results.append(1 if success else 0)
        if self.burst_window_start == 0.0 or now - self.burst_window_start > 3600:
            self.burst_window_start = now
            self.burst_count = 0
        self.burst_count += 1

    @property
    def success_rate(self) -> float:
        if not self.recent_results:
            return 1.0
        return sum(self.recent_results) / len(self.recent_results)

    def should_publish_now(self) -> tuple[bool, str]:
        now = time.time()
        if now < self.cool_down_until:
            wait = self.cool_down_until - now
            self.last_reason = f"cool_down({wait:.0f}s)"
            return False, self.last_reason
        gap = now - self.last_publish_ts
        if self.last_publish_ts > 0 and gap < self.min_interval_s:
            wait = self.min_interval_s - gap
            self.last_reason = f"min_interval({wait:.0f}s)"
            return False, self.last_reason
        if self.burst_count >= self.burst_limit:
            # Reaching the burst limit is a *rest*, not a terminal veto. Arm
            # the rest window and reset the counter here; otherwise nothing
            # ever cleared burst_count (force_burst_rest had no caller) and
            # the platform stayed blocked for the remainder of the run.
            count = self.burst_count
            self.force_burst_rest()
            self.last_reason = f"burst_limit_reached({count})"
            return False, self.last_reason
        rate = self.success_rate
        if len(self.recent_results) >= 5 and rate < self.fail_rate_backoff:
            # Time-bounded, not terminal. The window that trips this rule only
            # changes when a publish is recorded — which the rule itself
            # prevents — so a plain veto here blocked the platform forever and
            # the queue could never drain. Arm a cool-down instead, then grant
            # exactly one probe when it expires: if the probe succeeds the
            # rolling rate recovers, and if it fails the rule re-arms.
            if self._low_rate_armed:
                self._low_rate_armed = False
                self.last_reason = f"low_rate_probe({rate:.2f})"
                return True, self.last_reason
            self._low_rate_armed = True
            self.enter_cool_down(self.low_rate_rest_s)
            self.last_reason = f"success_rate_low({rate:.2f})"
            return False, self.last_reason
        self.last_reason = "ok"
        return True, self.last_reason

    def enter_cool_down(self, seconds: float) -> None:
        self.cool_down_until = max(self.cool_down_until,
                                    time.time() + seconds)

    def force_burst_rest(self) -> None:
        self.cool_down_until = max(self.cool_down_until,
                                    time.time() + self.burst_rest_s)
        self.burst_count = 0
        self.burst_window_start = time.time()

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "last_publish_ts": self.last_publish_ts,
            "recent_results": list(self.recent_results),
            "burst_count": self.burst_count,
            "burst_window_start": self.burst_window_start,
            "cool_down_until": self.cool_down_until,
            "min_interval_s": self.min_interval_s,
            "burst_limit": self.burst_limit,
            "burst_rest_s": self.burst_rest_s,
            "fail_rate_backoff": self.fail_rate_backoff,
            "low_rate_rest_s": self.low_rate_rest_s,
            "_low_rate_armed": self._low_rate_armed,
            "last_reason": self.last_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlatformPacing":
        p = cls(platform=d["platform"])
        # burst_limit was missing from this list, so a tuned limit (or one
        # raised via update_params) silently reverted to the default on every
        # restart even though to_dict persisted it.
        for k in ("last_publish_ts", "burst_window_start", "cool_down_until",
                  "min_interval_s", "burst_limit", "burst_rest_s",
                  "fail_rate_backoff", "low_rate_rest_s"):
            if k in d:
                setattr(p, k, d[k])
        p.burst_count = d.get("burst_count", 0)
        p._low_rate_armed = bool(d.get("_low_rate_armed", False))
        p.last_reason = d.get("last_reason", "")
        rr = d.get("recent_results", [])
        p.recent_results = deque(rr, maxlen=20)
        return p


class PacingController:
    """Multi-platform adaptive pacer.

    Use:
        pacer = PacingController.load()
        if pacer.allow("douyin")[0]: ...
        pacer.record("douyin", success=True)
        pacer.save()
    """

    def __init__(self, platforms: list[str]):
        self._pacing: dict[str, PlatformPacing] = {
            p: PlatformPacing(platform=p) for p in platforms
        }

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH,
             platforms: Optional[list[str]] = None) -> "PacingController":
        platforms = platforms or ["douyin", "kuaishou", "weixin", "xhs"]
        pc = cls(platforms)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                for plat, d in data.get("platforms", {}).items():
                    # Also adopts platforms absent from the initial list, so a
                    # state file written by a wider run is not silently lost.
                    pc._pacing[plat] = PlatformPacing.from_dict(d)
            except Exception:
                # corrupt file → fall back to defaults; never crash
                pass
        return pc

    def save(self, path: Path = DEFAULT_STATE_PATH) -> None:
        payload = {"platforms": {p: pp.to_dict()
                                  for p, pp in self._pacing.items()},
                   "saved_at": time.time()}
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(path)
        except Exception:
            pass

    def get(self, platform: str) -> PlatformPacing:
        return self._pacing.setdefault(platform,
                                        PlatformPacing(platform=platform))

    def allow(self, platform: str) -> tuple[bool, str]:
        return self.get(platform).should_publish_now()

    def record(self, platform: str, success: bool) -> None:
        self.get(platform).record(success)

    def cool_down(self, platform: str, seconds: float) -> None:
        self.get(platform).enter_cool_down(seconds)

    def force_rest(self, platform: str) -> None:
        self.get(platform).force_burst_rest()

    def update_params(self, platform: str, *,
                      min_interval_s: Optional[float] = None,
                      burst_limit: Optional[int] = None,
                      burst_rest_s: Optional[float] = None,
                      fail_rate_backoff: Optional[float] = None,
                      low_rate_rest_s: Optional[float] = None) -> None:
        p = self.get(platform)
        if min_interval_s is not None:
            p.min_interval_s = float(min_interval_s)
        if burst_limit is not None:
            p.burst_limit = int(burst_limit)
        if burst_rest_s is not None:
            p.burst_rest_s = float(burst_rest_s)
        if fail_rate_backoff is not None:
            p.fail_rate_backoff = float(fail_rate_backoff)
        if low_rate_rest_s is not None:
            p.low_rate_rest_s = float(low_rate_rest_s)

    def snapshot(self) -> dict:
        return {p: pp.to_dict() for p, pp in self._pacing.items()}
