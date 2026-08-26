"""
Douyin (抖音) publisher.

Douyin creator platform has a public API at:
- https://creator.douyin.com/creator-micro/api/...

Upload flow:
1. POST /api/upload/video/create/  (get upload_id, aws S3 endpoint)
2. PUT to S3 presigned URL  (chunked upload)
3. POST /api/upload/video/complete/  (finalize)
4. POST /api/creator/post/publish/  (create post)

The web-based API requires X-T token (HMAC over request body + timestamp).
Reverse-engineering this signature is non-trivial; we use browser automation
as the primary path.

Strategy:
1. Try direct API: requires session cookies + X-T signing
2. Fall back to Playwright CDP: navigate, fill form, click 发布
"""
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/publish"
VIDEO_PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/video/upload"


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Douyin API requires:
    - aBogus / X-Bogus signature (custom anti-bot signing)
    - msToken (set as cookie)
    - webid
    - user_unique_id

    These are JS-generated and rotate frequently. Stubbed for now.
    """
    return PublishResult("douyin", "fail", method="api", error="douyin_api_requires_xbogus_signature_use_browser")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Douyin: title max 30 chars.
    Playwright + CDP: navigate to douyin creator, upload video, fill form, click 发布.
    """
    title = (title or "")[:30]  # douyin title cap
    from playwright.sync_api import sync_playwright

    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "douyin.com" in pg.url), None)

        if page is None:
            page = ctx.new_page()
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            page = next((pg for pg in ctx.pages if "douyin.com" in pg.url and ("publish" in pg.url or "upload" in pg.url)), None)
            if not page:
                return PublishResult("douyin", "fail", method="cdp", error="no_douyin_tab")

        # If existing tab is on /content/manage (not /content/publish or /upload)
        page.bring_to_front()
        page.set_viewport_size({"width": 1440, "height": 900})
        # Check the path component specifically, not the query string
        from urllib.parse import urlparse
        path = urlparse(page.url).path
        if not (path.endswith('/publish') or path.endswith('/upload') or '/publish/' in path or '/upload/' in path):
            print(f"[douyin] navigating from {page.url} to {PUBLISH_URL}")
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
        time.sleep(2)

        # Upload video
        try:
            inp = page.locator("input[type=file]").first
            inp.set_input_files(video, timeout=20000)
        except Exception as e:
            return PublishResult("douyin", "fail", method="cdp", error=f"upload: {e}")

        # Wait for upload (douyin shows progress bar)
        for i in range(120):
            time.sleep(1)
            state = page.evaluate("""(() => {
                const t = document.body.innerText;
                return {
                    hasTitle: !!document.querySelector('input[placeholder*="标题"], input[placeholder*="描述"]'),
                    uploading: t.includes('上传中') || t.includes('上传视频'),
                    body: t.slice(0, 200)
                };
            })()""")
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
        except Exception as e:
            pass

        time.sleep(2)

        # Click 发布 (button has name conflict - use .first or scroll-then-click by class)
        try:
            # Scroll to bottom to ensure publish button is visible
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            clicked = page.evaluate("""(() => {
                // Look for div with class containing primary or button-primary
                const all = document.querySelectorAll('button, div');
                for (const d of all) {
                    const cls = (d.className || '').toString();
                    const t = (d.innerText || '').trim();
                    if ((cls.includes('button--primary') || cls.includes('primary-btn')) && t === '发布') {
                        d.scrollIntoView({block: 'center'});
                        d.click();
                        return 'clicked_primary';
                    }
                }
                // Fallback: find the publish button (last visible one with text 发布)
                const btns = Array.from(document.querySelectorAll('button')).filter(b => b.innerText.trim() === '发布' && b.offsetParent !== null);
                if (btns.length > 0) {
                    btns[btns.length - 1].scrollIntoView({block: 'center'});
                    btns[btns.length - 1].click();
                    return 'clicked_button_last';
                }
                return 'not_found';
            })()""")
            if clicked == "not_found":
                # Last fallback: get_by_role with .first
                btn = page.get_by_role("button", name="发布").first
                btn.click(timeout=10000)
        except Exception as e:
            return PublishResult("douyin", "fail", method="cdp", error=f"no_publish_button: {e}")

        # Wait for success (douyin publish can take up to 3 min for video processing)
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
