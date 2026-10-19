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
from core.covers import make_covers
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


def _click_exact(page, text: str, *, min_w: float = 50,
                  center: bool = False) -> bool:
    """Click the most specific visible element with exact text.

    center=True restricts to elements centered in the viewport (used for the
    vertical-cover guide popup, whose label also exists as a tab/footer)."""
    js = r"""([text, minW, center]) => {
      const vw = window.innerWidth;
      const hits = [];
      for (const el of document.querySelectorAll('button, div, span')) {
        if ((el.innerText || '').trim() !== text) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height || r.width < minW) continue;
        const centered = Math.abs((r.x + r.width / 2) - vw / 2) < vw / 4;
        if (center && !centered) continue;
        hits.push({el, depth: (() => { let n = el, d = 0;
          while (n.parentElement && d < 20) { n = n.parentElement; d++; }
          return d; })(), w: r.width});
      }
      if (!hits.length) return false;
      // leaf-most, then smallest width (the action button among containers)
      hits.sort((a, b) => a.depth - b.depth || a.w - b.w);
      hits[0].el.scrollIntoView({block: 'center'});
      hits[0].el.click();
      return true;
    }"""
    return bool(page.evaluate(js, [text, min_w, center]))


def _direct_cover_input(page):
    """Locator for the direct '上传封面' zone's hidden input.

    Four hidden image inputs exist in the cover modal; the first two belong to
    '生成参考图' (AI reference). The direct-cover zone is the one whose
    container carries '点击上传文件或拖拽文件到这里'."""
    idx = page.evaluate(r"""() => {
      const ins = Array.from(document.querySelectorAll(
        '.semi-upload-hidden-input, .semi-upload-hidden-input-replace'));
      for (let i = 0; i < ins.length; i++) {
        let n = ins[i];
        for (let j = 0; j < 8; j++) {
          n = n.parentElement;
          if (!n) break;
          if ((n.innerText || '').includes('点击上传文件或拖拽文件到这里'))
            return i;
        }
      }
      return -1;
    }""")
    if idx < 0:
        return None
    return page.locator(
        ".semi-upload-hidden-input, .semi-upload-hidden-input-replace").nth(idx)


def _set_covers(page, cover_h: str, cover_v: str) -> bool:
    """Upload custom horizontal (4:3) + vertical (3:4) covers.

    Proven flow (2026-09 analysis): open modal -> direct-upload h -> 完成 ->
    guide popup 设置竖封面 -> direct-upload v -> 完成/暂不设置. Both files commit
    even when the second guide popup is dismissed with 暂不设置."""
    try:
        _click_exact(page, "选择封面", min_w=80)
        time.sleep(2.5)
        h_inp = _direct_cover_input(page)
        if h_inp is None:
            print("[douyin] cover: direct input not found")
            return False
        h_inp.set_input_files(cover_h)
        time.sleep(2.5)
        _click_exact(page, "完成", min_w=60)
        time.sleep(1.5)
        # Vertical-cover guide popup; its action switches to the vertical tab.
        _click_exact(page, "设置竖封面", min_w=80, center=True)
        time.sleep(2.0)
        v_inp = _direct_cover_input(page)
        if v_inp is not None:
            v_inp.set_input_files(cover_v)
            time.sleep(2.5)
        done = _click_exact(page, "完成", min_w=60)
        if not done:
            # Guide re-popup: 暂不设置 closes the modal; v already committed.
            _click_exact(page, "暂不设置", min_w=60, center=True)
        time.sleep(2.0)
        remaining = page.evaluate(r"""() => Array.from(
          document.querySelectorAll('div, button, span'))
          .filter(e => (e.innerText || '').trim() === '选择封面'
              && e.getBoundingClientRect().width > 80).length""")
        ok = remaining == 0
        print(f"[douyin] covers set h+v: {ok}")
        return ok
    except Exception as e:
        print(f"[douyin] covers skipped: {e}")
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
    return bool((data.get("status_code") in (0, None) or data.get("statusCode") in (0, None)) and (data.get("item_id") or data.get("aweme_id") or data.get("itemId") or data.get("awemeId")))


# 2026-10-14 /goal：上传就绪判定曾两度失效。旧版等 `transend` XHR —— 实测抓包
# 上传链根本没有名为 transend 的 JSON POST，等待必然烧满 420s；兜底的
# `!innerText.includes("上传中")` 又被常驻静态提示「如作品还在上传中」恒假化。
# 插桩实测的可信时序（0.5s 采样）：set_input_files 后约 0.8s 出现
# `.upload-progress-*` 卡片（0%→100%，伴随「取消上传 / 上传过程中」文案），
# 传完卡片先卸载（此时预览 <video> 尚未挂载），约 2s 后 video 出现且
# readyState 由 0 一次跳到 4、duration/videoWidth 可用。标题框 / 编辑器 /
# 「选择封面」在空页面就存在，不能当信号。
# 返回：busy=字节上传中 | idle=尚未开始 | meta=预览已挂载未解码 | done=可发布
_UPLOAD_STATE_JS = (
    "(() => {"
    " const c=document.querySelector('[class*=\"upload-progress-\"]');"
    " const busy=!!(c && (c.offsetWidth||c.offsetHeight));"
    " const txt=document.body.innerText;"
    " if (busy || txt.includes('取消上传') || txt.includes('上传过程中'))"
    "   return 'busy';"
    " const vs=[...document.querySelectorAll('video')];"
    " if (vs.some(v => v.readyState===4 && isFinite(v.duration)"
    "                 && v.duration>0 && v.videoWidth>0)) return 'done';"
    " if (vs.some(v => v.readyState>0)) return 'meta';"
    " return 'idle';"
    "})()"
)
# 连续 3 次 done（间隔 0.6s）才算就绪，跨过卡片卸载/预览挂载的抖动窗口。
_UPLOAD_DONE_STREAK = 3
# 没采到上传卡片也允许判定 done 的最早时刻：防极快/缓存上传卡片一闪而过，
# 同时挡住 t≈0 时旧页面残留预览造成的误判。
_UPLOAD_NO_CARD_GRACE_S = 15.0


def _wait_upload_ready(page, watch, timeout: int) -> bool:
    """Poll the upload state machine until a stable 'done'.

    `watch` stays in the signature for call-site symmetry; the upload chain
    emits no capturable completion XHR, so DOM state is the only signal.
    """
    deadline = time.time() + timeout
    t0 = time.time()
    saw_busy = False
    streak = 0
    while time.time() < deadline:
        try:
            state = page.evaluate(_UPLOAD_STATE_JS)
        except Exception as e:
            if is_fatal_connection_error(e):
                raise
            streak = 0
            time.sleep(0.6)
            continue
        if state == "busy":
            saw_busy = True
            streak = 0
        elif state == "done" and (saw_busy
                                  or time.time() - t0 > _UPLOAD_NO_CARD_GRACE_S):
            streak += 1
            if streak >= _UPLOAD_DONE_STREAK:
                return True
        else:
            streak = 0
        time.sleep(0.6)
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

    if not _wait_upload_ready(page, watch, UPLOAD_TIMEOUT_S):
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

    try:
        cover_h, cover_v = make_covers(video)
        _set_covers(page, cover_h, cover_v)
    except Exception as e:
        print(f"[douyin] cover generation skipped: {e}")

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
