"""Weixin Channels (视频号) publisher — signal-driven fast path.

Wujie architecture (2026-09 analysis, channels.weixin.qq.com/platform/post/create):
- The host boots a Wujie micro-frontend. <iframe name=content> is ONLY the JS
  sandbox (it stays on /empty.html, 0x0); querying its document returns a static
  221-char shell and NEVER reflects app state. The real UI renders inside the
  host element <wujie-app>'s OPEN SHADOW ROOT. Playwright CSS locators pierce
  open shadow roots; raw JS must use document.querySelector('wujie-app')
  .shadowRoot explicitly.
- Create form mounts within ~5s of a direct goto (no bounce needed):
    video input: input[type=file][accept*=video] (first one; a later input is
                 the cover-image uploader)
    short title: input[placeholder*="短标题"] (<=14 chars)
    description: div.input-editor (contenteditable attr is "")
    topics: type "#word" in editor -> suggestion dropdown -> click/Enter
    publish:   visible enabled button, exact text 发表
- Upload chain: applyuploaddfs -> completepartuploaddfs -> post_clip_video ->
  post_clip_video_result (POLLING; first hit = queued, not done). Ready signal:
  a visible <video src="blob:"> with readyState>=1 plus auto-generated covers.
  Dedupe/caching means repeat uploads of the same clip can finish in seconds.
- Real create call (2026-09):
    POST /micro/content/cgi-bin/mmfinderassistant-bin/post/post_create
    body {"errCode":0,"data":{"baseResp":{"errcode":0}}}
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from publisher import PublishResult
from core.watch import ApiWatch
from core.dialog_guard import attach_dialog_guard

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
PUBLISH_URL = "https://channels.weixin.qq.com/platform/post/create"

TITLE_MAX = 14
# Binary upload + platform transcode ceiling for first-time clips (~3min/12MB).
UPLOAD_TIMEOUT_S = 420
# 2026-09: raised 20 -> 30 after 5/12 runs produced "no post_create" partials;
# the loop now re-clicks once at 7s and records validation toasts.
PUBLISH_TIMEOUT_S = 30

# Observed 2026-09: this is the real create call; helper_*/report paths are
# telemetry and must not match.
PUBLISH_API_PATTERNS = (
    ("mmfinderassistant-bin/post/post_create",),
)

_SR = "document.querySelector('wujie-app').shadowRoot"


def _sr(page, expr: str):
    """Evaluate an expression with `r` bound to the wujie shadow root."""
    return page.evaluate(f"() => {{ const r = {_SR}; return ( {expr} ); }}")


def _match_url():
    def match(url: str) -> bool:
        return "channels.weixin" in url
    return match


def _dismiss_tips(page) -> None:
    """Dismiss visible '我知道了' tip overlays inside the shadow app."""
    try:
        _sr(page, r"""Array.from(r.querySelectorAll('button'))
            .filter(b => b.getBoundingClientRect().width > 0
                && (b.innerText || '').trim() === '我知道了')
            .forEach(b => b.click())""")
    except Exception:
        pass


def _setup_page(page):
    page.bring_to_front()
    page.set_viewport_size({"width": 1440, "height": 900})
    attach_dialog_guard(page)
    # A stale unsaved clip triggers beforeunload, auto-accepted by the guard.
    page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
    return page


def _wait_ready(page, timeout: float = 60) -> bool:
    """True when the video file input exists in the wujie shadow app."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _sr(page, "r.querySelectorAll(\"input[type=file][accept*='video']\").length") > 0:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _add_topics(page, topics: list) -> None:
    for word in topics[:3]:
        tag = word.lstrip("#")
        try:
            page.locator("wujie-app .input-editor").first.click(timeout=5000)
            page.keyboard.insert_text(f" #{tag}")
        except Exception:
            continue
        time.sleep(1.0)
        try:
            picked = _sr(page, r"""(() => {
                const items = Array.from(r.querySelectorAll(
                    '[class*=topic] [class*=item], [class*=mention] [class*=item], li'))
                    .filter(e => e.getBoundingClientRect().width > 0
                        && (e.innerText || '').trim().length > 0
                        && (e.innerText || '').trim().length < 20);
                if (items.length) { items[0].click(); return true; }
                return false;
            })()""")
            if not picked:
                page.keyboard.press("Enter")
        except Exception:
            pass
        time.sleep(0.3)


