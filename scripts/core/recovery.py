"""Auto-recovery engine.

When classify() says the action is REFRESH_PAGE / RELOGIN / COOL_DOWN / etc.,
this module does the actual recovery work:

- REFRESH_PAGE    -> hard reload the publish page, re-attach dialog guard
- RELOGIN         -> navigate to the platform's landing page so the user can
                     re-scan QR / re-enter password; report non-recoverable
                     if a re-login flow is required (we never store passwords)
- COOL_DOWN       -> sleep the class cooldown, return Action.COOL_DOWN
- SKIP_VIDEO      -> mark video as skipped, continue
- ABORT_PLATFORM  -> signal caller to stop the platform loop
- ABORT_BATCH     -> signal caller to stop everything

The orchestrator should treat recovery as a black box — pass it the page + the
classification, get back a RecoveryResult describing what happened.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page

from core.errors import Action, Classification
from core.health import check_page

# Per-platform re-login URL (the page where a QR / password prompt lives).
RELANDING_URL = {
    "douyin": "https://creator.douyin.com/creator-micro/home",
    "kuaishou": "https://cp.kuaishou.com/article/publish/video",
    "weixin": "https://channels.weixin.qq.com/platform/post/create",
    "xhs": "https://creator.xiaohongshu.com/publish/publish?from=menu&target=video",
}


@dataclass
class RecoveryResult:
    action: Action
    succeeded: bool
    detail: str = ""
    next_classification: Optional[Classification] = None
    cooldown_until: float = 0.0     # epoch seconds; orchestrator may sleep
    requires_human: bool = False

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "succeeded": self.succeeded,
            "detail": self.detail,
            "requires_human": self.requires_human,
            "cooldown_until": self.cooldown_until,
        }


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def _safe_reload(page: Page, *, wait_ms: int = 4000) -> bool:
    """Hard reload and wait for the page to settle."""
    try:
        page.reload(wait_until="domcontentloaded", timeout=15000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        time.sleep(wait_ms / 1000.0)
        return True
    except Exception:
        return False


def _navigate(page: Page, url: str, *, wait_ms: int = 3000) -> bool:
    """Navigate to a known-good URL on this platform."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        time.sleep(wait_ms / 1000.0)
        return True
    except Exception:
        return False


def _page_responsive(page: Page) -> bool:
    """Cheap liveness probe; True if evaluate() works."""
    try:
        page.evaluate("1")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Recovery strategies
# ---------------------------------------------------------------------------

def refresh_page(page: Page, platform: str) -> RecoveryResult:
    """Reload the publish page and re-check health."""
    target = RELANDING_URL.get(platform)
    ok = _safe_reload(page) if not target else _navigate(page, target)
    if not ok:
        return RecoveryResult(
            action=Action.REFRESH_PAGE, succeeded=False,
            detail="reload_navigation_failed",
            requires_human=False,
        )
    report = check_page(page, platform)
    if report.ok and not report.captcha_visible and report.session_alive:
        return RecoveryResult(
            action=Action.REFRESH_PAGE, succeeded=True,
            detail=f"recovered; {sum(1 for c in report.checks if c['ok'])}/"
                  f"{len(report.checks)} checks ok",
        )
    # Refresh didn't help; upgrade to whatever the report now says.
    if report.captcha_visible:
        return RecoveryResult(
            action=Action.ESCALATE, succeeded=False,
            detail="captcha_present_after_refresh",
            requires_human=True,
        )
    if not report.session_alive:
        return RecoveryResult(
            action=Action.RELOGIN, succeeded=False,
            detail="session_lost_after_refresh",
            requires_human=True,
        )
    return RecoveryResult(
        action=Action.REFRESH_PAGE, succeeded=False,
        detail=f"refresh_insufficient: {report.to_dict()}",
    )


