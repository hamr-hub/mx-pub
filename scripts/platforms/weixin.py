"""Weixin 视频号 publisher.

Strategy:
- Weixin Channels is a Wujie sub-app: parent page (/platform/post/create) hosts
  an iframe[name=content] that loads /micro/content/post/create.
- As of 2026-08-26: file input lives in the OUTER parent page, NOT inside the iframe.
- Form fields (title, description, publish button) live in the iframe.
"""
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://channels.weixin.qq.com/platform/post/create"


def _match_url():
    def match(url: str) -> bool:
        return "channels.weixin" in url
    return match


def _find_weixin_page(ctx):
    """Find the weixin post/create tab if open."""
    for pg in ctx.pages:
        if "channels.weixin.qq.com" in pg.url and ("platform/post" in pg.url or "platform/statistics" in pg.url):
            return pg
    return None


def _setup_page(page):
    """Navigate to weixin post create."""
    if "platform/post/create" not in page.url or page.url == "about:blank":
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(10)
    return page


def publish_via_api(*, title, description, video, topics=None, location=None, cookies=None, **kwargs) -> PublishResult:
    return PublishResult("weixin", "fail", method="api", error="weixin_api_blocked_use_browser")


def publish_on_page(page, *, title, description, video, topics=None, location=None,
                    fast_mode: bool = False, **kwargs) -> PublishResult:
    """Publish using an existing Playwright page (shared-session mode).

    fast_mode: skip upload-wait and confirmation-wait. Returns immediately after
    the click. Use for batch publishing. Default off.
    """
    title = (title or "")[:14]
    page.bring_to_front()
    page.set_viewport_size({"width": 1440, "height": 900})

    if "platform/post/create" not in page.url:
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5 if fast_mode else 10)

    # Wait for file input to appear on OUTER page (fast: 3s, normal: 25s)
    file_input_max = 3 if fast_mode else 25
    for _ in range(file_input_max):
        if page.locator("input[type=file]").count() > 0:
            break
        time.sleep(1)

    if page.locator("input[type=file]").count() == 0:
        return PublishResult("weixin", "fail", method="cdp", error="no_file_input")

    # Upload via outer-page file input
    try:
        page.locator("input[type=file]").first.set_input_files(video, timeout=60000, no_wait_after=True)
    except Exception as e:
        return PublishResult("weixin", "fail", method="cdp", error=f"upload: {e}")

    # Wait for upload (fast: skip, normal: 60s)
    if not fast_mode:
        for _ in range(60):
            state = page.evaluate(
                "(() => ({has_cover: document.body.innerText.includes('封面'), uploading: document.body.innerText.includes('上传中')}))()"
            )
            if state and state.get("has_cover") and not state.get("uploading"):
                break
            time.sleep(2)

    # Fill form: title + description (live in iframe)
    iframe_loc = page.frame_locator("iframe[name=content]")
    try:
        title_inp = iframe_loc.locator('input[placeholder*="短标题"]')
        if title_inp.count() > 0:
            title_inp.first.fill(title, timeout=10000)
    except Exception:
        pass

    try:
        ce = iframe_loc.locator(".input-editor")
        if ce.count() > 0:
            ce.first.click()
            ce.first.fill(f"{description} {' '.join(topics or [])}", timeout=10000)
    except Exception:
        pass

    time.sleep(1 if fast_mode else 2)

    # Click 发表 button (lives in iframe)
    clicked = page.evaluate("""(() => {
        const iframe = document.querySelector("iframe[name=content]");
        if (!iframe || !iframe.contentDocument) return 'no_iframe_doc';
        const btns = Array.from(iframe.contentDocument.querySelectorAll('*'));
        for (const b of btns) {
            const t = (b.innerText || '').trim();
            if (t === '发表' && b.children.length === 0) {
                b.click();
                return 'clicked';
            }
        }
        return 'not_found';
    })()""")
    if clicked != 'clicked':
        return PublishResult("weixin", "fail", method="cdp", error=f"no_publish_button: {clicked}")

    # Wait for confirmation (fast: skip, normal: 45s)
    if fast_mode:
        return PublishResult("weixin", "ok", method="cdp", clicked=clicked,
                            fast_mode=True, note="submitted; verification skipped")

    for _ in range(45):
        cur = page.url
        if "post/list" in cur or "manage" in cur:
            return PublishResult("weixin", "ok", method="cdp", final=cur)
        body = page.evaluate("document.body.innerText")
        if "已发表" in body or "发布成功" in body:
            return PublishResult("weixin", "ok", method="cdp", final=body[:200])
        time.sleep(1)

    return PublishResult("weixin", "partial", method="cdp", error="timeout_waiting_for_publish_confirm")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish."""
    from playwright.sync_api import sync_playwright
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = _find_weixin_page(ctx)
        if page is None:
            page = ctx.new_page()
        try:
            return publish_on_page(page, title=title, description=description, video=video, topics=topics, location=location)
        except Exception as e:
            return PublishResult("weixin", "fail", method="cdp", error=str(e)[:200])