TITLE_MIN = 6
GENERIC_TITLE_SUFFIX = "精彩瞬间"


def _build_short_title(title: str, description: str = "") -> str:
    """Channels requires 6-14 chars in 短标题. Short queue titles (4-5 chars,
    e.g. 冬日古堡) leave 发表 class-disabled with '标题至少6个字' — the 2026-09
    partial root cause. Extend with distinct description tokens, then suffix.

    Punctuation is stripped first: a middle dot in '圣像修复师·凝眸' left 发表
    class-disabled with NO validation toast (2026-09-15 canary batch), while
    identical-form titles without it posted fine."""
    t = re.sub(r"[^一-龥A-Za-z0-9]+", "", (title or "").strip())
    if len(t) >= TITLE_MIN:
        return t[:TITLE_MAX]
    rest = re.sub(re.escape(t), " ", description or "")
    for tok in re.findall(r"[一-龥A-Za-z0-9]+", rest):
        for piece in re.findall(r"[一-龥]{2,4}|[A-Za-z0-9]+", tok):
            if piece in t:
                continue
            if len(t) + len(piece) <= TITLE_MAX:
                t += piece
            if len(t) >= TITLE_MIN:
                return t[:TITLE_MAX]
    while len(t) < TITLE_MIN and len(t) < TITLE_MAX:
        room = min(TITLE_MIN - len(t), TITLE_MAX - len(t), len(GENERIC_TITLE_SUFFIX))
        t += GENERIC_TITLE_SUFFIX[:room]
    return t[:TITLE_MAX]


def _wait_button_enabled(page, timeout: float = 20):
    """Poll until 发表 loses weui-desktop-btn_disabled; return final state."""
    deadline = time.time() + timeout
    st = {}
    while time.time() < deadline:
        st = _publish_button_state(page) or {}
        if st.get("found") and not st.get("class_disabled") and not st.get("attr_disabled"):
            return st
        time.sleep(0.5)
    return st


def _fill_form(page, *, title, description, topics) -> None:
    short_title = _build_short_title(title, description)
    page.locator("wujie-app input[placeholder*='短标题']").first.fill(
        short_title, timeout=10000)

    editor = page.locator("wujie-app .input-editor").first
    editor.click(timeout=10000)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Delete")
    time.sleep(0.3)
    if description:
        page.keyboard.insert_text(description)
        time.sleep(0.3)
    if topics:
        _add_topics(page, topics)


def _set_video_annotation(page) -> bool:
    """视频标注 = 含AI生成内容 (compliance for AI-generated clips).

    The select and its popup both render inside the wujie open shadow root.
    2026-09 analysis: options list 无需标注/含AI生成内容/虚构剧情仅供娱乐/
    个人观点仅供参考/营销广告/自行拍摄/转载."""
    try:
        # JS el.click() leaves the popup 0x0 (it renders but never displays);
        # only Playwright's trusted click on .mark-tag-select opens it.
        page.locator("wujie-app .mark-tag-select").first.click(timeout=8000)
        time.sleep(0.8)
        page.locator("wujie-app .mark-tag-option",
                     has_text="含AI生成内容").first.click(timeout=5000)
        time.sleep(0.4)
        display = _sr(page,
                      "r.querySelector('.mark-tag-select .select-display')"
                      ".innerText.trim()")
        ok = display == "含AI生成内容"
        print(f"[weixin] video annotation: {display}")
        return ok
    except Exception as e:
        print(f"[weixin] video annotation skipped: {e}")
        return False


def _publish_button_state(page):
    """Locate the 发表 button; return center coords + disabled/toast diagnostics."""
    return _sr(page, r"""(() => {
        const btns = Array.from(r.querySelectorAll('button'))
            .filter(b => (b.innerText || '').trim() === '发表'
                && b.getBoundingClientRect().width > 0);
        if (!btns.length) return {found: false};
        const b = btns[btns.length - 1];
        const rc = b.getBoundingClientRect();
        const cls = (b.className || '').toString();
        const toastSel = '[class*=toast],[class*=tips],[class*=message],'
            + '[role=alert],[class*=notice],[class*=Toast]';
        const toasts = Array.from(r.querySelectorAll(toastSel))
            .filter(e => e.getBoundingClientRect().width > 0)
            .map(e => (e.innerText || '').trim())
            .filter(t => t && t.length <= 60);
        return {
            found: true,
            x: Math.round(rc.x + rc.width / 2),
            y: Math.round(rc.y + rc.height / 2),
            attr_disabled: !!b.disabled,
            class_disabled: /disabled/i.test(cls),
            cls: cls.slice(0, 80),
            toasts: Array.from(new Set(toasts)).slice(0, 5),
        };
    })()""")


