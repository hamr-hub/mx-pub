"""
Xiaohongshu (小红书) publisher.

API reality check: xhs does NOT expose a clean public post-creation API.
- /api/sns/web/v1/.../upload (video upload, requires signed policy)
- /api/sns/web/v1/.../post (publish, requires X-t sign + cookies + captcha)

Since the upload/publish endpoints require complex HMAC signing + captcha
that reverse-engineering is fragile, the API path is: **upload only**.
After upload, fall back to browser automation to click "发布".

Strategy:
1. Try direct upload (chunked POST to upload API, if we have cookies)
2. Fall back to browser automation (proven working path)

Required: xhs must be opened in Chrome (cdp://9222) with valid session.
"""
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult


UPLOAD_URL = "https://edith.xiaohongshu.com/api/sns/web/v1/upload"
# NEW URL (2026-08-25: old /publish/short-video-from-local returns "页面不见了")
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?from=menu&target=video"
EDITOR_URL = "https://creator.xiaohongshu.com/publish/publish?from=menu&target=video"
CDP_URL_DEFAULT = "http://127.0.0.1:9222"


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Direct upload only. The post-create step still needs browser.

    For xiaohongshu, the API upload requires:
    - X-s, X-t headers (HMAC signed timestamp + nonce)
    - Files in multipart/form-data with policy
    - cookies for user session

    This is a placeholder; the working path is publish_via_browser.
    Returns fail so the orchestrator falls through to browser.
    """
    return PublishResult("xhs", "fail", method="api", error="xhs_api_not_implemented_use_browser")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Proven path. Uses Playwright + CDP to:
    1. Open xiaohongshu creator editor
    2. Upload video via file input
    3. Wait for cover generation
    4. Fill title + content
    5. Click publish button
    6. Wait for "上传中" to disappear

    Requires Chrome @ port 9222 with an open xhs tab (or one will be opened).
    """
    from playwright.sync_api import sync_playwright

    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    # XHS 标题硬上限 20 字符 — 超出会让 publish 按钮不渲染 (no_btn_found)
    title = (title or "")[:20]
    topic_str = " ".join(f"#{t}" if not t.startswith("#") else t for t in (topics or []))

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "xiaohongshu.com" in pg.url and "publish" in pg.url), None)

        if page is None:
            page = ctx.new_page()
            page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            # Find the editor tab (xhs opens the actual editor in a new tab)
            page = next((pg for pg in ctx.pages if "xiaohongshu.com" in pg.url and "publish" in pg.url), None)
            if page is None:
                return PublishResult("xhs", "fail", method="cdp", error="no_editor_after_navigate")

        page.bring_to_front()
        page.set_viewport_size({"width": 1280, "height": 800})
        time.sleep(1)

        # Upload video (xhs accept attribute lists extensions like .mp4,.mov, not 'video' word)
        try:
            inp = page.locator("input[type=file][accept*='.mp4']").first
            inp.set_input_files(video, timeout=15000, no_wait_after=True)
        except Exception as e:
            return PublishResult("xhs", "fail", method="cdp", error=f"upload_failed: {e}")

        # Wait for cover generation (typically 25s)
        for _ in range(45):
            state = page.evaluate("""(() => ({
                hasCover: document.body.innerText.includes('封面') || document.querySelectorAll('.cover, [class*="cover"]').length > 0,
                hasBtn: document.querySelectorAll('xhs-publish-btn, [class*="publish-btn"]').length,
                body: document.body.innerText.slice(0, 300)
            }))()""")
            if state.get("hasCover") and state.get("hasBtn", 0) > 0:
                break
            time.sleep(1)

        # Fill title
        try:
            page.locator('input[placeholder*="标题"]').first.fill(title, timeout=5000)
        except Exception as e:
            pass  # title may be optional, but usually required

        # Fill content
        try:
            ce = page.locator(".tiptap.ProseMirror, [contenteditable=true]").first
            ce.click()
            ce.fill(f"{description} {topic_str}".strip())
        except Exception as e:
            pass

        # Find and click the actual publish button
        # NOTE 2026-08-26: New xhs UI structure:
        # - <xhs-publish-btn> is a custom-element PLACEHOLDER with no rendered children
        # - The real visible button is OUTSIDE the placeholder, in regular DOM:
        #   div.btn-wrapper > div.btn-inner containing text "发布笔记"
        # - Clicking xhs-publish-btn triggers Vue preview state but doesn't submit
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

        # Wait for upload to complete
        for _ in range(60):
            time.sleep(1)
            still_uploading = page.evaluate("(() => document.body.innerText.includes('上传中'))()")
            if not still_uploading:
                # Check for success indicator
                final = page.evaluate("(() => document.body.innerText.slice(0, 500))()")
                if "成功" in final or "已发布" in final or "创作" in final:
                    return PublishResult("xhs", "ok", method="cdp", clicked=clicked, final=final[:200])
                # Wait a bit more, might be processing
                continue

        return PublishResult("xhs", "partial", method="cdp", error="upload_text_still_showing_after_60s", clicked=clicked)
