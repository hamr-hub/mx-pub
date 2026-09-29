"""Pre-flight health checks before publishing.

Detects the conditions that turn a "looks fine" Chrome into a 30-minute black
hole of silent failures:

- Cookie / session expired on a platform tab
- Captcha / verification panel showing
- Platform rate limit banners
- Login page redirects
- Network reachability to the platform host
- Chrome CDP transport still healthy

Returns a structured HealthReport so the orchestrator can decide whether to
proceed, fix-and-retry, or abort.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Page

# Shared with errors.py so the same wording means the same thing.


# ---------------------------------------------------------------------------
# Per-platform reachability & landing markers
# ---------------------------------------------------------------------------

#: Hosts whose connectivity we expect for each platform.
PLATFORM_HOSTS = {
    "douyin": ("creator.douyin.com", "www.douyin.com"),
    "kuaishou": ("cp.kuaishou.com", "www.kuaishou.com"),
    "weixin": ("channels.weixin.qq.com",),
    "xhs": ("creator.xiaohongshu.com",),
}

#: Words that appear on the platform's "you're logged out" page (zh + en).
LOGGED_OUT_MARKERS = (
    "请登录", "登录后", "未登录", "扫码登录", "请先登录",
    "sign in", "log in", "扫码", "qr code",
)

#: Words that indicate a captcha / verification challenge.
CAPTCHA_MARKERS = (
    "滑块验证", "人机验证", "图形验证", "验证码",
    "captcha", "verify you are human", "robot check",
    "请完成验证", "请进行安全验证",
)

#: Words that indicate an account warning / limit banner.
WARNING_MARKERS = (
    "账号异常", "账号已被限制", "功能受限", "请稍后再试",
    "操作频繁", "今日已达上限", "账号申诉", "违规提醒",
    "warning", "limited", "abnormal",
)


@dataclass
class HealthReport:
    platform: str
    ok: bool = True
    checks: list[dict] = field(default_factory=list)

    # Pre-filled convenience flags so callers don't dig through checks.
    session_alive: bool = True
    captcha_visible: bool = False
    warning_visible: bool = False
    rate_limit_visible: bool = False
    page_responsive: bool = True
    network_reachable: bool = True
    url: str = ""

    def add(self, name: str, ok: bool, **detail):
        self.checks.append({"name": name, "ok": bool(ok), **detail})
        if not ok:
            self.ok = False

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "ok": self.ok,
            "session_alive": self.session_alive,
            "captcha_visible": self.captcha_visible,
            "warning_visible": self.warning_visible,
            "rate_limit_visible": self.rate_limit_visible,
            "page_responsive": self.page_responsive,
            "network_reachable": self.network_reachable,
            "url": self.url,
            "checks": self.checks,
        }


# ---------------------------------------------------------------------------
# Page-level probes
# ---------------------------------------------------------------------------

def _probe_page(page: Page) -> dict:
    """Return a JSON-serializable dict of in-page health signals.

    Safe to run against any page; uses try/except per signal so one stale
    iframe can't break the whole probe.
    """
    js = r"""(() => {
        const out = {
            url: location.href,
            title: document.title || '',
            bodyText: (document.body ? document.body.innerText : '').slice(0, 4000),
            modalCount: document.querySelectorAll('[role=dialog], .modal, .semi-modal, .ant-modal').length,
            buttonCount: document.querySelectorAll('button').length,
            fileInputCount: document.querySelectorAll('input[type=file]').length,
        };
        return out;
    })()"""
    try:
        return page.evaluate(js) or {}
    except Exception as e:
        return {"error": str(e)[:200], "_probe_error": True}


def _has_marker(text: str, markers: tuple) -> Optional[str]:
    if not text:
        return None
    text_lower = text.lower()
    for m in markers:
        if m.lower() in text_lower:
            return m
    return None


def _is_login_page(url: str, body_text: str) -> bool:
    """Detect platform landing/login redirects by URL + body markers."""
    if not url:
        return False
    url_lower = url.lower()
    # 1. URL shape is decisive: any of these segments IS the login surface.
    if any(seg in url_lower for seg in ("/login", "/signin", "/oauth",
                                          "passport.", "login.")):
        return True
    # 2. Body markers are a weaker signal: a logged-in home page also renders
    #    "扫码登录" as a UI affordance, so this only counts when there is no
    #    file input / publish UI on the page.
    return _has_marker(body_text, LOGGED_OUT_MARKERS)


def _looks_like_publish_page(platform: str, body_text: str, url: str) -> bool:
    """Cheap heuristic: a real publish editor renders an upload zone and form."""
    if not url or not body_text:
        return False
    markers = {
        "douyin": ("作品标题", "上传视频", "input[type=file]"),
        "kuaishou": ("上传视频", "重新上传", "发布"),
        "weixin": ("短标题", "话题", "视频号"),
        "xhs": ("标题", "正文", "上传"),
    }
    pats = markers.get(platform, ())
    url_lower = url.lower()
    publish_url_hint = any(s in url_lower for s in
                            ("/publish", "/post/create", "/upload"))
    body_match = sum(1 for p in pats if p in body_text) >= 1
    return publish_url_hint or body_match


def check_page(page: Page, platform: str, *,
               timeout_ms: int = 8000) -> HealthReport:
    """Run the standard in-page health checks for one platform tab."""
    report = HealthReport(platform=platform)
    try:
        page.bring_to_front()
        report.url = page.url or ""
    except Exception as e:
        report.add("bring_to_front", False, error=str(e)[:120])
        report.page_responsive = False
        return report

    # Page must answer evaluate calls — proves the CDP transport is alive.
    t0 = time.time()
    try:
        info = _probe_page(page)
    except Exception as e:
        report.page_responsive = False
        report.add("page_evaluate", False, error=str(e)[:160])
        return report
    report.add("page_evaluate", True, latency_ms=int((time.time() - t0) * 1000))

    body = info.get("bodyText") or ""
    url = info.get("url") or report.url

    # 1. Session/login
    if _is_login_page(url, body):
        report.session_alive = False
        report.add("session", False, reason="login_page_or_logged_out",
                   url=url)
    else:
        report.add("session", True)

    # 2. Captcha / verification challenge
    cap = _has_marker(body, CAPTCHA_MARKERS)
    if cap:
        report.captcha_visible = True
        report.add("captcha", False, marker=cap)
    else:
        report.add("captcha", True)

    # 3. Account warning / limit banner
    warn = _has_marker(body, WARNING_MARKERS)
    if warn:
        report.warning_visible = True
        report.rate_limit_visible = "上限" in warn or "rate" in warn.lower() \
            or "限" in warn
        report.add("warning_banner", False, marker=warn)
    else:
        report.add("warning_banner", True)

    # 4. Looks like the publish editor at all?
    if not _looks_like_publish_page(platform, body, url):
        report.add("publish_ui", False, reason="no_publish_markers_in_dom",
                   url=url)
    else:
        report.add("publish_ui", True)

    # 5. Captcha + warning = block right away.
    if report.captcha_visible:
        report.ok = False
    if report.warning_visible and report.rate_limit_visible:
        report.ok = False
    if not report.session_alive:
        report.ok = False

    return report


# ---------------------------------------------------------------------------
# Network-level checks
# ---------------------------------------------------------------------------

def check_network(platform: str, *, timeout_s: float = 4.0) -> bool:
    """Quick reachability probe — does the platform's CDN answer?"""
    import socket
    hosts = PLATFORM_HOSTS.get(platform, ())
    if not hosts:
        return True
    for host in hosts:
        try:
            socket.create_connection((host, 443), timeout=timeout_s).close()
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Composite check used by the orchestrator before each video / platform
# ---------------------------------------------------------------------------

def preflight(page: Page, platform: str, *,
              require_publish_ui: bool = False) -> HealthReport:
    """One-call health gate before kicking off publishing.

    If require_publish_ui is True, the report is failing when the page doesn't
    look like a publish editor (helps catch wrong-tab navigation).
    """
    report = check_page(page, platform)
    if not report.network_reachable:
        report.network_reachable = check_network(platform)
        report.add("network", report.network_reachable,
                   platform=platform)
    if require_publish_ui:
        publish_ok = any(c.get("name") == "publish_ui" and c.get("ok")
                          for c in report.checks)
        if not publish_ok:
            report.ok = False
    return report