def _click_publish(page) -> str:
    """Trusted click on the shadow 发表 button.

    History: JS el.click() was silently ignored for short-title partials; raw
    page.mouse.click(x,y) missed because the button sits at y~1260 in a nested
    wujie scroll container and shadow scrollIntoView did not move the viewport.
    A Playwright locator click pierces the open shadow root, auto-scrolls the
    real scroll container, and dispatches trusted events — primary path.
    Coordinate click is only a fallback after explicit shadow scrolling."""
    st = _publish_button_state(page)
    if not st or not st.get("found"):
        return "not_found"
    if st.get("attr_disabled") or st.get("class_disabled"):
        return f"disabled:{st.get('cls')}"
    try:
        btns = page.locator("wujie-app button")
        for i in range(btns.count() - 1, -1, -1):
            loc = btns.nth(i)
            try:
                if loc.inner_text(timeout=1000).strip() != "发表":
                    continue
                if not loc.is_visible():
                    continue
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=5000)
                return "clicked:locator"
            except Exception:
                continue
    except Exception:
        pass
    try:
        page.mouse.click(st["x"], st["y"])
    except Exception as e:
        return f"click_error:{e}"
    return f"clicked:mouse:{st.get('cls', '')[:30]}"


def _publish_api_ok(data: dict) -> bool:
    if not isinstance(data, dict) or data.get("errCode") != 0:
        return False
    base = ((data.get("data") or {}).get("baseResp") or {})
    return base.get("errcode", 0) == 0


def publish_via_api(*, title, description, video, topics=None, location=None, cookies=None, **kwargs) -> PublishResult:
    return PublishResult("weixin", "fail", method="api", error="weixin_api_blocked_use_browser")


def _wait_clip_ready(page, watch, t0: float) -> bool:
    """Wait until the uploaded clip is bound and transcoded (blob <video> live)."""
    deadline = time.time() + UPLOAD_TIMEOUT_S
    clip_seen = False
    while time.time() < deadline:
        if not clip_seen and watch.find("post_clip_video_result") is not None:
            clip_seen = True
        try:
            state = _sr(page, r"""(() => {
                const t = r.textContent || '';
                const vids = Array.from(r.querySelectorAll('video'))
                    .filter(v => v.getBoundingClientRect().width > 0);
                return {
                    ready: vids.some(v => (v.readyState || 0) >= 1
                        && (v.src || '').startsWith('blob:')),
                    uploading: t.includes('上传中'),
                    busy: t.includes('处理中') || t.includes('转码'),
                };
            })()""")
        except Exception:
            state = {"ready": False, "uploading": False, "busy": False}
        if state["ready"] and not state["uploading"] and not state["busy"]:
            print(f"[weixin] clip_seen={clip_seen} clip_ready=True "
                  f"{time.time()-t0:.0f}s", flush=True)
            return True
        time.sleep(0.8)
    print(f"[weixin] clip_seen={clip_seen} clip_ready=False {time.time()-t0:.0f}s", flush=True)
    return False


