"""Kuaishou (快手) publisher."""
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"


def _match_url():
    """Predicate factory: matches kuaishou publish URLs."""
    def match(url: str) -> bool:
        return "cp.kuaishou.com/article/publish" in url
    return match


def _setup_page(page):
    """Navigate to the publish page if not already there."""
    if "publish" not in page.url:
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
    return page


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    return PublishResult("kuaishou", "fail", method="api", error="kuaishou_api_requires_sign_use_browser")


def publish_on_page(page, *, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Publish using an existing Playwright page (shared-session mode)."""
    title = (title or "")[:30]  # kuaishou title cap
    page.bring_to_front()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

    # Upload (page has 2 file inputs: video + image. Use video one explicitly)
    try:
        time.sleep(2)
        inp = page.locator("input[type=file][accept*='video']").first
        inp.set_input_files(video, timeout=60000, no_wait_after=True)
    except Exception as e:
        return PublishResult("kuaishou", "fail", method="cdp", error=f"upload: {e}")

    # Wait for upload to complete
    for i in range(120):
        time.sleep(1)
        state = page.evaluate("""(() => ({
            hasTitle: !!document.querySelector('input[placeholder*="标题"], input[placeholder*="描述"]'),
            uploading: document.body.innerText.includes('上传中'),
            body: document.body.innerText.slice(0, 200)
        }))()""")
        if state.get("hasTitle") and not state.get("uploading"):
            break

    # Fill form
    try:
        for sel, txt in [
            ('input[placeholder*="标题"]', title),
            ('textarea[placeholder*="描述"]', description),
            ('.ql-editor, [contenteditable=true]', description),
        ]:
            el = page.locator(sel).first
            if el.count() > 0:
                el.fill(txt)
                break
    except Exception:
        pass

    time.sleep(2)

    # Click 发布
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        clicked = page.evaluate("""(() => {
            const all = document.querySelectorAll('div');
            for (const d of all) {
                const cls = (d.className || '').toString();
                const t = (d.innerText || '').trim();
                if (cls.includes('_button-primary') && t === '发布') {
                    d.scrollIntoView({block: 'center'});
                    d.click();
                    return 'clicked_primary';
                }
            }
            const btns = Array.from(document.querySelectorAll('*')).filter(e => {
                const t = (e.innerText || '').trim();
                return t === '发布' && e.children.length === 0 && e.offsetParent !== null;
            });
            if (btns.length > 0) {
                btns[btns.length - 1].scrollIntoView({block: 'center'});
                btns[btns.length - 1].click();
                return 'clicked_text';
            }
            return 'not_found';
        })()""")
        if clicked == "not_found":
            btn = page.locator('button:has-text("发布")').first
            if btn.count() > 0:
                btn.click(timeout=10000)
            else:
                return PublishResult("kuaishou", "fail", method="cdp", error=f"no_publish_button: {clicked}")
    except Exception as e:
        return PublishResult("kuaishou", "fail", method="cdp", error=f"no_publish_button: {e}")

    # Wait for confirmation
    for i in range(30):
        time.sleep(1)
        txt = page.evaluate("() => document.body.innerText")
        if "发布成功" in txt or "已发布" in txt or "审核中" in txt:
            return PublishResult("kuaishou", "ok", method="cdp", final=txt[:200])
        if any(err in txt for err in ["发布失败，请", "提交失败，请", "上传失败", "网络错误", "请检查网络"]):
            return PublishResult("kuaishou", "fail", method="cdp", error="publish_failed_in_page")

    cur_url = page.url
    if 'article/manage' in cur_url:
        return PublishResult("kuaishou", "ok", method="cdp", final=cur_url)

    state = page.evaluate("() => ({hasReupload: document.body.innerText.includes('重新上传'), url: location.href})")
    if state.get("hasReupload"):
        return PublishResult("kuaishou", "ok", method="cdp", final=state.get("url", ""))

    return PublishResult("kuaishou", "partial", method="cdp", error="timeout_no_redirect")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish — opens its own Playwright session."""
    from playwright.sync_api import sync_playwright
    title = (title or "")[:30]
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if _match_url()(pg.url)), None)
        if page is None:
            page = ctx.new_page()
        try:
            return publish_on_page(page, title=title, description=description, video=video, topics=topics, location=location)
        except Exception as e:
            return PublishResult("kuaishou", "fail", method="cdp", error=str(e)[:200])