"""mx-pub: Multi-platform auto-publish orchestrator.

Two modes:
1. Standalone: publish(platform, ...) opens its own Playwright session.
   Use this for one-off commands.

2. Shared session: publish_all(platforms, video, ...) opens ONE Playwright
   session and reuses it across all platforms. Eliminates the "Timeout 15000ms"
   errors that occur when 4+ Playwright sessions are created in rapid succession.

Each platform module exposes:
- publish_via_api(**kwargs) -> PublishResult  (legacy)
- publish_on_page(page, **kwargs) -> PublishResult  (new shared mode)
- publish_via_browser(**kwargs) -> PublishResult  (legacy standalone)
"""
import json
import time
from pathlib import Path

STATE_FILE = Path(__file__).parent.parent / "publish_state.json"
CDP_URL_DEFAULT = "http://127.0.0.1:9222"


class PublishResult:
    def __init__(self, platform, status, *, method, duration_s=0, error=None, **details):
        self.platform = platform
        self.status = status  # ok / partial / fail
        self.method = method  # api / extension / cdp / gui
        self.duration_s = duration_s
        self.error = error
        self.details = details
        self.tokens_used = 0

    def to_dict(self):
        return {
            "platform": self.platform,
            "status": self.status,
            "method": self.method,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            **self.details,
        }


def track(platform, result: PublishResult):
    """Record publish attempt to state file."""
    state = {"platforms": {}, "history": []}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    if platform not in state["platforms"]:
        state["platforms"][platform] = {"attempts": 0, "successes": 0, "methods": {}, "last_status": None}
    p = state["platforms"][platform]
    p["attempts"] += 1
    if result.status == "ok":
        p["successes"] += 1
    p["last_status"] = result.status
    p["methods"][result.method] = p["methods"].get(result.method, 0) + 1
    p["last_updated"] = time.time()

    state["history"].append({
        "ts": time.time(),
        "platform": platform,
        **result.to_dict(),
        "tokens_used": result.tokens_used,
    })
    state["history"] = state["history"][-100:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _get_module(platform):
    """Lazy-import platform module."""
    import importlib
    return importlib.import_module(f"platforms.{platform}")


def publish(platform, *, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish. Opens its own Playwright session.

    Tries API first, falls back to browser automation.
    """
    t0 = time.time()
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)

    mod = _get_module(platform)

    # API first
    api_err = None
    try:
        if hasattr(mod, "publish_via_api"):
            result = mod.publish_via_api(title=title, description=description, video=video, topics=topics or [], location=location, **kwargs)
            if result.status == "ok":
                result.duration_s = time.time() - t0
                track(platform, result)
                return result
    except Exception as e:
        api_err = str(e)

    # Browser fallback
    try:
        if hasattr(mod, "publish_on_page"):
            # Shared mode available — but standalone creates its own session
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
                ctx = browser.contexts[0]
                # Find or create a page for this platform
                page = _find_page(ctx, platform, mod)
                if page is None:
                    result = PublishResult(platform, "fail", method="cdp", error=f"no_{platform}_page")
                else:
                    page.bring_to_front()
                    if hasattr(mod, "_setup_page"):
                        page = mod._setup_page(page) or page
                    result = mod.publish_on_page(page, title=title, description=description, video=video, topics=topics or [], location=location, **kwargs)
                    result.method = result.method or "cdp"
        else:
            # Legacy standalone (opens own playwright)
            result = mod.publish_via_browser(title=title, description=description, video=video, topics=topics or [], location=location, **kwargs)
        result.duration_s = time.time() - t0
        track(platform, result)
        return result
    except Exception as e:
        result = PublishResult(platform, "fail", method="cdp", error=f"api={api_err}; cdp={e}")
        result.duration_s = time.time() - t0
        track(platform, result)
        return result


def _find_page(ctx, platform, mod):
    """Find an appropriate page for this platform in the browser context."""
    if hasattr(mod, "_match_url"):
        match = mod._match_url()
        for t in ctx.pages:
            if match(t.url):
                return t
    return None


def publish_all(platforms, video, *, cdp_url=None, inter_delay_s=2) -> dict:
    """Publish one video to multiple platforms using ONE shared Playwright session.

    This is the recommended path for batch workflows — avoids Chrome CDP
    timeout errors from creating 4+ Playwright sessions in rapid succession.

    Args:
        platforms: list of platform names (e.g. ["xhs", "douyin", "kuaishou", "weixin"])
        video: dict with keys: path, title, description, hashtags
        cdp_url: Chrome DevTools Protocol URL (default localhost:9222)
        inter_delay_s: seconds to wait between platforms

    Returns:
        dict mapping platform name to PublishResult
    """
    from playwright.sync_api import sync_playwright

    cdp_url = cdp_url or CDP_URL_DEFAULT
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=20000)
        ctx = browser.contexts[0]

        for i, platform in enumerate(platforms):
            if i > 0:
                time.sleep(inter_delay_s)
            mod = _get_module(platform)

            t0 = time.time()
            try:
                page = _find_page(ctx, platform, mod)
                if page is None:
                    # Fallback: create a new tab and let the module navigate it
                    page = ctx.new_page()
                page.bring_to_front()
                if hasattr(mod, "_setup_page"):
                    page = mod._setup_page(page) or page
                if hasattr(mod, "publish_on_page"):
                    result = mod.publish_on_page(
                        page,
                        title=video.get("title", ""),
                        description=video.get("description", ""),
                        video=video["path"],
                        topics=video.get("hashtags", []),
                    )
                    result.method = result.method or "cdp"
                else:
                    result = PublishResult(platform, "fail", method="cdp", error="no_publish_on_page_in_module")
                result.duration_s = time.time() - t0
                track(platform, result)
                results[platform] = result
            except Exception as e:
                result = PublishResult(platform, "fail", method="cdp", error=str(e)[:200])
                result.duration_s = time.time() - t0
                track(platform, result)
                results[platform] = result

    return results