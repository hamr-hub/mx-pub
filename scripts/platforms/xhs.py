"""Xiaohongshu (小红书) publisher."""
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
EDITOR_URL = "https://creator.xiaohongshu.com/publish/publish?from=menu&target=video"


def _match_url():
    """Match xhs publish editor tab (must have file input, not just /publish launch page)."""
    def match(url: str) -> bool:
        return "creator.xiaohongshu.com" in url and "publish" in url and "publish/publish" in url
    return match


def _find_xhs_publish_page(ctx):
    """Find a tab that already has the file input — the real editor."""
    for pg in ctx.pages:
        if _match_url()(pg.url) and pg.locator("input[type=file]").count() > 0:
            return pg
    return None


def _setup_page(page):
    """Navigate to xhs editor URL."""
    if "publish/publish" not in page.url or page.url == "about:blank":
        page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
    return page


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    return PublishResult("xhs", "fail", method="api", error="xhs_api_not_implemented_use_browser")


def publish_on_page(page, *, title, description, video, topics=None, location=None,
                    fast_mode: bool = False, **kwargs) -> PublishResult:
    """Publish using an existing Playwright page."""
    title = (title or "")[:20]
    topic_str = " ".join(f"#{t}" if not t.startswith("#") else t for t in (topics or []))

    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 800})
    time.sleep(1)

    for _ in range(15):
        if page.locator("input[type=file]").count() > 0:
            break
        time.sleep(1)

    try:
        inp = page.locator("input[type=file]").first
        inp.set_input_files(video, timeout=15000, no_wait_after=True)
    except Exception as e:
        return PublishResult("xhs", "fail", method="cdp", error=f"upload_failed: {e}")

    # Wait for cover generation (fast: 5s, normal: 45s)
    cover_max = 5 if fast_mode else 45
    for _ in range(cover_max):
        state = page.evaluate("""(() => ({
            hasCover: document.body.innerText.includes('封面') || document.querySelectorAll('.cover, [class*="cover"]').length > 0,
            hasBtn: document.querySelectorAll('xhs-publish-btn, [class*="publish-btn"]').length,
        }))()""")
        if state.get("hasCover") and state.get("hasBtn", 0) > 0:
            break
        time.sleep(1)

    # Fill title
    try:
        page.locator('input[placeholder*="标题"]').first.fill(title, timeout=5000)
    except Exception:
        pass

    # Fill content
    try:
        ce = page.locator(".tiptap.ProseMirror, [contenteditable=true]").first
        ce.click()
        ce.fill(f"{description} {topic_str}".strip())
    except Exception:
        pass

    # Find and click the actual publish button
    clicked = page.evaluate("""(() => {
        const wrapper = document.querySelector('.btn-wrapper');
        if (!wrapper) return 'no_btn_wrapper';
        const inner = wrapper.querySelector('.btn-inner');
        if (!inner) return 'no_btn_inner';
        inner.click();
        return 'clicked_btn_inner';
    })()""")

    if clicked != 'clicked_btn_inner':
        return PublishResult("xhs", "fail", method="cdp", error=f"no_publish_button: {clicked}")

    # Wait for completion (fast: skip, normal: 60s)
    if fast_mode:
        return PublishResult("xhs", "ok", method="cdp", clicked=clicked,
                            fast_mode=True, note="submitted; verification skipped")

    for _ in range(60):
        time.sleep(1)
        still_uploading = page.evaluate("(() => document.body.innerText.includes('上传中'))()")
        if not still_uploading:
            final = page.evaluate("(() => document.body.innerText.slice(0, 500))()")
            if "成功" in final or "已发布" in final or "创作" in final:
                return PublishResult("xhs", "ok", method="cdp", clicked=clicked, final=final[:200])
            continue

    return PublishResult("xhs", "partial", method="cdp", error="upload_text_still_showing_after_60s", clicked=clicked)


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish — opens its own session."""
    from playwright.sync_api import sync_playwright
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]

        page = _find_xhs_publish_page(ctx)
        if page is None:
            page = ctx.new_page()
            page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=30000)
            for _ in range(15):
                time.sleep(1)
                page = _find_xhs_publish_page(ctx)
                if page:
                    break
            if page is None:
                return PublishResult("xhs", "fail", method="cdp", error="no_editor_after_navigate")

        try:
            return publish_on_page(page, title=title, description=description, video=video, topics=topics, location=location)
        except Exception as e:
            return PublishResult("xhs", "fail", method="cdp", error=str(e)[:200])