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
import threading
import time
from pathlib import Path

from core.jsonio import atomic_write_json, read_json_safe

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


# Guards the read-modify-write cycle in track(). Platform threads call it
# concurrently (core/parallel.py runs them through a ThreadPoolExecutor), and
# without this the whole cycle races: lost counter updates, or a JSONDecodeError
# from reading a half-written file.
_STATE_LOCK = threading.Lock()


def track(platform, result: PublishResult):
    """Record a publish attempt to the state file.

    Never raises: telemetry must not be able to fail a publish. A corrupt or
    unreadable state file is reset rather than propagated.
    """
    try:
        with _STATE_LOCK:
            _track_locked(platform, result)
    except Exception:
        pass


def _track_locked(platform, result: PublishResult):
    state = {"platforms": {}, "history": []}
    state = read_json_safe(STATE_FILE, default={})
    if not isinstance(state, dict) or "platforms" not in state:
        state = {"platforms": {}, "history": []}

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
    atomic_write_json(STATE_FILE, state)


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


# Errors that indicate a transient condition (retry with backoff may help)
TRANSIENT_ERROR_PATTERNS = (
    "timeout 20000ms exceeded",
    "timeout 15000ms exceeded",
    "timeout 10000ms exceeded",
    "target page, context or browser has been closed",
    "browser has been closed",
    "connection closed",
    # Emitted by batch_publish when the CDP transport dies mid-video. It used
    # to go unrecognised, so those rows were treated as permanent failures.
    "fatal_connection_closed",
    "navigation interrupted",
    "page crashed",
    "set_input_files",
    "no_weixin_tab",
    "no_douyin_tab",
    "no_kuaishou_tab",
    "no_xhs_tab",
    "no_editor_after_navigate",
)


def is_transient_error(err: str) -> bool:
    """Whether an error message suggests the operation might succeed on retry."""
    if not err:
        return False
    err_lower = err.lower()
    return any(pat in err_lower for pat in TRANSIENT_ERROR_PATTERNS)


# Prefixes the platform modules use for recoverable upload/form errors. They
# are not literal entries in TRANSIENT_ERROR_PATTERNS because the rest of the
# message varies, but an upload that timed out is still worth another go.
RETRYABLE_PREFIXES = ("upload:", "fill_form:", "upload_not_ready")


def is_retryable_failure(error: str | None) -> bool:
    """Whether a failed row is worth re-attempting in a later run.

    Single source of truth for retry eligibility. Two call sites used to
    disagree: the in-loop retry in batch_publish honoured these prefixes,
    while needs_publish() consulted only is_transient_error — so a video
    could be retried twice inside one run and then never again.

    Content rejections, account limits and banned uploads deliberately do not
    qualify: retrying those is how an account gets flagged.

    The rich classifier in core.errors is authoritative when available: it
    knows the safety-gate and preflight prefixes, the account/content tables
    and separator variants. Previously this function consulted only the legacy
    pattern list, so the two disagreed on the same string (e.g. "safety:"
    was permanent here but `unknown/retry` in errors.py).
    """
    error = error or ""
    if not error:
        return True          # no diagnosis recorded — give it one more chance
    try:
        from core.errors import ErrorClass, classify
        cls = classify(error, status="fail").cls
        return cls in (ErrorClass.TRANSIENT, ErrorClass.RECOVERABLE,
                       ErrorClass.PLATFORM, ErrorClass.UNKNOWN)
    except Exception:
        pass
    # Fallback: legacy pattern list (core.errors unavailable).
    if is_transient_error(error):
        return True
    return error.startswith(RETRYABLE_PREFIXES)


def _find_file_input(page, prefer_video=True):
    """Find a file input on the page using multiple fallback selectors.

    Tries selectors in order:
    1. Video file input by accept='video' (most common pattern)
    2. File input by accept containing common video extensions (.mp4, .mov)
    3. Any visible file input
    4. First file input (last resort)

    Returns Playwright locator or None if no input found.
    """
    selectors = []
    if prefer_video:
        selectors += [
            "input[type=file][accept*='video']",
            "input[type=file][accept*='.mp4']",
            "input[type=file][accept*='.mov']",
            "input[type=file][accept*='.flv']",
        ]
    selectors.append("input[type=file]")

    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _connect_chrome(cdp_url, max_attempts=3, base_backoff_s=5):
    """Connect to Chrome CDP with retry. Returns (browser, context) or raises."""
    from playwright.sync_api import sync_playwright

    last_err = None
    for attempt in range(max_attempts):
        try:
            p = sync_playwright().start()
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=20000)
            ctx = browser.contexts[0]
            return p, browser, ctx
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                wait = base_backoff_s * (2 ** attempt)  # 5s, 10s, 20s
                print(f"  [chrome] connect failed (attempt {attempt+1}/{max_attempts}), "
                      f"retrying in {wait}s: {str(e)[:100]}")
                time.sleep(wait)
            try:
                p.stop()
            except Exception:
                pass
    raise RuntimeError(f"chrome_connect_failed after {max_attempts} attempts: {last_err}")


def publish_all(platforms, video, *, cdp_url=None, inter_delay_s=2,
                retry_transient=True, retry_max=2) -> dict:
    """Publish one video to multiple platforms using ONE shared Playwright session.

    Args:
        platforms: list of platform names
        video: dict with path, title, description, hashtags
        cdp_url: Chrome DevTools Protocol URL
        inter_delay_s: seconds between platforms
        retry_transient: retry on transient errors (CDP timeout, etc.)
        retry_max: max retry attempts per platform

    Returns:
        dict mapping platform name to PublishResult
    """
    cdp_url = cdp_url or CDP_URL_DEFAULT
    results = {}

    try:
        p, browser, ctx = _connect_chrome(cdp_url)
    except Exception as e:
        # Connection failed entirely — return fail for all platforms
        for platform in platforms:
            result = PublishResult(platform, "fail", method="cdp", error=f"chrome_connect: {str(e)[:150]}")
            track(platform, result)
            results[platform] = result
        return results

    try:
        for i, platform in enumerate(platforms):
            if i > 0:
                time.sleep(inter_delay_s)
            mod = _get_module(platform)

            attempts = 0
            last_result = None
            while True:
                attempts += 1
                t0 = time.time()
                try:
                    page = _find_page(ctx, platform, mod)
                    if page is None:
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
                        result = PublishResult(platform, "fail", method="cdp",
                                              error="no_publish_on_page_in_module")
                    result.duration_s = time.time() - t0
                    last_result = result
                    # Retry on transient errors
                    if (retry_transient and result.status != "ok"
                            and is_transient_error(result.error)
                            and attempts <= retry_max):
                        wait = 5 * (2 ** (attempts - 1))
                        print(f"  [{platform}] transient error, retry {attempts}/{retry_max} in {wait}s: "
                              f"{result.error[:80]}")
                        time.sleep(wait)
                        continue
                    break
                except Exception as e:
                    err = str(e)[:200]
                    last_result = PublishResult(platform, "fail", method="cdp", error=err)
                    last_result.duration_s = time.time() - t0
                    if (retry_transient and is_transient_error(err)
                            and attempts <= retry_max):
                        wait = 5 * (2 ** (attempts - 1))
                        print(f"  [{platform}] exception (transient), retry {attempts}/{retry_max} in {wait}s: "
                              f"{err[:80]}")
                        time.sleep(wait)
                        continue
                    break

            track(platform, last_result)
            results[platform] = last_result
    finally:
        try:
            p.stop()
        except Exception:
            pass

    return results
