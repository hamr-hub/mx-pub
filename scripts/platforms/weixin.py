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


def publish_via_api(*, title, description, video, topics=None, location=None, cookies=None, **kwargs) -> PublishResult:
    """Direct API publish. Requires pre-uploaded video (media already on CDN).
    Weixin has heavy server-side validation (errCode 300002) — API not implemented.
    """
    return PublishResult("weixin", "fail", method="api", error="weixin_api_blocked_use_browser")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Weixin 视频号: title max 14 chars (server enforces)."""
    title = (title or "")[:14]
    from playwright.sync_api import sync_playwright

    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "channels.weixin" in pg.url), None)
        if not page:
            return PublishResult("weixin", "fail", method="cdp", error="no_weixin_tab")

        if "platform/post/create" not in page.url:
            page.bring_to_front()
            page.set_viewport_size({"width": 1440, "height": 900})
            try:
                page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                return PublishResult("weixin", "fail", method="cdp", error=f"nav: {e}")
            time.sleep(10)

        # Wait for file input to appear on OUTER page
        for _ in range(25):
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

        # Wait for upload to complete (text changes from "上传" to cover/preview state)
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
            # Title (短标题 input)
            title_inp = iframe_loc.locator('input[placeholder*="短标题"]')
            if title_inp.count() > 0:
                title_inp.first.fill(title, timeout=10000)
        except Exception:
            pass

        try:
            # Description (.input-editor contenteditable)
            ce = iframe_loc.locator(".input-editor")
            if ce.count() > 0:
                ce.first.click()
                ce.first.fill(f"{description} {' '.join(topics or [])}", timeout=10000)
        except Exception:
            pass

        time.sleep(2)

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

        # Wait for confirmation (URL change to manage page, or "已发表" text)
        for _ in range(45):
            cur = page.url
            if "post/list" in cur or "manage" in cur:
                return PublishResult("weixin", "ok", method="cdp", final=cur)
            body = page.evaluate("document.body.innerText")
            if "已发表" in body or "发布成功" in body:
                return PublishResult("weixin", "ok", method="cdp", final=body[:200])
            time.sleep(1)

        return PublishResult("weixin", "partial", method="cdp", error="timeout_waiting_for_publish_confirm")