def relogin(page: Page, platform: str) -> RecoveryResult:
    """Navigate to the platform landing page so the human can re-authenticate.

    We deliberately do NOT attempt to script credentials — only QR / SSO flows
    the platform exposes natively. Returns requires_human=True so the
    orchestrator pauses and notifies the user.
    """
    target = RELANDING_URL.get(platform)
    if not target:
        return RecoveryResult(
            action=Action.RELOGIN, succeeded=False,
            detail=f"unknown platform: {platform}",
            requires_human=True,
        )
    ok = _navigate(page, target, wait_ms=4000)
    if not ok:
        return RecoveryResult(
            action=Action.RELOGIN, succeeded=False,
            detail="navigate_failed",
            requires_human=True,
        )
    report = check_page(page, platform)
    if report.session_alive and not report.captcha_visible:
        return RecoveryResult(
            action=Action.RELOGIN, succeeded=True,
            detail="session_alive_after_reload",
        )
    return RecoveryResult(
        action=Action.RELOGIN, succeeded=False,
        detail="session_still_lost_after_reload",
        requires_human=True,
    )


def cool_down(classification: Classification) -> RecoveryResult:
    """Just return the cooldown until the wall-clock catches up."""
    return RecoveryResult(
        action=Action.COOL_DOWN,
        succeeded=True,
        detail=f"cooldown {classification.cooldown_s:.0f}s "
               f"({classification.label})",
        cooldown_until=time.time() + classification.cooldown_s,
    )


def skip_video(reason: str = "") -> RecoveryResult:
    return RecoveryResult(
        action=Action.SKIP_VIDEO, succeeded=True,
        detail=f"skipped: {reason}",
    )


def abort_platform(reason: str = "") -> RecoveryResult:
    return RecoveryResult(
        action=Action.ABORT_PLATFORM, succeeded=True,
        detail=f"abort_platform: {reason}",
    )


def abort_batch(reason: str = "") -> RecoveryResult:
    return RecoveryResult(
        action=Action.ABORT_BATCH, succeeded=True,
        detail=f"abort_batch: {reason}",
        requires_human=True,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def perform_recovery(action: Action,
                     *,
                     page: Optional[Page] = None,
                     platform: Optional[str] = None,
                     classification: Optional[Classification] = None,
                     detail: str = "") -> RecoveryResult:
    """Run the recovery action against the (page, platform, classification).

    The orchestrator normally only cares about RecoveryResult.succeeded and
    .requires_human.
    """
    if action == Action.RETRY:
        return RecoveryResult(action=Action.RETRY, succeeded=True, detail=detail)

    if action == Action.COOL_DOWN:
        if classification is None:
            return RecoveryResult(action=Action.COOL_DOWN, succeeded=True,
                                  detail="cool_down_no_classification")
        return cool_down(classification)

    if action in (Action.SKIP_VIDEO, Action.ABORT_PLATFORM, Action.ABORT_BATCH,
                  Action.ESCALATE):
        return RecoveryResult(action=action, succeeded=True, detail=detail,
                              requires_human=(action == Action.ESCALATE or
                                              action == Action.ABORT_BATCH))

    # Actions that need a live page.
    if page is None or platform is None:
        return RecoveryResult(action=action, succeeded=False,
                              detail=f"recovery action {action.value} "
                                     "needs page+platform")

    # Fatal-connection: don't try recovery — the page is dead.
    # Split from the liveness check below to keep the two failure modes
    # (dead classification / unresponsive page) readable side by side.
    if classification is not None and classification.is_fatal_connection:  # noqa: SIM102
        if not _page_responsive(page):
            return RecoveryResult(
                action=Action.ABORT_PLATFORM, succeeded=True,
                detail="cdp_transport_dead_no_recovery")

    if action == Action.REFRESH_PAGE:
        return refresh_page(page, platform)
    if action == Action.RELOGIN:
        return relogin(page, platform)

    # Anything else: retry once after a brief settle.
    return RecoveryResult(action=Action.RETRY, succeeded=True,
                          detail=f"unhandled action {action.value}; falling "
                                 "back to RETRY")


# ---------------------------------------------------------------------------
# High-level: given a PublishResult, run the right recovery.
# ---------------------------------------------------------------------------

def recover_from_result(result, *, page=None) -> RecoveryResult:
    """End-to-end recovery decision from a failed PublishResult."""
    from publisher import PublishResult  # local import; avoid cycle at module load

    if not isinstance(result, PublishResult) or result.status == "ok":
        return RecoveryResult(action=Action.RETRY, succeeded=True,
                              detail="no_recovery_needed")

    from core.errors import classify
    classification = classify(
        result.error, status=result.status, platform=result.platform)
    return perform_recovery(
        classification.action,
        page=page,
        platform=result.platform,
        classification=classification,
        detail=result.error or "",
    )
