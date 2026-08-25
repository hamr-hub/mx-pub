"""
Weixin 视频号 publisher - WORKING version (2026-08-25 21:33).

Solution: Wujie micro-frontend with cross-origin iframe at
`https://channels.weixin.qq.com/micro/content/post/create`. The iframe is the
mount target for the Vue 2 PostCreate component (`canPost` computed at depth 7).

Workflow:
1. Use EXISTING tab at `channels.weixin.qq.com/platform/post/create` (must
   already be open in Chrome — the iframe only loads if the user lands on
   /platform/post/create with an active session; closing+reopening via CDP
   breaks the Wujie mount and the iframe stays at `empty.html`)
2. `page.locator('input[type=file]').first.set_input_files(video)` — Playwright
   auto-pierces iframes
3. Poll Vue `canPost` computed — becomes True after `coverUrl` is set (~4s
   after upload completes)
4. Fill 短标题 via `input[placeholder*="短标题"]`
5. Fill 描述 via `[contenteditable="true"]` (clear 商品 textarea first)
6. Call `handlePost` via Vue method directly (more reliable than DOM click)
7. Page redirects to `/platform/post/list` on success

Verified: 2026-08-25 21:33 → video appears at top of list with timestamp
2026年08月25日 21:33 (zero views because just published).

Why this works vs the failed approaches:
- Direct API: errCode 300002 (account/session-level reject, not fixable)
- DOM click on 发表 button: button has weui-desktop-btn_disabled when canPost=False
- Setting Vue state manually: fileList is React/Vue-controlled, mutations get
  overwritten; only real upload (via input[type=file]) flips canPost
"""
import json
import time
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from publisher import PublishResult

CDP_URL = "http://127.0.0.1:9222"
PUBLISH_URL = "https://channels.weixin.qq.com/platform/post/create"
EDITOR_URL_FRAGMENT = "micro/content/post/create"


def _inspect_state(fr):
    """Return all Vue components with canPost computed, plus file/upload state."""
    return fr.evaluate("""(() => {
        const root = document.querySelector('#app');
        if (!root || !root.__vue__) return [{error: 'no_root'}];
        const seen = new Set();
        const results = [];
        const walk = (vm, d=0) => {
            if (!vm || seen.has(vm) || d > 15) return;
            seen.add(vm);
            const c = vm.$options.computed || {};
            if (c.canPost) {
                const data = vm.$data || {};
                const fileList = data.fileList || [];
                results.push({
                    d, tag: vm.$options?._componentTag || vm.$options?.name || '?',
                    canPost: c.canPost.get.call(vm),
                    disablePostTip: c.disablePostTip?.get.call(vm),
                    fileListLen: fileList.length,
                    coverUrl: !!data.coverUrl,
                });
            }
            if (vm.$children) for (const ch of vm.$children) walk(ch, d+1);
        };
        walk(root.__vue__);
        return results;
    })()""")


