"""Douyin (抖音) publisher."""
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/publish"


def _match_url():
    def match(url: str) -> bool:
        return "creator.douyin.com" in url
    return match


def _setup_page(page):
    """Navigate to publish if needed. Handles blank pages too."""
    if "content/publish" not in page.url or page.url == "about:blank":
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
    return page


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    return PublishResult("douyin", "fail", method="api", error="douyin_api_requires_xbogus_signature_use_browser")


def publish_on_page(page, *, title, description, video, topics=None, location=None,
                    fast_mode: bool = False, **kwargs) -> PublishResult:
    """Publish using an existing Playwright page."""
    title = (title or "")[:30]
    page.bring_to_front()
    page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3 if fast_mode else 5)

    try:
        inp = page.locator("input[type=file][accept*='video']").first
        inp.set_input_files(video, timeout=20000, no_wait_after=True)
    except Exception as e:
        return PublishResult("douyin", "fail", method="cdp", error=f"upload: {e}")

    # Wait for upload processing (fast: 3s, normal: 60s)
    upload_max = 3 if fast_mode else 60
    for i in range(upload_max):
        time.sleep(2)
        state = page.evaluate("""(() => ({
            hasTitle: !!document.querySelector('input[placeholder*="标题"]'),
            uploading: document.body.innerText.includes('上传中') || document.body.innerText.includes('处理中'),
        }))()""")
        if state.get("hasTitle") and not state.get("uploading"):
            break

    # Fill title + description
    try:
        for selector, text in [
            ('input[placeholder*="标题"]', title),
            ('textarea[placeholder*="描述"]', description),
            ('.ql-editor, [contenteditable=true]', description),
        ]:
            el = page.locator(selector).first
            if el.count() > 0:
                el.fill(text)
                break
    except Exception:
        pass

    time.sleep(1 if fast_mode else 2)

    # Click 发布
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        clicked = page.evaluate("""(() => {
            const all = document.querySelectorAll('button, div');
            for (const d of all) {
                const cls = (d.className || '').toString();
                const t = (d.innerText || '').trim();
                if ((cls.includes('button--primary') || cls.includes('primary-btn') ||
                     cls.includes('primary-cECiOJ') || cls.includes('primary-')) && t === '发布') {
                    d.scrollIntoView({block: 'center'});
                    d.click();
                    return 'clicked_primary';
                }
            }
            const btns = Array.from(document.querySelectorAll('button')).filter(b =>
                b.innerText.trim() === '发布' && b.offsetParent !== null
            );
            if (btns.length > 0) {
                btns[btns.length - 1].scrollIntoView({block: 'center'});
                btns[btns.length - 1].click();
                return 'clicked_button_last';
            }
            return 'not_found';
        })()""")
        if clicked == "not_found":
            btn = page.get_by_role("button", name="发布").first
            btn.click(timeout=10000)
    except Exception as e:
        return PublishResult("douyin", "fail", method="cdp", error=f"no_publish_button: {e}")

    # Wait for success (fast: skip, normal: up to 5min)
    if fast_mode:
        return PublishResult("douyin", "ok", method="cdp", clicked=clicked,
                            fast_mode=True, note="submitted; verification skipped")

    for i in range(150):
        time.sleep(2)
        txt = page.evaluate("() => document.body.innerText")
        if "发布成功" in txt or "已发布" in txt or "审核中" in txt or "作品管理" in txt:
            return PublishResult("douyin", "ok", method="cdp", final=txt[:200])
        if "重新上传" in txt:
            return PublishResult("douyin", "ok", method="cdp", final=txt[:200])
        if "发布失败" in txt or "上传失败" in txt or "请检查网络" in txt:
            return PublishResult("douyin", "fail", method="cdp", error="publish_failed_in_page", body=txt[:300])
        cur_url = page.url
        if 'content/manage' in cur_url:
            return PublishResult("douyin", "ok", method="cdp", final=cur_url)

    return PublishResult("douyin", "partial", method="cdp", error="timeout_waiting_for_publish_confirm")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish."""
    from playwright.sync_api import sync_playwright
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "creator.douyin.com" in pg.url), None)
        if page is None:
            page = ctx.new_page()
        try:
            return publish_on_page(page, title=title, description=description, video=video, topics=topics, location=location)
        except Exception as e:
            return PublishResult("douyin", "fail", method="cdp", error=str(e)[:200])