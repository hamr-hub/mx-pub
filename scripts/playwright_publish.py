"""Playwright-driven publisher: connects to user's Chrome via CDP (port 9222).

This bypasses all webbridge limits:
- True background tab control (no foreground dance)
- Native CDP click/upload/fill (no anti-bot UI element bypass)
- Stable session (no daemon disconnect)
- Smart features (智能标题 / 智能推荐封面 / 智能话题 / 智能文案) all triggered

Pre-req: Chrome launched with --remote-debugging-port=9222 + user-data-dir.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext, expect


CDP_URL = "http://127.0.0.1:9222"
VIDEO = "/Volumes/ssd/codespace/personal/images/20260819/minimax/video_3.mp4"


def connect():
    print(f"connecting to {CDP_URL}...")
    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp(CDP_URL, timeout=30000)
    print(f"connected! browser has {len(browser.contexts)} context(s)")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    pages = list(ctx.pages)
    print(f"existing pages: {len(pages)}")
    for pg in pages[:10]:
        print(f"  - {pg.url[:80]}")
    return p, browser, ctx, pages


def smart_click(page: Page, text: str, timeout: float = 8.0) -> bool:
    """Click a button by visible text. Returns True on success."""
    try:
        loc = page.locator(f"text={text}").first
        if loc.count() and loc.is_visible():
            loc.click(timeout=timeout * 1000)
            return True
    except Exception:
        pass
    return False


def find_or_open(page: Page, ctx: BrowserContext, url: str, marker_text: str, timeout: float = 15) -> Page:
    """Open url in new tab; wait for marker_text to appear."""
    print(f"  open {url}")
    new_page = ctx.new_page()
    new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if marker_text:
        try:
            new_page.locator(f"text={marker_text}").first.wait_for(timeout=timeout * 1000)
        except Exception:
            pass
    return new_page


# ─────────────────────────────────────────────────────────────────────────────
# Per-platform flows
# ─────────────────────────────────────────────────────────────────────────────


def flow_douyin(ctx: BrowserContext) -> str:
    print("\n=== DOUYIN ===")
    page = find_or_open(ctx, ctx, "https://creator.douyin.com/creator-micro/content/upload", "上传视频")
    print("  upload...")
    page.locator("input[type=file]").set_input_files(VIDEO)
    page.wait_for_timeout(8000)
    page.locator("input[placeholder*='作品标题']").first.fill("今天看到一段很治愈的画面 🌊")
    print("  title filled")
    # description (contenteditable)
    desc = page.locator("[contenteditable=true][data-placeholder*='作品简介']")
    if desc.count():
        desc.first.evaluate(
            "el => { el.innerHTML='<p>一直想拍这种安静的画面</p><p>光影在水面上慢慢流动 看了好久</p>'; "
            "el.dispatchEvent(new Event('input',{bubbles:true})); }"
        )
        print("  desc filled")
    # smart cover
    smart_click(page, "智能推荐封面")
    page.wait_for_timeout(3000)
    # smart title
    smart_click(page, "智能标题")
    page.wait_for_timeout(3000)
    # publish
    before_url = page.url
    res = page.evaluate("""(() => {
        const all = Array.from(document.querySelectorAll('button[type=submit],button'));
        const el = all.find(b => /^发布$/.test(b.innerText.trim()));
        if (!el) return 'no_btn';
        el.scrollIntoView();
        el.click();
        return 'clicked';
    })()""")
    print(f"  publish click: {res}")
    # wait for success indication
    for i in range(15):
        body = page.evaluate("document.body.innerText.slice(0,200)")
        if "发布成功" in body or "已发布" in body or "审核中" in body:
            print(f"  text shows success @ i={i}")
            return "ok"
        if page.url != before_url and "/creator-micro/content" not in page.url:
            print(f"  URL changed @ i={i}: {page.url}")
            return "ok"
        page.wait_for_timeout(2000)
    return "unknown"


def flow_kuaishou(ctx: BrowserContext) -> str:
    print("\n=== KUAISHOU ===")
    page = find_or_open(ctx, ctx, "https://cp.kuaishou.com/article/publish/video", "上传视频")
    page.locator("input[type=file]").set_input_files(VIDEO)
    page.wait_for_timeout(12000)
    # post-upload dialog: 继续编辑
    smart_click(page, "继续编辑")
    page.wait_for_timeout(5000)
    # description
    desc = page.locator("._description_17g9x_24")
    if desc.count():
        desc.first.evaluate(
            "el => { el.innerHTML='<p>一直想拍这种安静的画面</p><p>光影在水面上慢慢流动 看了好久</p>'; "
            "el.dispatchEvent(new Event('input',{bubbles:true})); }"
        )
        print("  desc filled")
    # smart features
    smart_click(page, "智能文案")
    page.wait_for_timeout(15000)
    smart_click(page, "智能话题")
    page.wait_for_timeout(15000)
    smart_click(page, "智能推荐标题")
    page.wait_for_timeout(3000)
    smart_click(page, "智能推荐封面")
    page.wait_for_timeout(5000)
    # publish
    res = page.evaluate("""(() => {
        const all = Array.from(document.querySelectorAll('button'));
        let el = all.find(b => /^发布$/.test((b.innerText || '').trim()));
        if (!el) {
            el = Array.from(document.querySelectorAll('*')).find(e => (e.innerText || '').trim() === '发布' && e.children.length === 0);
        }
        if (!el) return 'no_btn';
        el.scrollIntoView();
        el.click();
        return 'clicked';
    })()""")
    print(f"  publish click: {res}")
    for i in range(15):
        body = page.evaluate("document.body.innerText.slice(0,200)")
        if "发布成功" in body or "已发布" in body:
            print(f"  text shows success @ i={i}")
            return "ok"
        page.wait_for_timeout(2000)
    return "unknown"


def flow_xhs(ctx: BrowserContext) -> str:
    print("\n=== XIAOHONGSHU ===")
    page = find_or_open(ctx, ctx, "https://creator.xiaohongshu.com/new/home?source=official", "发布视频笔记")
    smart_click(page, "发布视频笔记")
    page.wait_for_timeout(8000)
    # Wait for upload input
    page.locator("input[type=file]").wait_for(timeout=15000)
    page.locator("input[type=file]").set_input_files(VIDEO)
    print("  uploaded, waiting 20s for cover generation...")
    page.wait_for_timeout(20000)
    # title
    title = page.locator("input[placeholder*='填写标题']")
    if title.count():
        title.first.fill("今天看到一段很治愈的画面 🌊")
        print("  title filled")
    # smart title
    smart_click(page, "智能标题")
    page.wait_for_timeout(5000)
    # smart cover
    smart_click(page, "智能推荐封面")
    page.wait_for_timeout(8000)
    # smart topics
    smart_click(page, "话题")
    page.wait_for_timeout(5000)
    # description
    desc = page.locator(".tiptap.ProseMirror")
    if desc.count():
        desc.first.evaluate(
            "el => { el.innerHTML='<p>一直想拍这种安静的画面</p><p>分享给你们 周末愉快 ☕</p>'; "
            "el.dispatchEvent(new Event('input',{bubbles:true})); }"
        )
        print("  desc filled")
    # publish
    before_url = page.url
    for selector in [".btn-wrapper .btn-inner", ".btn-wrapper"]:
        try:
            loc = page.locator(selector).first
            if loc.count():
                loc.scroll_into_view_if_needed()
                loc.click()
                print(f"  clicked {selector}")
                break
        except Exception as e:
            print(f"  click {selector}: {e}")
    # wait + poll
    for i in range(20):
        if page.url != before_url:
            print(f"  URL changed @ i={i}: {page.url}")
            break
        body = page.evaluate("document.body.innerText")
        if "已发布" in body or "发布成功" in body:
            print(f"  text shows success @ i={i}")
            return "ok"
        page.wait_for_timeout(2000)
    # verify
    notes = ctx.new_page()
    notes.goto("https://creator.xiaohongshu.com/new/note-manager", wait_until="domcontentloaded")
    notes.wait_for_timeout(5000)
    body = notes.evaluate("document.body.innerText")
    published = ("今天看到一段很治愈" in body) or ("一直想拍" in body)
    print(f"  notes contains our video: {published}")
    notes.close()
    return "ok" if published else "drafted"


def flow_weixin(ctx: BrowserContext) -> str:
    print("\n=== WEIXIN CHANNELS ===")
    # try direct post/create URL
    page = find_or_open(ctx, ctx, "https://channels.weixin.qq.com/platform/post/create", None, timeout=20)
    page.wait_for_timeout(8000)
    state = page.evaluate("location.href+'|fileInputs='+document.querySelectorAll('input[type=file]').length+'|'+document.body.innerText.slice(0,300)")
    print(f"  state: {state[:300]}")
    if "fileInputs=0" in state or "input[type=file]" not in state:
        # try via dashboard
        page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        # click 内容管理 → 视频
        smart_click(page, "内容管理")
        page.wait_for_timeout(2000)
        smart_click(page, "视频")
        page.wait_for_timeout(5000)
        state = page.evaluate("location.href+'|fileInputs='+document.querySelectorAll('input[type=file]').length+'|'+document.body.innerText.slice(0,200)")
        print(f"  state after menu: {state[:300]}")
    # upload
    try:
        page.locator("input[type=file]").set_input_files(VIDEO)
        page.wait_for_timeout(15000)
        print("  uploaded")
    except Exception as e:
        print(f"  upload err: {e}")
        return "no_upload_input"
    # fill title + desc
    title = page.locator("input[placeholder*='标题']")
    if title.count():
        title.first.fill("今天看到一段很治愈的画面 🌊")
    desc = page.locator("[contenteditable=true]")
    if desc.count():
        desc.first.evaluate("el => { el.innerHTML='<p>一直想拍这种安静的画面</p>'; el.dispatchEvent(new Event('input',{bubbles:true})); }")
    # smart topics
    smart_click(page, "智能话题")
    page.wait_for_timeout(8000)
    # publish
    res = page.evaluate("""(() => {
        const all = Array.from(document.querySelectorAll('button'));
        const el = all.find(b => /^(发表|发布)$/.test((b.innerText || '').trim()));
        if (!el) return 'no_btn';
        el.click();
        return 'clicked';
    })()""")
    print(f"  publish click: {res}")
    for i in range(15):
        body = page.evaluate("document.body.innerText")
        if "已发布" in body or "发布成功" in body:
            print(f"  text shows success @ i={i}")
            return "ok"
        page.wait_for_timeout(2000)
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated: douyin,kuaishou,xiaohongshu,weixin")
    ap.add_argument("--video", default=VIDEO)
    args = ap.parse_args()

    p, browser, ctx, pages = connect()
    flows = []
    only = set((args.only or "").split(",")) if args.only else None
    if not only or "douyin" in only: flows.append(("douyin", flow_douyin))
    if not only or "kuaishou" in only: flows.append(("kuaishou", flow_kuaishou))
    if not only or "xiaohongshu" in only: flows.append(("xiaohongshu", flow_xhs))
    if not only or "weixin" in only: flows.append(("weixin", flow_weixin))

    results = {}
    for name, fn in flows:
        for attempt in range(2):
            try:
                results[name] = fn(ctx)
                if results[name] == "ok":
                    break
                print(f"  {name} attempt {attempt+1}: {results[name]}")
            except Exception as e:
                results[name] = f"exception: {e}"
                print(f"  {name} exception: {e}")
            time.sleep(2)

    print("\n=== SUMMARY ===")
    for n, r in results.items():
        print(f"  {n}: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
