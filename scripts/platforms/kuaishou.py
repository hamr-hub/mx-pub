"""
Kuaishou (快手) publisher.

Kuaishou creator platform:
- https://cp.kuaishou.com/  (creator home)
- https://cp.kuaishou.com/article/publish/video  (video publish)

API endpoints (reverse-engineered):
- POST /rest/pc/photo/photoUpload  (upload video)
- POST /rest/pc/photo/publish  (publish post)

The web API uses custom sign parameter (X-Plus-App-Sign etc.).
The simpler path: Playwright CDP with manual form interaction.

Strategy:
1. Try direct API: requires sign parameter (complex)
2. Fall back to browser automation
"""
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Kuaishou API requires X-Plus-App-Sign header (custom HMAC).
    Stubbed.
    """
    return PublishResult("kuaishou", "fail", method="api", error="kuaishou_api_requires_sign_use_browser")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Playwright + CDP: navigate to kuaishou creator, upload, fill, click 发布.
    Kuaishou: title max 30 chars.
    """
    title = (title or "")[:30]  # kuaishou title cap
    from playwright.sync_api import sync_playwright

    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "kuaishou.com" in pg.url), None)

        if page is None:
            page = ctx.new_page()
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            page = next((pg for pg in ctx.pages if "kuaishou.com" in pg.url and "publish" in pg.url), None)
            if not page:
                return PublishResult("kuaishou", "fail", method="cdp", error="no_kuaishou_tab")

        # Always navigate to the publish page to reset state between batches
        page.bring_to_front()
        page.set_viewport_size({"width": 1440, "height": 900})
        # Force navigate to ensure clean state
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        time.sleep(2)

        # Upload (file input is hidden; just attach directly)
        try:
            time.sleep(2)
            inp = page.locator("input[type=file]").first
            inp.set_input_files(video, timeout=60000)
        except Exception as e:
            return PublishResult("kuaishou", "fail", method="cdp", error=f"upload: {e}")

        # Wait for upload
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

        # Click 发布 (button is custom DIV with parent class _button-primary)
        try:
            # Scroll to bottom to make button visible
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            # Find the publish button - it's a DIV with class containing _button-primary
            clicked = page.evaluate("""(() => {
                // Look for parent with class _button-primary
                const all = document.querySelectorAll('div');
                for (const d of all) {
                    const cls = (d.className || '').toString();
                    if (cls.includes('_button-primary') && d.innerText.trim() === '发布') {
                        d.scrollIntoView({block: 'center'});
                        d.click();
                        return 'clicked_primary';
                    }
                }
                // Fallback: any clickable element with text '发布'
                for (const e of document.querySelectorAll('*')) {
                    const t = (e.innerText || '').trim();
                    if (t === '发布' && e.children.length === 0 && e.offsetParent !== null) {
                        e.scrollIntoView({block: 'center'});
                        e.click();
                        return 'clicked_text';
                    }
                }
                return 'not_found';
            })()""")
            if clicked == "not_found":
                # Try Playwright locators
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
            # '失败' in body alone is too noisy (could be UI text, recommendations, etc)
            # Only treat as failure if explicitly an error message
            if any(err in txt for err in ["发布失败，请", "提交失败，请", "上传失败", "网络错误", "请检查网络"]):
                return PublishResult("kuaishou", "fail", method="cdp", error="publish_failed_in_page")

        # If we waited 30s with no explicit failure, treat as success since the click went through
        # Check if URL changed to /article/manage (success) or check for upload complete state
        cur_url = page.url
        if 'article/manage' in cur_url:
            return PublishResult("kuaishou", "ok", method="cdp", final=cur_url)

        # Check if "重新上传" (re-upload) appears, meaning the publish state advanced
        state = page.evaluate("() => ({hasReupload: document.body.innerText.includes('重新上传'), url: location.href})")
        if state.get("hasReupload"):
            return PublishResult("kuaishou", "ok", method="cdp", final=state.get("url", ""))

        return PublishResult("kuaishou", "partial", method="cdp", error="timeout_no_redirect")
