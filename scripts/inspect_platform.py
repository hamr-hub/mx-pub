#!/usr/bin/env python3
"""Live platform publish-page inspector (analysis tool).

Usage:
  python3 scripts/inspect_platform.py <platform> [command]

Commands:
  state   — open publish page, report login state + key DOM signals (default)
  dom     — dump full interactive element inventory (inputs/buttons/editors/tabs)
  upload  — upload a sample video, then dump post-upload form inventory
  net     — attach network logger, reload, print XHR/fetch calls for 30s
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"

PAGES = {
    "douyin": "https://creator.douyin.com/creator-micro/content/publish",
    "kuaishou": "https://cp.kuaishou.com/article/publish/video",
    "weixin": "https://channels.weixin.qq.com/platform/post/create",
}

LOGIN_HINTS = {
    "douyin": ["登录", "扫码登录", "手机号验证"],
    "kuaishou": ["扫码登录", "手机号登录", "账号登录"],
    "weixin": ["扫码登录", "微信扫码", "请使用微信扫码"],
}


def get_page(ctx, platform, new=True):
    url = PAGES[platform]
    for pg in ctx.pages:
        if platform == "douyin" and "creator.douyin.com" in pg.url:
            return pg
        if platform == "kuaishou" and "cp.kuaishou.com" in pg.url:
            return pg
        if platform == "weixin" and "channels.weixin.qq.com" in pg.url:
            return pg
    pg = ctx.new_page()
    pg.goto(url, wait_until="domcontentloaded", timeout=45000)
    return pg


def dump_inventory(page, platform):
    """Dump interactive elements. Weixin form lives in iframe[name=content]."""
    js = r"""
    (root) => {
      const doc = root || document;
      const q = s => Array.from(doc.querySelectorAll(s));
      const vis = e => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const txt = e => (e.innerText || e.value || '').trim().slice(0, 40);
      const out = {inputs: [], textareas: [], editables: [], buttons: [],
                   clickableText: [], selects: [], fileInputs: 0, iframes: []};
      for (const i of q('input')) {
        if (i.type === 'file') { out.fileInputs++; continue; }
        out.inputs.push({type: i.type, ph: i.placeholder||'', name: i.name||'',
                         cls: (i.className||'').slice(0,60), vis: vis(i)});
      }
      for (const t of q('textarea'))
        out.textareas.push({ph: t.placeholder||'', cls: (t.className||'').slice(0,60), vis: vis(t)});
      for (const e of q('[contenteditable=true]'))
        out.editables.push({ph: e.getAttribute('data-placeholder')||e.getAttribute('placeholder')||'',
                            cls: (e.className||'').slice(0,60), vis: vis(e)});
      for (const b of q('button'))
        out.buttons.push({t: txt(b), cls: (b.className||'').slice(0,60),
                          disabled: b.disabled, vis: vis(b)});
      // clickable divs/spans with short text (tabs, feature entries)
      for (const e of q('div,span,a')) {
        const t = (e.innerText||'').trim();
        if (!t || t.length > 12 || e.children.length > 0) continue;
        const r = e.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) continue;
        const cls = (e.className||'').toString().slice(0,50);
        out.clickableText.push({t, tag: e.tagName.toLowerCase(), cls});
      }
      for (const f of q('iframe'))
        out.iframes.push({name: f.name||'', src: (f.src||'').slice(0,100)});
      return out;
    }
    """
    data = page.evaluate(js)
    if platform == "weixin":
        try:
            fr = page.frame_locator("iframe[name=content]")
            frame = next(f for f in page.frames if f.name == "content")
            data["iframe_content"] = frame.evaluate(js)
        except Exception as e:
            data["iframe_error"] = str(e)
    # dedupe clickableText
    seen = set()
    uniq = []
    for c in data.get("clickableText", []):
        k = (c["t"], c["cls"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    data["clickableText"] = uniq
    return data


def cmd_state(page, platform):
    body = page.evaluate("() => document.body.innerText.slice(0, 3000)")
    hints = LOGIN_HINTS[platform]
    logged_out = any(h in body for h in hints) and ("发布" not in body or platform == "weixin")
    info = {
        "url": page.url,
        "title": page.title(),
        "maybe_logged_out": logged_out,
        "file_inputs": page.locator("input[type=file]").count(),
        "body_head": body[:800],
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))


def main():
    platform = sys.argv[1]
    cmd = sys.argv[2] if len(sys.argv) > 2 else "state"
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP, timeout=15000)
        ctx = browser.contexts[0]
        page = get_page(ctx, platform)
        page.bring_to_front()
        time.sleep(6)

        if cmd == "state":
            cmd_state(page, platform)
        elif cmd == "dom":
            inv = dump_inventory(page, platform)
            print(json.dumps(inv, ensure_ascii=False, indent=1))
        elif cmd == "upload":
            video = sys.argv[3]
            page.locator("input[type=file]").first.set_input_files(
                video, timeout=60000, no_wait_after=True)
            print("uploaded, waiting 20s for form...")
            time.sleep(20)
            inv = dump_inventory(page, platform)
            print(json.dumps(inv, ensure_ascii=False, indent=1))
        elif cmd == "net":
            calls = []

            def on_req(req):
                if req.resource_type in ("xhr", "fetch"):
                    calls.append({"method": req.method, "url": req.url[:160]})

            page.on("request", on_req)
            page.reload(wait_until="domcontentloaded")
            time.sleep(30)
            for c in calls:
                print(c["method"], c["url"])


if __name__ == "__main__":
    main()
