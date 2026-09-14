"""Douyin (抖音) publisher — signal-driven fast path.

Page map (2026-09 analysis, creator.douyin.com/creator-micro/content/publish):
- Title:  input.semi-input[placeholder*="填写作品标题"] (max 30 chars)
- Desc:   contenteditable div.editor-kit-container (NOT a textarea — fill() misses it)
- Topic:  focus editor, type "#word" -> suggestion dropdown -> Enter to pick
- AI declaration: "请选择自主声明" modal -> radio "内容由AI生成" -> 确定
- Collection: "请选择合集" semi-select (attach_douyin_to_collection)
- Button: button.primary-cECiOJ with exact text 发布
- Upload chain: ApplyUploadInner -> tos fragments -> CommitUploadInner -> transend
  (~5s on short clips); fast_detect/poll is 发文助手 and does NOT block publishing.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from publisher import PublishResult
from core.watch import (ApiWatch, await_publish, is_fatal_connection_error,
                        wait_dom)
from core.dialog_guard import attach_dialog_guard, clear_contenteditable
from mx_collections import attach_douyin_to_collection

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/publish"

TITLE_MAX = 30
DESC_MAX = 900
# Binary blob upload dominates: ~140s observed for 12MB on slow uplink.
UPLOAD_TIMEOUT_S = 420
PUBLISH_TIMEOUT_S = 25

# POST endpoints that confirm a successful create. NOTE: match on path
# segments, not bare substrings — "post" is a substring of "publish" and
# /aweme/v1/open/publish/limit_app_groups/ (a config fetch) false-matched.
PUBLISH_API_PATTERNS = (
    ("aweme/v1", "create"),
    ("/creator-micro/", "publish"),
    ("creator.douyin.com", "/create"),
)


def _match_url():
    def match(url: str) -> bool:
        return "creator.douyin.com" in url
    return match


def _setup_page(page):
    """Navigate to a clean publish page and wait for the upload zone."""
    page.bring_to_front()
    attach_dialog_guard(page)
    page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
    ready = wait_dom(
        page,
        'document.querySelector("input[type=file][accept*=video]") !== null',
        timeout=30,
    )
    return page if ready else page


def _set_ai_generated_declaration(page) -> bool:
    """Mark 自主声明 = 内容由AI生成 (required compliance for AI-generated clips)."""
    try:
        clicked = page.evaluate(r"""() => {
            const els = Array.from(document.querySelectorAll('*'));
            const t = els.find(e => (e.innerText || '').trim() === '请选择自主声明'
                && e.getBoundingClientRect().width > 0);
            if (!t) return false;
            t.scrollIntoView({block: 'center'});
            t.click();
            return true;
        }""")
        if not clicked:
            return False
        time.sleep(1.0)
        picked = page.evaluate(r"""() => {
            const labels = Array.from(document.querySelectorAll('label, div, span'));
            const row = labels.find(e => (e.innerText || '').trim().startsWith('内容由AI生成')
                && e.getBoundingClientRect().width > 200);
            if (!row) return false;
            const radio = row.querySelector('input[type=radio]') || row;
            radio.click();
            return true;
        }""")
        if not picked:
            page.keyboard.press("Escape")
            return False
        time.sleep(0.4)
        page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll('button'))
                .filter(b => b.getBoundingClientRect().width > 0 && !b.disabled);
            const ok = btns.find(b => (b.innerText || '').trim() === '确定');
            if (ok) ok.click();
        }""")
        time.sleep(0.6)
        return True
    except Exception:
        return False


def _add_topics(page, topics: list) -> None:
    """Type each '#word' in the editor and accept the first suggestion (Enter)."""
    for word in topics[:3]:
        tag = word.lstrip("#")
        try:
            page.keyboard.insert_text(f" #{tag}")
        except Exception:
            page.keyboard.type(f" #{tag}", delay=10)
        time.sleep(1.2)
        # Suggestion dropdown visible -> pick first via Enter; else keep plain text
        has_dd = page.evaluate(r"""() => Array.from(document.querySelectorAll(
            '[class*=mention] [class*=item], [class*=topic] [class*=item], [role=listbox] [role=option]'))
            .some(e => e.getBoundingClientRect().width > 0)""")
        if has_dd:
            page.keyboard.press("Enter")
            time.sleep(0.3)


def _fill_form(page, *, title, description, topics) -> None:
    title_inp = page.locator('input[placeholder*="填写作品标题"]').first
    title_inp.fill((title or "")[:TITLE_MAX], timeout=10000)

    editor = page.locator(".editor-kit-container [contenteditable=true], .editor-kit-container").first
    clear_contenteditable(page, editor)
    time.sleep(0.3)
    if description:
        page.keyboard.insert_text(description[:DESC_MAX])
        time.sleep(0.3)
    if topics:
        _add_topics(page, topics)


def _click_publish(page) -> str:
    """Click the primary 发布 button. Returns strategy name or 'not_found'."""
    clicked = page.evaluate(r"""() => {
        const btns = Array.from(document.querySelectorAll('button'))
            .filter(b => (b.innerText || '').trim() === '发布'
                && b.getBoundingClientRect().width > 0 && !b.disabled);
        const primary = btns.find(b => (b.className || '').includes('primary')) || btns[btns.length - 1];
        if (primary) {
            primary.scrollIntoView({block: 'center'});
            primary.click();
            return 'clicked';
        }
        return 'not_found';
    }""")
    if clicked == "clicked":
        return clicked
    try:
        btn = page.get_by_role("button", name="发布", exact=True).last
        btn.click(timeout=8000)
        return "clicked_role"
    except Exception:
        return "not_found"


