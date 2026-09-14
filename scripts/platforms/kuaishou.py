"""Kuaishou (快手) publisher — signal-driven fast path.

Page map (2026-09 analysis, cp.kuaishou.com/article/publish/video):
- NO separate title field — only one contenteditable description editor
  (div[class*="_description"]); title text is folded into the description.
- Topic: type "#word" in the editor -> 智能话题 dropdown -> Enter; plain #tags
  are also auto-recognized when submitted.
- Stale-draft dialog ("继续编辑/放弃") must be dismissed with 放弃 before upload.
- Button: div._button-primary with exact text 发布.
- Upload chain: upload/pre -> fragment -> complete -> upload/finish (~3s on
  short clips). Editor appears immediately after finish.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from publisher import PublishResult
from core.watch import ApiWatch, wait_dom, await_publish
from core.dialog_guard import attach_dialog_guard

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"

DESC_MAX = 500
UPLOAD_TIMEOUT_S = 420
PUBLISH_TIMEOUT_S = 25

# Observed 2026-09: real create call is .../video/pc/submit; "publish" paths
# are config endpoints (realize/entrance, publish/time).
PUBLISH_API_PATTERNS = (
    ("/rest/cp/works/", "submit"),
    ("video/pc", "publish"),
)


def _match_url():
    def match(url: str) -> bool:
        return "cp.kuaishou.com/article/publish" in url
    return match


def _dismiss_draft(page) -> None:
    """Discard the 'continue editing last draft?' dialog if present."""
    try:
        page.evaluate(r"""() => {
            const els = Array.from(document.querySelectorAll('button, div, span, a'));
            const giveUp = els.find(e => (e.innerText || '').trim() === '放弃'
                && e.getBoundingClientRect().width > 0);
            if (giveUp) giveUp.click();
        }""")
        time.sleep(0.8)
    except Exception:
        pass


def _setup_page(page):
    page.bring_to_front()
    page.set_viewport_size({"width": 1440, "height": 900})
    attach_dialog_guard(page)
    page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
    wait_dom(page, 'document.querySelector("input[type=file][accept*=video]") !== null', timeout=30)
    _dismiss_draft(page)
    return page


def _fill_form(page, *, title, description, topics) -> None:
    editor = page.locator('[class*="_description"][contenteditable], [contenteditable=true]').first
    editor.click(timeout=10000)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Delete")
    time.sleep(0.3)
    text = description or title or ""
    page.keyboard.insert_text(text[:DESC_MAX])
    time.sleep(0.3)

    for word in (topics or [])[:5]:
        tag = word.lstrip("#")
        page.keyboard.insert_text(f" #{tag}")
        time.sleep(1.0)
        picked = page.evaluate(r"""() => {
            const items = Array.from(document.querySelectorAll('[class*=topic] [class*=item], [class*=ai-topics-item]'))
                .filter(e => e.getBoundingClientRect().width > 0);
            if (items.length) { items[0].click(); return true; }
            return false;
        }""")
        if not picked:
            # Plain "#tag" is auto-recognized as a smart topic on submit — leave as is.
            time.sleep(0.2)


def _click_publish(page) -> str:
    clicked = page.evaluate(r"""() => {
        const all = Array.from(document.querySelectorAll('button, div'))
            .filter(e => (e.innerText || '').trim() === '发布'
                && e.getBoundingClientRect().width > 0);
        const primary = all.find(e => (e.className || '').toString().includes('button-primary'));
        const target = primary || all[all.length - 1];
        if (target) {
            target.scrollIntoView({block: 'center'});
            target.click();
            return 'clicked';
        }
        return 'not_found';
    }""")
    if clicked == "clicked":
        return clicked
    try:
        page.locator('button:has-text("发布"), div:has-text("发布")').last.click(timeout=8000)
        return "clicked_locator"
    except Exception:
        return "not_found"


def _publish_api_ok(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("result") == 1 or data.get("status") == 1:
        return True
    if data.get("error_code") in (0, None) and data.get("error_msg") in (None, "", "成功"):
        return True
    return False


def publish_via_api(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    return PublishResult("kuaishou", "fail", method="api",
                         error="kuaishou_api_requires_sign_use_browser")


def publish_on_page(page, *, title, description, video, topics=None, location=None,
                    pre_publish=None, **kwargs) -> PublishResult:
    """One upload->publish cycle on an already-open Kuaishou tab."""
    t0 = time.time()
    _setup_page(page)

    watch = ApiWatch(page)
    try:
        page.locator("input[type=file][accept*='video']").first.set_input_files(
            video, timeout=60000, no_wait_after=True)
    except Exception as e:
        return PublishResult("kuaishou", "fail", method="cdp", error=f"upload: {e}")

    # Race the finish XHR against editor DOM: on slow uplinks the binary blob
    # PUTs dominate (~3min/12MB) and both signals land within the same second.
    editor_js = ('document.querySelector(\'[class*="_description"][contenteditable],'
                 ' [contenteditable=true]\') !== null'
                 ' && document.body.innerText.includes("重新上传")')
    finished = editor_ready = False
    deadline_up = time.time() + UPLOAD_TIMEOUT_S
    editor_grace = None
    while time.time() < deadline_up:
        if not finished and watch.find("upload/finish") is not None:
            finished = True
            editor_grace = time.time() + 20
        if not editor_ready:
            try:
                editor_ready = page.evaluate(
                    f"() => {{ try {{ return !!({editor_js}); }} catch (e) {{ return false; }} }}")
            except Exception:
                pass
        if editor_ready:
            break
        # Editor DOM renders a beat after the finish XHR; short grace so we
        # never click before the contenteditable exists.
        if editor_grace is not None and time.time() > editor_grace:
            break
        time.sleep(0.5)
    print(f"[kuaishou] finish_signal={finished} editor_ready={editor_ready} "
          f"{time.time()-t0:.0f}s", flush=True)
    if not finished and not editor_ready:
        return PublishResult("kuaishou", "fail", method="cdp", error="upload_not_ready_timeout")

    try:
        _fill_form(page, title=title, description=description, topics=topics)
    except Exception as e:
        return PublishResult("kuaishou", "fail", method="cdp", error=f"fill_form: {e}")

    if callable(pre_publish):
        try:
            pre_publish(page)
        except Exception as e:
            print(f"[kuaishou] pre_publish hook error: {e}")

    time.sleep(0.8)  # let cover recommendation autosave settle
    start_index = len(watch.events)
    clicked = _click_publish(page)
    if clicked == "not_found":
        return PublishResult("kuaishou", "fail", method="cdp", error="no_publish_button")

    dom_ok = ('document.body.innerText.includes("发布成功")'
              ' || document.body.innerText.includes("已发布")'
              ' || document.body.innerText.includes("审核中")'
              ' || location.href.includes("article/manage")')
    dom_fail = ('document.body.innerText.includes("发布失败")'
                ' || document.body.innerText.includes("提交失败")')
    kind, ev, data, dom_text = await_publish(
        watch, start_index, PUBLISH_API_PATTERNS, page,
        dom_success=dom_ok, dom_fail=dom_fail, timeout=PUBLISH_TIMEOUT_S)

    dur = round(time.time() - t0, 1)
    if kind == "api":
        api_path = ev["path"]
        if _publish_api_ok(data):
            return PublishResult("kuaishou", "ok", method="cdp", duration_s=dur,
                                 final=f"api_ok {api_path}")
        msg = (data or {}).get("error_msg") or (data or {}).get("message") or str(data)[:150]
        return PublishResult("kuaishou", "fail", method="cdp", duration_s=dur,
                             error=f"publish_api_rejected: {msg}")
    if kind == "dom":
        return PublishResult("kuaishou", "ok", method="cdp", duration_s=dur, final=dom_text[:150])
    if kind == "fail":
        return PublishResult("kuaishou", "fail", method="cdp", duration_s=dur,
                             error="publish_failed_in_page", body=(dom_text or "")[:200])
    debug = watch.recent_paths(8)
    print(f"[kuaishou] no confirm signal; recent POSTs: {debug}")
    return PublishResult("kuaishou", "partial", method="cdp", duration_s=dur,
                         error="timeout_waiting_for_confirm", recent=" | ".join(debug[-4:]))


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish — opens its own Playwright session."""
    from playwright.sync_api import sync_playwright
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if _match_url()(pg.url)), None)
        if page is None:
            page = ctx.new_page()
        try:
            return publish_on_page(page, title=title, description=description,
                                   video=video, topics=topics, location=location, **kwargs)
        except Exception as e:
            return PublishResult("kuaishou", "fail", method="cdp", error=str(e)[:200])
