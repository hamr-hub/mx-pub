"""Error taxonomy & classification.

Every publish attempt produces a PublishResult with .status and .error. This
module takes the raw error string (or exception) and classifies it into one of:

    TRANSIENT       - retry soon, the same code path may work
    RECOVERABLE     - retry after a recovery action (refresh, re-login, etc.)
    CONTENT         - this video is unacceptable to the platform, skip
    ACCOUNT         - the account is in trouble (limit hit, ban risk)
    PLATFORM        - the platform itself is degraded (5xx, maintenance)
    FATAL           - infrastructure is broken, escalate to human
    UNKNOWN         - unclassified, treat as recoverable for safety

The classifier also exposes:
- recommended_action():  what the recovery engine should try
- severity():             how serious (0=info, 10=critical/ban risk)
- cooldown_seconds():     minimum wait before retrying THIS error class
- is_ban_signal():        hard signal that may indicate the account is flagged

Design notes:
- We match on substrings because platform error messages are localized and
  unstable across versions. Use multi-language keywords (zh + en) where the
  platform exposes both.
- Patterns are evaluated top-down; first match wins. Order matters.
- 2026-09-15: this is the brain behind the self-healing layer — recovery.py
  looks up the class and decides which strategy to run.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ErrorClass(str, Enum):
    OK = "ok"                        # published successfully — never an error
    TRANSIENT = "transient"
    RECOVERABLE = "recoverable"
    CONTENT = "content"
    ACCOUNT = "account"
    PLATFORM = "platform"
    FATAL = "fatal"
    UNKNOWN = "unknown"


class Action(str, Enum):
    """What the recovery engine should do when this error class triggers."""
    RETRY = "retry"                  # just retry after cooldown
    REFRESH_PAGE = "refresh_page"    # reload the publish page
    RELOGIN = "relogin"              # full login flow required
    COOL_DOWN = "cool_down"          # back off, possibly skip this platform
    SKIP_VIDEO = "skip_video"        # this video can't post, skip and move on
    ABORT_PLATFORM = "abort_platform"  # stop posting to this platform
    ABORT_BATCH = "abort_batch"      # stop the whole batch
    ESCALATE = "escalate"            # log + notify human
    NONE = "none"                    # nothing to do (successful publish)


@dataclass(frozen=True)
class Classification:
    cls: ErrorClass
    action: Action
    severity: int                # 0..10
    cooldown_s: float
    is_ban_signal: bool = False
    is_fatal_connection: bool = False
    label: str = ""             # human-readable sub-class
    note: str = ""              # explanation / recommendation


# ---------------------------------------------------------------------------
# Pattern tables — first match wins.
# ---------------------------------------------------------------------------

#: Hard ban / account-loss signals. These are nearly always single-attempt.
BAN_SIGNAL_PATTERNS = (
    ("account_banned", ("账号已被封禁", "账号被封", "account banned",
                         "账户已封停", "用户已被封禁", "封号")),
    ("verification_required", ("需要进行身份验证", "实名认证", "real-name",
                                 "verify identity", "实名验证")),
    ("device_blocked", ("设备异常", "device blocked", "当前设备被限制",
                          "设备已被封禁")),
    ("content_removed_appeal", ("内容违规下架", "违规下架", "作品已被删除",
                                  "视频已被下架")),
)

#: Account-level limits / soft-bans (cool down heavily but recoverable).
ACCOUNT_PATTERNS = (
    ("rate_limited", ("今日已达上限", "今日发布上限", "已达到每日上限",
                       "今日次数已用完", "rate limit", "too many requests",
                       "频繁发布", "操作过于频繁", "请稍后再试")),
    ("draft_locked", ("草稿箱已满", "草稿数量已达上限", "draft limit")),
    ("quota_exceeded", ("配额已用完", "quota exceeded", "次数已用完")),
    ("appeal_required", ("需要申诉", "appeal required", "请先申诉",
                           "账号申诉")),
    ("abnormal_activity", ("账号异常", "账号状态异常", "account abnormal",
                            "behavior abnormal")),
)

#: Content rejections — same video won't pass even after retry.
CONTENT_PATTERNS = (
    # Prefixes the driver itself writes into PublishResult.error. These are
    # terminal by construction: a safety-gate veto or a preflight duration
    # block will never become publishable by retrying, and `classify` used to
    # fall through to `unknown/retry` for them.
    ("safety_gate", ("safety:", "safety gate", "safety gate blocked")),
    ("too_short", ("视频时长不足", "时长过短", "duration too short",
                    "duration less than", "minimum duration")),
    ("too_long", ("视频时长超过", "时长过长", "duration exceeds",
                   "max duration")),
    ("format_unsupported", ("格式不支持", "format not supported",
                              "unsupported format", "invalid format")),
    ("resolution_unsupported", ("分辨率不支持", "resolution not supported")),
    ("size_too_large", ("文件过大", "file too large", "size exceeds")),
    ("duplicate_content", ("内容重复", "重复发布", "duplicate",
                             "already posted", "已发布过")),
    ("copyright", ("侵犯版权", "copyright", "侵权", "版权问题",
                    "原创声明", "音乐侵权", "肖像权")),
    ("prohibited_content", ("内容违规", "内容不符合", "prohibited",
                              "违规内容", "视频违规", "违规", "禁止发布",
                              "policy violation", "内容不合规", "违反",
                              "不予通过", "审核不通过", "review failed",
                              "存在争议", "仅自己可见", "下架", "封禁内容")),
    ("ai_undeclared", ("未声明AI生成", "ai declaration required")),
    ("topic_blocked", ("话题不可用", "话题已被禁用", "topic blocked")),
    ("music_blocked", ("音乐不可用", "音频不可用", "music blocked",
                        "audio blocked", "版权音乐")),
)

#: Platform-side issues — retry usually works.
PLATFORM_PATTERNS = (
    ("http_5xx", ("500", "502", "503", "504", "internal server error",
                   "bad gateway", "service unavailable", "gateway timeout")),
    ("maintenance", ("系统维护", "维护中", "under maintenance",
                      "服务升级")),
    ("cdn_error", ("cdn error", "网络异常", "network error",
                    "上传服务异常", "upload service error")),
    ("api_deprecated", ("api deprecated", "接口已下线", "endpoint moved")),
)

#: Recoverable — same code path may work after a recovery action.
RECOVERABLE_PATTERNS = (
    ("cookie_expired", ("登录已过期", "session expired", "请重新登录",
                          "cookie expired", "not logged in", "未登录")),
    ("captcha_required", ("验证码", "captcha", "人机验证", "滑块验证",
                            "verify you are human")),
    ("auth_required", ("需要登录", "请登录", "auth required",
                        "permission denied", "无权限")),
    ("selector_drift", ("element not found", "locator not found",
                          "selector timeout", "no such element",
                          "等待元素超时", "未找到元素")),
    ("upload_failed_retry", ("upload failed", "上传失败", "upload timeout",
                               "上传超时", "fragment failed", "分片失败",
                               "tos upload")),
    ("transcode_failed", ("transcode failed", "转码失败", "transcode error")),
    ("modal_blocking", ("modal open", "弹窗遮挡", "dialog blocking",
                          "需要先关闭")),
    ("button_disabled", ("button disabled", "按钮被禁用", "按钮不可点击",
                          "发布按钮未激活")),
)

#: Transient infra — retry soon with same code.
TRANSIENT_PATTERNS = (
    # A media path the *browser* cannot open is not a verdict on the video.
    # It is listed first so this specific diagnosis beats the generic
    # "timeout" substring. In production this was the single largest error
    # class: a long-lived browser kept a stale view of an external volume,
    # returned ENOENT for files the shell could open, and every row was
    # scored unknown/retry — a 5s retry loop that burned browser round-trips
    # and pushed the platform toward a risk-monitor abort. Retrying after a
    # fresh browser session is exactly the right remedy, so it is transient,
    # with a cooldown long enough not to spin.
    ("media_unreadable", ("file_missing", "no such file or directory",
                          "filenotfounderror", "errno 2",
                          "文件不存在", "视频文件不存在", "找不到文件",
                          "cannot find the file", "is not a file")),
    ("timeout", ("timeout", "超时", "timed out")),
    ("connection_reset", ("connection reset", "connection refused",
                           "econnreset", "connection aborted")),
    ("network_blip", ("network", "网路", "断网", "offline")),
    ("cdn_throttle", ("429", "too many requests from", "throttled")),
    ("publish_in_progress", ("上次发布未完成", "publish in progress",
                                "operation in progress")),
    # "Target crashed" is the renderer dying, which is just as terminal as a
    # closed target: there is no retry point on a dead page. It was missing
    # here while the legacy list in publisher.py had "page crashed", so a
    # crashed Chrome scored unknown/retry and the run ground through every
    # remaining row on a page that could never work again.
    ("fatal_connection", ("connection closed", "target closed",
                            "target crashed", "target has crashed",
                            "page crashed", "page has crashed",
                            "renderer crashed", "session closed",
                            "browser closed",
                            "browser has been closed",
                            "target page, context or browser has been closed",
                            "target page has been closed",
                            "playwright connection closed",
                            "page has been closed")),
)

#: Pure infra / configuration / code issues — escalate to human.
FATAL_PATTERNS = (
    ("chrome_dead", ("chrome not found", "no chrome",
                       "chromium binary missing")),
    ("no_publish_button", ("no_publish_button", "发布按钮未找到",
                             "publish button gone")),
    ("selector_completely_broken", ("404 not found", "页面不存在",
                                       "page not found")),
    ("config_error", ("config error", "configuration missing",
                        "missing required config", "invalid token")),
)


def _normalize(text: str) -> str:
    """Fold case and separator style so one pattern table covers all spellings.

    The driver emits machine-readable markers like `fatal_connection_closed`,
    while every pattern in the tables is written with spaces
    (`"connection closed"`). Matching the raw text meant the most important
    dead-transport signal in the codebase was classified `unknown/retry` — the
    driver retried against a dead CDP instead of aborting the platform.
    """
    return (text.lower()
            .replace("_", " ")
            .replace("-", " ")
            .replace("/", " "))


def _match_table(text: str, table) -> Optional[tuple]:
    """Return (label, keyword) of the first matching pattern, or None."""
    if not text:
        return None
    text_lower = _normalize(text)
    for label, keywords in table:
        for kw in keywords:
            if _normalize(kw) in text_lower:
                return (label, kw)
    return None


def classify(error: Optional[str], *,
             exc: Optional[BaseException] = None,
             status: Optional[str] = None,
             platform: Optional[str] = None) -> Classification:
    """Classify a publish error into one of the ErrorClass buckets.

    Args:
        error:  the .error string from PublishResult (may be None)
        exc:    the original exception if available
        status: the PublishResult.status (ok/fail/partial/blocked)
        platform: platform id (for context-aware hints)
    """
    text_parts = []
    if error:
        text_parts.append(str(error))
    if exc is not None:
        text_parts.append(str(exc))
    text = " | ".join(text_parts)

    # Status-driven shortcuts
    if status == "blocked":
        return Classification(
            cls=ErrorClass.CONTENT, action=Action.SKIP_VIDEO,
            severity=2, cooldown_s=0.0, label="blocked",
            note="explicitly blocked (duration/codec/etc)")
    if status == "ok":
        # A successful publish must never be recorded as a transient error:
        # successes carried cls="transient", so every error aggregate counted
        # them — the risk monitor reported "30/30 recent failures" for a
        # platform with a 100% success rate.
        return Classification(
            cls=ErrorClass.OK, action=Action.NONE,
            severity=0, cooldown_s=0.0, label="ok",
            note="published")

    # 1. Fatal infra (always last to win, but we want it early for clear
    #    config issues so escalation happens instead of futile retries).
    if m := _match_table(text, FATAL_PATTERNS):
        return Classification(
            cls=ErrorClass.FATAL, action=Action.ABORT_BATCH,
            severity=10, cooldown_s=0.0, label=m[0],
            note=f"matched: {m[1]}")

    # 1b. The driver's own preflight blocks (<5s clips) are terminal content
    #     decisions, not upload failures. Matched before the generic tables so
    #     "duration_2.10s_below_min_5s" cannot be read as a transient timeout.
    if isinstance(error, str) and error.startswith("duration_") \
            and "below_min" in error:
        return Classification(
            cls=ErrorClass.CONTENT, action=Action.SKIP_VIDEO,
            severity=2, cooldown_s=0.0, label="too_short",
            note="preflight: clip shorter than the 5s account floor")

    # 2. Ban signals — never retry blindly; escalate immediately.
    if m := _match_table(text, BAN_SIGNAL_PATTERNS):
        return Classification(
            cls=ErrorClass.ACCOUNT, action=Action.ABORT_PLATFORM,
            severity=10, cooldown_s=86400, is_ban_signal=True,
            label=m[0], note=f"matched: {m[1]} — escalate")

    # 3. Account-level soft limits — cool down hard.
    if m := _match_table(text, ACCOUNT_PATTERNS):
        sev = 7
        cd = 3600.0  # 1 hour default
        if m[0] == "rate_limited":
            cd = 1800.0
        elif m[0] == "abnormal_activity":
            sev = 9
            cd = 7200.0
        return Classification(
            cls=ErrorClass.ACCOUNT, action=Action.COOL_DOWN,
            severity=sev, cooldown_s=cd, label=m[0],
            note=f"matched: {m[1]} — cool down {cd:.0f}s")

    # 4. Content rejections — skip this video.
    if m := _match_table(text, CONTENT_PATTERNS):
        return Classification(
            cls=ErrorClass.CONTENT, action=Action.SKIP_VIDEO,
            severity=4, cooldown_s=0.0, label=m[0],
            note=f"matched: {m[1]} — don't retry same video")

    # 5. Platform-side issues — wait + retry.
    if m := _match_table(text, PLATFORM_PATTERNS):
        cd = 60.0
        if m[0] == "maintenance":
            cd = 600.0
        return Classification(
            cls=ErrorClass.PLATFORM, action=Action.RETRY,
            severity=5, cooldown_s=cd, label=m[0],
            note=f"matched: {m[1]} — retry after {cd:.0f}s")

    # 6. Recoverable — needs an action before retry.
    if m := _match_table(text, RECOVERABLE_PATTERNS):
        action_map = {
            "cookie_expired": Action.RELOGIN,
            "captcha_required": Action.ESCALATE,
            "auth_required": Action.RELOGIN,
            "selector_drift": Action.REFRESH_PAGE,
            "upload_failed_retry": Action.RETRY,
            "transcode_failed": Action.RETRY,
            "modal_blocking": Action.REFRESH_PAGE,
            "button_disabled": Action.REFRESH_PAGE,
        }
        cd_map = {
            "cookie_expired": 5.0,
            "captcha_required": 0.0,
            "auth_required": 5.0,
            "selector_drift": 3.0,
            "upload_failed_retry": 30.0,
            "transcode_failed": 60.0,
            "modal_blocking": 2.0,
            "button_disabled": 5.0,
        }
        sev_map = {
            "captcha_required": 9,
            "cookie_expired": 6,
            "auth_required": 6,
        }
        return Classification(
            cls=ErrorClass.RECOVERABLE,
            action=action_map.get(m[0], Action.RETRY),
            severity=sev_map.get(m[0], 3),
            cooldown_s=cd_map.get(m[0], 5.0),
            label=m[0], note=f"matched: {m[1]}")

    # 7. Transient infra — fast retry.
    if m := _match_table(text, TRANSIENT_PATTERNS):
        cd_map = {
            "fatal_connection": 0.0,
            "media_unreadable": 60.0,
            "timeout": 5.0,
            "connection_reset": 3.0,
            "network_blip": 10.0,
            "cdn_throttle": 30.0,
            "publish_in_progress": 60.0,
        }
        is_fatal_conn = m[0] == "fatal_connection"
        return Classification(
            cls=ErrorClass.TRANSIENT,
            action=Action.ABORT_PLATFORM if is_fatal_conn else Action.RETRY,
            severity=2 if not is_fatal_conn else 8,
            cooldown_s=cd_map.get(m[0], 5.0),
            is_fatal_connection=is_fatal_conn,
            label=m[0], note=f"matched: {m[1]}")

    # 8. Unknown — assume recoverable so we don't lose work.
    return Classification(
        cls=ErrorClass.UNKNOWN, action=Action.RETRY,
        severity=3, cooldown_s=15.0, label="unknown",
        note=f"unclassified: {text[:120]}")


def is_fatal_connection_text(text: str) -> bool:
    """Shorthand used by core/watch.py and elsewhere."""
    cls = classify(text)
    return cls.is_fatal_connection


# Human-friendly description for logs / insights.
_CLASS_DESCRIPTIONS = {
    ErrorClass.TRANSIENT: "transient (retry soon)",
    ErrorClass.RECOVERABLE: "recoverable (action required)",
    ErrorClass.CONTENT: "content (skip this video)",
    ErrorClass.ACCOUNT: "account (cool down / escalate)",
    ErrorClass.PLATFORM: "platform (retry later)",
    ErrorClass.FATAL: "fatal (abort batch)",
    ErrorClass.UNKNOWN: "unknown (treat as recoverable)",
}


def describe(cls: Classification) -> str:
    base = _CLASS_DESCRIPTIONS.get(cls.cls, str(cls.cls))
    if cls.label and cls.label != "ok":
        return f"{base} — {cls.label}"
    return base


def action_for(error: Optional[str], **kwargs) -> Action:
    """Convenience: just return the recommended Action."""
    return classify(error, **kwargs).action
