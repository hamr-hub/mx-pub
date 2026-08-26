"""Shared browser utilities — used by all platform modules.

Common helpers:
- connect_chrome(): robust CDP connect with retry
- find_file_input(): multi-selector fallback for upload inputs
- find_platform_page(): match by URL pattern
- safe_click(): click + wait_for_element + retry
"""
import time
from typing import Callable, Optional


CDP_URL_DEFAULT = "http://127.0.0.1:9222"


def connect_chrome(cdp_url: str = CDP_URL_DEFAULT, max_attempts: int = 3,
                  base_backoff_s: int = 5):
    """Connect to Chrome via CDP with exponential backoff retry.

    Returns (playwright, browser, context) tuple, or raises RuntimeError.
    """
    from playwright.sync_api import sync_playwright

    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        p = sync_playwright().start()
        try:
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=20000)
            ctx = browser.contexts[0]
            return p, browser, ctx
        except Exception as e:
            last_err = e
            try:
                p.stop()
            except Exception:
                pass
            if attempt < max_attempts - 1:
                wait = base_backoff_s * (2 ** attempt)
                print(f"  [chrome] connect failed (attempt {attempt+1}/{max_attempts}), "
                      f"retrying in {wait}s: {str(e)[:100]}")
                time.sleep(wait)
    raise RuntimeError(f"chrome_connect_failed after {max_attempts} attempts: {last_err}")


def find_file_input(page, prefer_video: bool = True):
    """Find a file input via multiple fallback selectors.

    Order:
    1. input[type=file][accept*='video']
    2. input[type=file][accept*='.mp4']/.mov/.flv
    3. input[type=file] (any)
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
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def find_platform_page(ctx, url_match: Callable[[str], bool]):
    """Find a browser page matching the URL pattern."""
    for t in ctx.pages:
        try:
            if url_match(t.url):
                return t
        except Exception:
            continue
    return None


def fill_first(page, selectors_with_values: list, timeout_per: int = 5000):
    """Try each (selector, value) pair; fill the first one that exists.

    Each entry is (selector, text). Returns the one that succeeded or None.
    """
    for sel, txt in selectors_with_values:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(txt, timeout=timeout_per)
                return sel
        except Exception:
            continue
    return None


def click_publish_button(page, button_text: str = "发布",
                          primary_class_patterns: tuple = (
                              "button--primary", "primary-btn", "primary-",
                              "_button-primary", "btn-primary"),
                          timeout: int = 10000) -> str:
    """Click the platform's publish button via multiple strategies.

    Returns one of:
      'clicked_<strategy>' on success
      'not_found' if no button matches
    """
    js_click = """
    (() => {
        const text = arguments[0];
        const patterns = arguments[1];
        // Strategy 1: button or div with primary class + matching text
        const all = document.querySelectorAll('button, div, [role=button]');
        for (const d of all) {
            const cls = (d.className || '').toString();
            const t = (d.innerText || '').trim();
            if (patterns.some(p => cls.includes(p)) && t === text) {
                d.scrollIntoView({block: 'center'});
                d.click();
                return 'clicked_primary';
            }
        }
        // Strategy 2: any visible element with exact text
        const matches = Array.from(document.querySelectorAll('*')).filter(e => {
            const t = (e.innerText || '').trim();
            return t === text && e.children.length === 0 && e.offsetParent !== null;
        });
        if (matches.length > 0) {
            matches[matches.length - 1].scrollIntoView({block: 'center'});
            matches[matches.length - 1].click();
            return 'clicked_text';
        }
        // Strategy 3: by role
        return 'not_found';
    })()
    """
    clicked = page.evaluate(js_click, [button_text, list(primary_class_patterns)])
    if clicked != 'not_found':
        return clicked
    # Last resort: Playwright locator
    try:
        btn = page.get_by_role("button", name=button_text).first
        if btn.count() > 0:
            btn.click(timeout=timeout)
            return 'clicked_role'
    except Exception:
        pass
    return 'not_found'


TRANSIENT_ERROR_PATTERNS = (
    "timeout 20000ms exceeded",
    "timeout 15000ms exceeded",
    "timeout 10000ms exceeded",
    "timeout 30000ms exceeded",
    "target page, context or browser has been closed",
    "browser has been closed",
    "connection closed",
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
    """Whether the error message suggests a retry might succeed."""
    if not err:
        return False
    err_lower = err.lower()
    return any(pat in err_lower for pat in TRANSIENT_ERROR_PATTERNS)