"""Human-like behavior simulator.

The platforms are increasingly hostile to bursts of identical automation:
- Burst of identical publish timings -> "automation" flag
- Same mouse paths, no scroll -> "robot" flag
- No idle reads -> suspicious
- Filling forms without pause -> suspicious

This module injects mild, randomized human-like motion BEFORE the mechanical
steps run. It is deliberately conservative:

- It never interferes with selectors (no jiggling click targets)
- It only adds short pauses + small mouse moves + occasional scrolls
- It can be disabled with MX_HUMAN=0 (CI mode)

Anti-ban goals this module targets:
1. Spread publish timings so they look like a human posting over time
2. Add small intra-action pauses (form fill, page reload) so the platform
   doesn't see a "form appeared -> form filled in 200ms" pattern
3. Briefly look at the page after upload (humans check the preview)
4. Occasional scroll to dismiss content that would otherwise sit pinned

Usage:

    from core.behavior import Humanizer
    h = Humanizer(page, intensity="normal")
    h.before_form_fill()
    page.fill(...)
    h.after_form_fill()
    h.before_publish_click()
"""
from __future__ import annotations

import os
import random
import time
from enum import Enum
from typing import Optional

from playwright.sync_api import Page

# Disabled when MX_HUMAN=0 (CI / tests).
HUMAN_ENABLED = os.environ.get("MX_HUMAN", "1") not in ("0", "false", "no")


class HumanIntensity(str, Enum):
    SUBTLE = "subtle"        # 0.3..1.0x base — fast enough for batch
    NORMAL = "normal"        # 1.0x base — default
    THOROUGH = "thorough"    # 2.0..3.0x base — minimum risk


_BASE_DELAYS_MS = {
    # event -> (min, max) ms
    "after_navigation": (300, 900),
    "between_form_fields": (120, 380),
    "after_upload_complete": (1500, 3500),  # humans check the preview
    "before_publish_click": (200, 800),
    "after_publish_click": (300, 1100),
    "between_topics": (300, 900),
}


def _jitter(base_ms: int, intensity: float = 1.0,
            variance: float = 0.4) -> int:
    """Return base_ms * intensity with normal-ish jitter, clamped to sane range."""
    if not HUMAN_ENABLED:
        return 0
    factor = max(0.0, random.gauss(1.0, variance))
    ms = int(base_ms * intensity * factor)
    return max(0, ms)


def _sleep_ms(ms: int) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