def _publish_api_ok(data) -> bool:
    """Real create responses carry the new work id; config fetches don't."""
    if not isinstance(data, dict):
        return False
    if (data.get("status_code") in (0, None) or data.get("statusCode") in (0, None)) \
            and (data.get("item_id") or data.get("aweme_id")
                 or data.get("itemId") or data.get("awemeId")):
        return True
    return False


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    return PublishResult("douyin", "fail", method="api",
                         error="douyin_api_requires_xbogus_signature_use_browser")


def publish_on_page(page, *, title, description, video, topics=None, location=None,
                    pre_publish=None, **kwargs) -> PublishResult:
    """One upload->publish cycle on an already-open Douyin tab."""
    t0 = time.time()
    page.set_viewport_size({"width": 1440, "height": 900})
    _setup_page(page)

    watch = ApiWatch(page)
    try:
        page.locator("input[type=file][accept*='video']").first.set_input_files(
            video, timeout=30000, no_wait_after=True)
    except Exception as e:
        if is_fatal_connection_error(e):
            raise
        return PublishResult("douyin", "fail", method="cdp", error=f"upload: {e}")

    # Ready when transcode register (transend) returned and the form rendered.
    transend = watch.wait_url("transend", timeout=UPLOAD_TIMEOUT_S)
    form_ready = wait_dom(
        page,
        'document.querySelector(\'input[placeholder*="填写作品标题"]\') !== null'
        ' && !document.body.innerText.includes("上传中")',
        timeout=20,
    )
    if not transend and not form_ready:
        return PublishResult("douyin", "fail", method="cdp", error="upload_not_ready_timeout")
    print(f"[douyin] upload ready {time.time()-t0:.0f}s", flush=True)
    # Cover/analysis settle briefly (no fixed long sleep)
    wait_dom(page, '!document.body.innerText.includes("处理中")', timeout=15)

    try:
        _fill_form(page, title=title, description=description, topics=topics)
    except Exception as e:
        if is_fatal_connection_error(e):
            raise
        return PublishResult("douyin", "fail", method="cdp", error=f"fill_form: {e}")

    _set_ai_generated_declaration(page)

    if kwargs.get("collection"):
        try:
            attach_douyin_to_collection(page, collection_name=kwargs["collection"])
        except Exception as e:
            print(f"[douyin] collection attach skipped: {e}")

    if callable(pre_publish):
        try:
            pre_publish(page)
        except Exception as e:
            print(f"[douyin] pre_publish hook error: {e}")

    start_index = len(watch.events)
    clicked = _click_publish(page)
    if clicked == "not_found":
        return PublishResult("douyin", "fail", method="cdp", error="no_publish_button")

    dom_ok = ('document.body.innerText.includes("发布成功")'
              ' || document.body.innerText.includes("审核中")'
              ' || location.href.includes("content/manage")')
    dom_fail = ('document.body.innerText.includes("发布失败")'
                ' || document.body.innerText.includes("上传失败")')
    print(f"[douyin] form+topics+decl done {time.time()-t0:.0f}s, clicking 发布", flush=True)
    kind, ev, data, dom_text = await_publish(
        watch, start_index, PUBLISH_API_PATTERNS, page,
        dom_success=dom_ok, dom_fail=dom_fail, timeout=PUBLISH_TIMEOUT_S,
        api_validator=_publish_api_ok)

    dur = round(time.time() - t0, 1)
    if kind == "api":
        api_path = ev["path"]
        if _publish_api_ok(data):
            return PublishResult("douyin", "ok", method="cdp", duration_s=dur,
                                 final=f"api_ok {api_path}")
        # Non-zero status code -> genuine failure; surface the platform message.
        msg = (data or {}).get("status_msg") or (data or {}).get("message") or str(data)[:150]
        return PublishResult("douyin", "fail", method="cdp", duration_s=dur,
                             error=f"publish_api_rejected: {msg}")
    if kind == "dom":
        return PublishResult("douyin", "ok", method="cdp", duration_s=dur, final=dom_text[:150])
    if kind == "fail":
        return PublishResult("douyin", "fail", method="cdp", duration_s=dur,
                             error="publish_failed_in_page", body=(dom_text or "")[:200])
    # Unknown endpoint may have fired — log recent POSTs for diagnosis, trust submit
    debug = watch.recent_paths(8)
    print(f"[douyin] no confirm signal; recent POSTs: {debug}")
    return PublishResult("douyin", "partial", method="cdp", duration_s=dur,
                         error="timeout_waiting_for_confirm", recent=" | ".join(debug[-4:]))


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish (opens its own Playwright session)."""
    from playwright.sync_api import sync_playwright
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "creator.douyin.com" in pg.url), None)
        if page is None:
            page = ctx.new_page()
        try:
            return publish_on_page(page, title=title, description=description,
                                   video=video, topics=topics, location=location, **kwargs)
        except Exception as e:
            return PublishResult("douyin", "fail", method="cdp", error=str(e)[:200])
