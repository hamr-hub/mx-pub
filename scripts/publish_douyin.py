"""Douyin publish v3 - scroll to publish button."""
import json, time, re, sys
from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"
VIDEO = "/Users/hyx/workspace/heaven-grace/docs/video-pitch/heaven-grace-pitch-v1.mp4"
TITLE = "Heaven Grace 神恩浩荡"
DESC = "Heaven Grace 神恩浩荡 - 文明崛起对抗深渊。六部曲史诗奇幻大作 #神恩浩荡 #HeavenGrace"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
    ctx = browser.contexts[0]
    page = next((pg for pg in ctx.pages if "douyin" in pg.url), None)
    if not page:
        page = ctx.new_page()
    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 800})
    page.on("dialog", lambda d: d.accept())

    page.goto("https://creator.douyin.com/creator-micro/content/upload",
              wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)

    fi = page.locator("input[type=file]").first
    fi.set_input_files(VIDEO, timeout=60000)
    print("[upload] file set", flush=True)

    # Wait for upload
    for i in range(60):
        time.sleep(2)
        body = page.evaluate("() => document.body.innerText")
        if '重新上传' in body:
            print(f"[upload] DONE at t={i*2}s", flush=True)
            break

    # Title (作品描述) - 30 char limit, use short
    page.locator('textarea, [contenteditable="true"]').first.fill(TITLE, timeout=5000)
    print(f"[fill] 作品描述={TITLE}", flush=True)
    time.sleep(1)

    # Description (简介) - longer text
    desc_set = page.evaluate("""(text) => {
        const tas = document.querySelectorAll('textarea, [contenteditable="true"]');
        let count = 0;
        for (const t of tas) {
            count++;
            if (count === 2) {
                t.focus();
                t.value = text;
                t.dispatchEvent(new Event('input', {bubbles: true}));
                return 'filled-textarea-2';
            }
        }
        return 'only-1-found';
    }""", DESC)
    print(f"[fill] 简介={desc_set}", flush=True)
    time.sleep(1)

    # Wait for cover to generate
    for i in range(30):
        body = page.evaluate("() => document.body.innerText")
        if '生成中' not in body and '推荐封面' in body:
            print(f"[cover] DONE at t={i*2}s", flush=True)
            break
        time.sleep(2)
    time.sleep(2)
    page.screenshot(path="/tmp/_dy_pre_publish.png")

    # Scroll to bottom to find publish button
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1)
    page.screenshot(path="/tmp/_dy_scrolled.png")

    # Find publish button - try multiple selectors
    pub_info = page.evaluate("""() => {
        const all = document.querySelectorAll('button, [role=button], .semi-button');
        for (const b of all) {
            const t = (b.innerText || '').trim();
            if (t === '发布' || t === '发表') {
                const r = b.getBoundingClientRect();
                if (r.width > 20 && r.height > 20) {
                    return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height, text: t, inViewport: r.top >= 0 && r.bottom <= 800};
                }
            }
        }
        return null;
    }""")
    print(f"[pub_btn] {pub_info}", flush=True)

    if pub_info and not pub_info['inViewport']:
        # Scroll into view
        page.evaluate("""(y) => {
            window.scrollTo(0, y - 200);
        }""", pub_info['y'] + page.evaluate("() => window.pageYOffset"))
        time.sleep(1)
        # Re-get coords
        pub_info = page.evaluate("""() => {
            const all = document.querySelectorAll('button, [role=button]');
            for (const b of all) {
                const t = (b.innerText || '').trim();
                if (t === '发布' || t === '发表') {
                    const r = b.getBoundingClientRect();
                    if (r.width > 20 && r.height > 20) {
                        return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height, text: t};
                    }
                }
            }
            return null;
        }""")
        print(f"[pub_btn after scroll] {pub_info}", flush=True)

    if not pub_info:
        print("NO PUBLISH BUTTON AFTER SCROLL", flush=True)
        sys.exit(1)

    # Network
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    responses = []
    def on_resp(ev):
        try:
            url = ev['request']['url']
            if any(k in url for k in ['/aweme_post/', '/publish/', '/video/create/', '/upload_video', 'creator/']):
                responses.append({'url': url[:120], 'status': ev['response']['status'], 'method': ev['request']['method']})
        except:
            pass
    cdp.on("Network.responseReceived", on_resp)

    page.mouse.click(pub_info['x'], pub_info['y'])
    print(f"[click] at ({pub_info['x']:.0f}, {pub_info['y']:.0f})", flush=True)

    for i in range(20):
        time.sleep(2)
        try:
            body = page.evaluate("() => document.body.innerText")
        except:
            body = ""
        if '发布成功' in body or '已发布' in body or '提交成功' in body or '审核中' in body:
            print(f"[result] SUCCESS at t={i*2}s", flush=True)
            page.screenshot(path="/tmp/_dy_success.png")
            break
        if '发布失败' in body or '提交失败' in body or '系统繁忙' in body:
            print(f"[result] FAIL at t={i*2}s", flush=True)
            page.screenshot(path="/tmp/_dy_fail.png")
            print(f"body: {body[:400]}")
            break
    else:
        print(f"[result] TIMEOUT", flush=True)
        page.screenshot(path="/tmp/_dy_timeout.png")

    print(f"\nNetwork: {len(responses)} responses")
    for r in responses[-15:]:
        print(f"  {r}")