def _wait_canpost(editor, timeout_s=120):
    """Poll editor until canPost=True. Returns the state snapshot on success, else None."""
    for i in range(timeout_s // 2):
        try:
            s = _inspect_state(editor)
            comp = s[0] if s and s[0].get("error") is None else None
            if not comp:
                time.sleep(2)
                continue
            if comp.get("canPost"):
                return comp
            if i % 5 == 0:
                print(f"[wait t={i*2}s] canPost={comp.get('canPost')} "
                      f"tip={(comp.get('disablePostTip') or '')[:40]!r} "
                      f"flen={comp.get('fileListLen')} coverUrl={comp.get('coverUrl')}",
                      flush=True)
        except Exception:
            pass
        time.sleep(2)
    return None


def _fill_form(page, editor, *, short_title, description):
    """Fill 短标题 (top-level) + 描述 (inside iframe)."""
    # 短标题: in main page, NOT inside iframe (different input element)
    page.locator('input[placeholder*="短标题"]').first.fill(short_title, timeout=5000)

    # 描述: contenteditable inside iframe
    editor.evaluate("""(text) => {
        const tas = document.querySelectorAll('textarea');
        for (const t of tas) {
            if (t.placeholder && t.placeholder.includes('商品')) {
                t.value = '';
                t.dispatchEvent(new Event('input', {bubbles: true}));
            }
        }
        const ces = document.querySelectorAll('[contenteditable="true"]');
        for (const c of ces) {
            c.innerText = text;
            c.dispatchEvent(new Event('input', {bubbles: true}));
        }
    }""", description)
    time.sleep(1)


def _click_handle_post(editor):
    """Call PostCreate.handlePost directly via Vue method binding."""
    return editor.evaluate("""(() => {
        const root = document.querySelector('#app');
        const seen = new Set();
        let target = null;
        const walk = (vm, d=0) => {
            if (!vm || seen.has(vm) || d > 15) return;
            seen.add(vm);
            const c = vm.$options.computed || {};
            if (c.canPost && typeof vm.$options.methods?.handlePost === 'function') {
                target = vm;
                return;
            }
            if (vm.$children) for (const ch of vm.$children) walk(ch, d+1);
        };
        walk(root.__vue__);
        if (!target) return {called: false, reason: 'no_component_with_handlePost'};
        try {
            target.$options.methods.handlePost.call(target);
            return {called: true};
        } catch (e) {
            return {called: false, reason: e.message};
        }
    })()""")


def publish(*, title, description, video, topics=None, **kwargs) -> PublishResult:
    """Publish to Weixin 视频号 via existing CDP tab + Wujie iframe."""
    short_title = title[:16]  # 短标题 max 16 chars
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "channels.weixin.qq.com" in pg.url), None)
        if not page:
            return PublishResult("weixin", "fail", error="no_browser_tab")
        page.bring_to_front()
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.on("dialog", lambda d: d.accept())

        # Must NOT reload — reloading breaks Wujie iframe mount.
        # The tab must already be at /platform/post/create with the iframe loaded.
        editor = next((fr for fr in page.frames if EDITOR_URL_FRAGMENT in fr.url), None)
        if not editor:
            return PublishResult("weixin", "fail", method="cdp",
                                 error="editor_iframe_not_loaded — user must open /platform/post/create in Chrome first")

        # 1. Upload file
        inputs = page.locator('input[type=file]').count()
        if inputs == 0:
            return PublishResult("weixin", "fail", method="cdp", error="no_file_input")
        page.locator('input[type=file]').first.set_input_files(video, timeout=30000)

        # 2. Wait for coverUrl → canPost=True
        comp = _wait_canpost(editor, timeout_s=120)
        if not comp:
            return PublishResult("weixin", "fail", method="cdp",
                                 error="canPost_stuck_false — coverUrl never set, upload may have failed")

        # 3. Fill form
        _fill_form(page, editor, short_title=short_title, description=description)

        # 4. Click via Vue handlePost
        result = _click_handle_post(editor)
        if not result.get("called"):
            return PublishResult("weixin", "fail", method="cdp",
                                 error=f"handlePost_failed: {result.get('reason')}")

        # 5. Wait for redirect to list page
        for i in range(15):
            time.sleep(2)
            try:
                if "/platform/post/list" in page.url:
                    return PublishResult("weixin", "ok", method="cdp-wujie-handlepost",
                                         url=page.url, redirect_at_s=i*2)
                body = page.evaluate("() => document.body.innerText")
            except Exception:
                body = ""
            if "300002" in body or "发布失败" in body or "提交失败" in body:
                return PublishResult("weixin", "fail", method="cdp-wujie-handlepost",
                                     error="server_300002_or_page_reported_fail", body=body[:300])

        return PublishResult("weixin", "fail", method="cdp-wujie-handlepost",
                             error="timeout_no_redirect", final_url=page.url)