def publish_on_page(page, *, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """One upload->publish cycle on an already-open Weixin Channels tab."""
    t0 = time.time()
    _setup_page(page)

    watch = ApiWatch(page)
    if not _wait_ready(page):
        return PublishResult("weixin", "fail", method="cdp", error="no_upload_input_in_shadow")
    _dismiss_tips(page)
    try:
        page.locator("wujie-app input[type=file][accept*='video']").first.set_input_files(
            video, timeout=60000, no_wait_after=True)
    except Exception as e:
        return PublishResult("weixin", "fail", method="cdp", error=f"upload: {e}")

    if not _wait_clip_ready(page, watch, t0):
        return PublishResult("weixin", "fail", method="cdp", error="upload_not_ready_timeout")

    try:
        _fill_form(page, title=title, description=description, topics=topics)
    except Exception as e:
        return PublishResult("weixin", "fail", method="cdp", error=f"fill_form: {e}")

    _set_video_annotation(page)

    # Button is class-disabled while clip processing runs OR title validation
    # fails; never click a disabled button (the 2026-09 partial root cause).
    pre = _wait_button_enabled(page)
    if not pre.get("found"):
        return PublishResult("weixin", "fail", method="cdp", error="publish_button_gone")
    if pre.get("class_disabled") or pre.get("attr_disabled"):
        print(f"[weixin] button still disabled: {pre.get('cls')} "
              f"toasts={pre.get('toasts')}", flush=True)
        return PublishResult("weixin", "partial", method="cdp",
                             error=f"button_disabled {pre.get('cls')} "
                                   f"{'/'.join(pre.get('toasts') or [])[:80]}")

    start_index = len(watch.events)
    clicked = _click_publish(page)
    click_t = time.time()
    print(f"[weixin] publish click: {clicked} pre_toasts={pre.get('toasts') if pre else None}",
          flush=True)
    if not clicked.startswith(("clicked", "disabled")):
        return PublishResult("weixin", "fail", method="cdp",
                             error=f"no_publish_button: {clicked}")

    deadline_t = time.time() + PUBLISH_TIMEOUT_S
    result = None
    reclicked = False
    seen_toasts = []
    while time.time() < deadline_t:
        for ev in watch.events[start_index:]:
            url = ev["url"]
            if any(all(k in url for k in kw) for kw in PUBLISH_API_PATTERNS):
                result = ("api", ev)
                break
        if result:
            break
        try:
            shadow_text = _sr(page, "(r.textContent || '').slice(0, 500)") or ""
            if "发表失败" in shadow_text:
                result = ("fail", None)
                break
            if "已发表" in shadow_text or "发表成功" in shadow_text:
                result = ("dom", None)
                break
            st = _publish_button_state(page) or {}
            for tmsg in st.get("toasts") or []:
                if tmsg not in seen_toasts:
                    seen_toasts.append(tmsg)
        except Exception:
            st = {}
        if "/post/list" in (page.url or ""):
            result = ("dom", None)
            break
        # One guarded re-click: covers a swallowed first click while an overlay
        # (tip/toast/dropdown) was still settling. Never double-submit once the
        # button is gone or the page navigated.
        if (not reclicked and time.time() > click_t + 7
                and "/post/create" in (page.url or "") and st.get("found")
                and watch.find("post/post_create", after=start_index) is None
                and not st.get("attr_disabled") and not st.get("class_disabled")):
            reclicked = True
            print(f"[weixin] no post_create after 7s; re-click; toasts={seen_toasts}",
                  flush=True)
            _click_publish(page)
        time.sleep(0.5)

    dur = round(time.time() - t0, 1)
    if result is None:
        debug = watch.recent_paths(8)
        print(f"[weixin] no confirm signal; toasts={seen_toasts}; recent POSTs: {debug}")
        hint = " ; ".join(seen_toasts[:3])
        return PublishResult("weixin", "partial", method="cdp", duration_s=dur,
                             error=f"timeout_waiting_for_confirm {hint}".strip(),
                             recent=" | ".join(debug[-4:]))

    kind, ev = result
    if kind == "api":
        try:
            data = ev["resp"].json()
        except Exception:
            data = None
        if _publish_api_ok(data):
            return PublishResult("weixin", "ok", method="cdp", duration_s=dur,
                                 final=f"api_ok {ev['path']}")
        msg = str((data or {}))[:150]
        return PublishResult("weixin", "fail", method="cdp", duration_s=dur,
                             error=f"publish_api_rejected: {msg}")
    if kind == "dom":
        return PublishResult("weixin", "ok", method="cdp", duration_s=dur, final="dom_confirmed")
    return PublishResult("weixin", "fail", method="cdp", duration_s=dur,
                         error="publish_failed_in_shadow")


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """Standalone publish."""
    from playwright.sync_api import sync_playwright
    cdp_url = kwargs.get("cdp_url", CDP_URL_DEFAULT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if "channels.weixin.qq.com" in pg.url:
                page = pg
                break
        if page is None:
            page = ctx.new_page()
        try:
            return publish_on_page(page, title=title, description=description,
                                   video=video, topics=topics, location=location, **kwargs)
        except Exception as e:
            return PublishResult("weixin", "fail", method="cdp", error=str(e)[:200])
