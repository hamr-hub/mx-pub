"""Behavior simulator hook — anti-ban jitter on every publish.

Auto-loaded by load_extensions() at startup. Hooks into:
  - platform:after_setup     → small post-navigation pause
  - video:before_publish     → form-fill jitter
  - video:after_publish      → post-click jitter

Reads MX_HUMAN env to disable in CI mode.

Configurable via MX_HUMAN_INTENSITY= subtle|normal|thorough.
"""
import os

from core.hooks import Hooks


_INTENSITY = os.environ.get("MX_HUMAN_INTENSITY", "normal")


def _after_setup(**kw):
    if os.environ.get("MX_HUMAN", "1") in ("0", "false", "no"):
        return
    page = kw.get("page")
    if page is None:
        return
    from core.behavior import Humanizer
    Humanizer(page, intensity=_INTENSITY).after_navigation()


def _before_publish(**kw):
    if os.environ.get("MX_HUMAN", "1") in ("0", "false", "no"):
        return
    # We don't always have a page handle here; use the per-platform modules'
    # own jitter calls. This hook is a deliberately empty safety net.
    return


def _after_publish(**kw):
    if os.environ.get("MX_HUMAN", "1") in ("0", "false", "no"):
        return
    platform = kw.get("platform")
    # Apply a between-videos pause.
    from core.behavior import between_platforms_sleep
    if platform and platform not in ("douyin", "kuaishou", "weixin", "xhs"):
        # New platform — use a milder default
        between_platforms_sleep(2.0, 6.0)
    else:
        between_platforms_sleep(4.0, 12.0)


Hooks.register("platform:after_setup", _after_setup)
Hooks.register("video:before_publish", _before_publish)
Hooks.register("video:after_publish", _after_publish)