class Humanizer:
    """Stateful humanizer bound to a single page.

    Use one per video or per platform — the timing seed can be reset to
    deliberately vary bursts.
    """

    def __init__(self, page: Page, intensity: str = "normal",
                 *, seed: Optional[int] = None):
        self.page = page
        try:
            self.intensity = HumanIntensity(intensity)
        except ValueError:
            self.intensity = HumanIntensity.NORMAL
        if seed is not None:
            random.seed(seed)

    # ------------------------------------------------------------------
    # Movement primitives — best-effort, never raise.
    # ------------------------------------------------------------------

    def _scroll_once(self) -> None:
        try:
            self.page.mouse.wheel(0, random.randint(60, 220))
            self.page.wait_for_timeout(_jitter(120, 1.0))
            self.page.mouse.wheel(0, random.randint(-30, -120))
        except Exception:
            pass

    def _mouse_wiggle(self) -> None:
        try:
            x0 = random.randint(200, 900)
            y0 = random.randint(200, 700)
            self.page.mouse.move(x0, y0)
            self.page.wait_for_timeout(_jitter(80, 1.0))
            # gentle bezier-like jitter via 2 random offsets
            self.page.mouse.move(x0 + random.randint(-40, 40),
                                 y0 + random.randint(-30, 30))
        except Exception:
            pass

    def _look_at_upload(self) -> None:
        """Humans almost always pause to look at the preview after upload."""
        try:
            # Move to where the preview player typically is
            vp = self.page.viewport_size or {"width": 1440, "height": 900}
            self.page.mouse.move(vp["width"] * 0.35, vp["height"] * 0.55)
            self.page.wait_for_timeout(_jitter(400, 1.5))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Hooks called by platform modules.
    # ------------------------------------------------------------------

    def after_navigation(self) -> None:
        ms = _jitter(*_BASE_DELAYS_MS["after_navigation"],
                     intensity=self._scale())
        if random.random() < 0.5:
            self._mouse_wiggle()
        _sleep_ms(ms)

    def before_form_fill(self) -> None:
        if random.random() < 0.3:
            self._scroll_once()

    def between_form_fields(self) -> None:
        ms = _jitter(*_BASE_DELAYS_MS["between_form_fields"],
                     intensity=self._scale())
        if random.random() < 0.15:
            self._mouse_wiggle()
        _sleep_ms(ms)

    def between_topics(self) -> None:
        ms = _jitter(*_BASE_DELAYS_MS["between_topics"],
                     intensity=self._scale())
        _sleep_ms(ms)

    def after_upload_complete(self) -> None:
        # Look at the preview + scroll a little so we don't look mechanical.
        self._look_at_upload()
        if random.random() < 0.4:
            self._scroll_once()
        ms = _jitter(*_BASE_DELAYS_MS["after_upload_complete"],
                     intensity=self._scale())
        _sleep_ms(ms)

    def before_publish_click(self) -> None:
        ms = _jitter(*_BASE_DELAYS_MS["before_publish_click"],
                     intensity=self._scale())
        if random.random() < 0.5:
            self._mouse_wiggle()
        _sleep_ms(ms)

    def after_publish_click(self) -> None:
        ms = _jitter(*_BASE_DELAYS_MS["after_publish_click"],
                     intensity=self._scale())
        _sleep_ms(ms)

    def between_videos(self, *, base_s: float = 8.0,
                       spread_s: float = 12.0) -> None:
        """Bigger pause between videos — humans don't post 30 in 5 minutes.

        base_s + random(0..spread_s) on NORMAL; *2 on THOROUGH; /2 on SUBTLE.
        """
        ms = _jitter(int(base_s * 1000), intensity=self._scale(),
                     variance=0.6)
        ms += random.randint(0, int(spread_s * 1000))
        _sleep_ms(ms)

    # ------------------------------------------------------------------
    # Scaling
    # ------------------------------------------------------------------

    def _scale(self) -> float:
        return {
            HumanIntensity.SUBTLE: 0.5,
            HumanIntensity.NORMAL: 1.0,
            HumanIntensity.THOROUGH: 2.2,
        }.get(self.intensity, 1.0)


# ---------------------------------------------------------------------------
# Free helpers — usable without instantiating a Humanizer.
# ---------------------------------------------------------------------------

def micro_pause(min_ms: int = 50, max_ms: int = 180) -> None:
    """Tiny randomized pause between typing letters (keystroke jitter)."""
    if not HUMAN_ENABLED:
        return
    _sleep_ms(random.randint(min_ms, max_ms))


def jitter_sleep(base_s: float, *, variance: float = 0.4) -> None:
    """Sleep base_s * gauss(1, variance) seconds."""
    if not HUMAN_ENABLED:
        return
    _sleep_ms(_jitter(int(base_s * 1000), 1.0, variance))


def between_platforms_sleep(min_s: float = 6.0, max_s: float = 18.0) -> None:
    """Bigger pause between platforms — looks like a human switching context."""
    if not HUMAN_ENABLED:
        return
    _sleep_ms(random.randint(int(min_s * 1000), int(max_s * 1000)))


def session_floor(max_per_session: int = 18, *, min_rest_s: int = 300) -> bool:
    """Returns True if a "rest" should be taken; called by orchestrator.

    Lightweight so the caller can wrap its main loop:
        if should_rest(session_count): sleep_long()
    The actual session counter lives in the orchestrator (we have no state).
    """
    # No-op stub; orchestrator owns the counter.
    return False
