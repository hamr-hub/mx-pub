"""All-platforms test: publish one video per platform in a single session.

This script keeps one webbridge session alive and runs each platform's full
publish flow sequentially with internal retries. It uses direct webbridge calls
(no publisher script wait loops) so it's faster and less affected by daemon
instability.

Usage:
    python scripts/test_all_platforms.py [--video PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from webbridge_client import WebBridge  # noqa: E402

DAEMON = "http://127.0.0.1:10086"
SESSION = f"all4-{int(time.time())}"
WAIT_FOR_DAEMON_MAX = 30


def wait_daemon_ready(daemon: str = DAEMON, timeout: float = 30) -> None:
    """Block until webbridge daemon reports extension_connected=True."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{daemon}/status", timeout=2) as r:
                d = json.loads(r.read())
                if d.get("extension_connected"):
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(2)
    raise SystemExit(f"daemon at {daemon} never connected extension within {timeout}s")


def wb_call(wb: WebBridge, action: str, args: dict | None = None, *, retries: int = 8) -> dict:
    """webbridge call with retries on session-lost / 502 errors."""
    for i in range(retries):
        try:
            return wb.call(action, args or {})
        except Exception as e:
            msg = str(e)
            if i < retries - 1 and (
                "502" in msg or "tab was closed" in msg or "no extension" in msg
            ):
                print(f"    [{action}] retry {i+1}/{retries}: {msg[:60]}")
                time.sleep(2)
                continue
            raise


def find_or_navigate(wb: WebBridge, url: str, group_title: str) -> dict:
    """find an existing session tab matching url prefix; if none, navigate new."""
    try:
        tabs = wb_call(wb, "list_tabs", {})
        for t in tabs:
            if t.get("url", "").startswith(url.split("?")[0]):
                return wb_call(wb, "find_tab", {"url": t["url"], "active": False})
    except Exception as e:
        print(f"    list_tabs failed: {e}")
    return wb_call(wb, "navigate", {"url": url, "newTab": True, "group_title": group_title})


def js_run(wb: WebBridge, code: str) -> str:
    """evaluate code; return string value or error msg."""
    for i in range(5):
        try:
            res = wb_call(wb, "evaluate", {"code": code})
            return res.get("value", "")
        except Exception as e:
            if i < 4:
                print(f"    evaluate retry {i+1}: {str(e)[:60]}")
                time.sleep(2)
            else:
                return f"ERROR: {e}"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Per-platform flows
# ─────────────────────────────────────────────────────────────────────────────


def publish_douyin(wb: WebBridge, video: str) -> str:
    print(f"\n=== DOUYIN: {video} ===")
    find_or_navigate(wb, "https://creator.douyin.com/creator-micro/content/upload", "publish-douyin")
    time.sleep(2)

    # upload
    print("  uploading...")
    wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
    time.sleep(8)

    # title
    print("  filling title...")
    js_run(wb,
        "(()=>{var t=document.querySelector('input[placeholder*=\\\"作品标题\\\"]');"
        "if(t){var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "s.call(t,'今天看到一段很治愈的画面 🌊');t.dispatchEvent(new Event('input',{bubbles:true}));}return t?.value;})()"
    )

    # description (contenteditable)
    print("  filling description...")
    js_run(wb,
        "(()=>{var d=document.querySelector('[contenteditable=true][data-placeholder*=\\\"作品简介\\\"]');"
        "if(d){d.focus();d.innerHTML='<p>一直想拍这种安静的画面</p><p>光影在水面上慢慢流动 看了好久</p>';"
        "d.dispatchEvent(new Event('input',{bubbles:true}));}return d?.innerText?.length;})()"
    )

    # smart cover
    print("  smart cover...")
    js_run(wb, "(()=>{var b=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').includes('智能封面')&&e.children.length===0);if(b)b.click();return !!b;})()")
    time.sleep(3)

    # click publish
    print("  clicking 发布...")
    pub_js = (
        "(()=>{var all=Array.from(document.querySelectorAll('button[type=submit],button'));"
        "var el=all.find(b=>/^发布$|^发表$/.test(b.innerText.trim()));"
        "if(!el){el=document.querySelector('button.publish-btn,button[type=submit]');}"
        "if(!el)return 'no_btn';el.scrollIntoView();el.click();return 'clicked='+el.innerText.trim();})()"
    )
    res = js_run(wb, pub_js)
    print(f"  publish-click: {res}")
    return "ok" if "clicked" in res else "failed"


def publish_xiaohongshu(wb: WebBridge, video: str) -> str:
    print(f"\n=== XIAOHONGSHU: {video} ===")
    find_or_navigate(wb, "https://creator.xiaohongshu.com/new/home?source=official", "publish-xhs")
    time.sleep(3)
    # click 发布视频笔记
    js_run(wb,
        "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()==='发布视频笔记'&&e.children.length===0);"
        "if(el)el.click();return !!el;})()"
    )
    time.sleep(6)
    # upload
    print("  uploading...")
    wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
    time.sleep(20)  # cover generation

    # title
    js_run(wb,
        "(()=>{var t=document.querySelector('input[placeholder*=\\\"填写标题\\\"]');"
        "if(t){var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "s.call(t,'今天看到一段很治愈的画面 🌊');t.dispatchEvent(new Event('input',{bubbles:true}));}return t?.value;})()"
    )

    # smart title
    js_run(wb, "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()==='智能标题'&&e.children.length===0);if(el)el.click();return !!el;})()")
    time.sleep(5)

    # smart cover
    js_run(wb, "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()==='智能推荐封面'&&e.children.length===0);if(el)el.click();return !!el;})()")
    time.sleep(10)

    # smart topics
    js_run(wb, "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()=='话题'&&e.children.length<=1);if(el)el.click();return !!el;})()")
    time.sleep(15)

    # description
    js_run(wb,
        "(()=>{var d=document.querySelector('.tiptap.ProseMirror');"
        "if(d){d.innerHTML='<p>一直想拍这种安静的画面</p><p>光影在水面上慢慢流动 看了好久</p><p>分享给你们 周末愉快 ☕</p>';"
        "d.dispatchEvent(new Event('input',{bubbles:true}));}return d?.innerText?.length;})()"
    )

    # publish button (DIV not BUTTON)
    print("  clicking 发布笔记...")
    res = js_run(wb,
        "(()=>{var el=document.querySelector('.btn-wrapper .btn-inner');"
        "if(!el)return 'no_btn';el.scrollIntoView();el.click();return 'clicked';})()"
    )
    print(f"  publish-click: {res}")
    return "ok" if "clicked" in res else "failed"


def publish_kuaishou(wb: WebBridge, video: str) -> str:
    print(f"\n=== KUAISHOU: {video} ===")
    find_or_navigate(wb, "https://cp.kuaishou.com/article/publish/video", "publish-ks")
    time.sleep(4)
    # upload
    print("  uploading...")
    wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
    time.sleep(15)
    # continue editing (in case prompt appears)
    js_run(wb, "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()=='继续编辑'&&e.children.length===0);if(el)el.click();return !!el;})()")
    time.sleep(5)
    # description (contenteditable DIV ._description)
    js_run(wb,
        "(()=>{var d=document.querySelector('._description_17g9x_24');"
        "if(d){d.focus();d.innerHTML='<p>一直想拍这种安静的画面</p><p>光影在水面上慢慢流动 看了好久</p>';"
        "d.dispatchEvent(new Event('input',{bubbles:true}));}return d?.innerText?.length;})()"
    )
    # smart copy
    js_run(wb, "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()=='智能文案'&&e.children.length<=2);if(el)el.click();return !!el;})()")
    time.sleep(15)
    # smart tag
    js_run(wb, "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()=='智能话题'&&e.children.length<=2);if(el)el.click();return !!el;})()")
    time.sleep(15)
    # publish button
    print("  clicking 发布...")
    res = js_run(wb,
        "(()=>{var all=Array.from(document.querySelectorAll('button'));"
        "var el=all.find(b=>/^发布$/.test((b.innerText||'').trim()));"
        "if(!el){el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()==='发布'&&e.children.length===0);}"
        "if(!el)return 'no_btn';el.scrollIntoView();el.click();return 'clicked';})()"
    )
    print(f"  publish-click: {res}")
    return "ok" if "clicked" in res else "failed"


def publish_weixin(wb: WebBridge, video: str) -> str:
    print(f"\n=== WEIXIN CHANNELS: {video} ===")
    # navigate to publish entry (SPA path)
    find_or_navigate(wb, "https://channels.weixin.qq.com/platform/post/create", "publish-weixin")
    time.sleep(5)
    state = js_run(wb, "location.href+'|fileInputs='+document.querySelectorAll('input[type=file]').length+'|'+document.body.innerText.slice(0,300)")
    print(f"  state: {state[:200]}")
    if "input[type=file]" in state or "fileInputs=0" in state:
        print("  uploading...")
        wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
        time.sleep(15)
    # title + desc
    js_run(wb,
        "(()=>{var t=document.querySelector('input[placeholder*=\\\"标题\\\"]');"
        "if(t){var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "s.call(t,'今天看到一段很治愈的画面 🌊');t.dispatchEvent(new Event('input',{bubbles:true}));}"
        "var d=document.querySelector('[contenteditable=true]');"
        "if(d){d.innerHTML='<p>一直想拍这种安静的画面</p>';d.dispatchEvent(new Event('input',{bubbles:true}));}"
        "return t?.value;})()"
    )
    # smart topic
    js_run(wb, "(()=>{var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').includes('智能话题')&&e.children.length<=2);if(el)el.click();return !!el;})()")
    time.sleep(8)
    # publish
    print("  clicking 发表...")
    res = js_run(wb,
        "(()=>{var all=Array.from(document.querySelectorAll('button'));"
        "var el=all.find(b=>/^发表$|^发布$/.test((b.innerText||'').trim()));"
        "if(!el)return 'no_btn';el.click();return 'clicked';})()"
    )
    print(f"  publish-click: {res}")
    return "ok" if "clicked" in res else "failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="/Volumes/ssd/codespace/personal/images/20260819/minimax/video_3.mp4")
    ap.add_argument("--only", help="comma-separated: douyin,xiaohongshu,kuaishou,weixin")
    args = ap.parse_args()

    if not Path(args.video).exists():
        print(f"video not found: {args.video}")
        return 2

    print("Waiting for webbridge daemon...")
    wait_daemon_ready()
    print(f"Daemon ready. Session = {SESSION}")

    wb = WebBridge(session=SESSION)

    flows = []
    only = set((args.only or "").split(",")) if args.only else None
    if not only or "douyin" in only:
        flows.append(("douyin", publish_douyin))
    if not only or "xiaohongshu" in only:
        flows.append(("xiaohongshu", publish_xiaohongshu))
    if not only or "kuaishou" in only:
        flows.append(("kuaishou", publish_kuaishou))
    if not only or "weixin" in only:
        flows.append(("weixin", publish_weixin))

    results = {}
    for name, fn in flows:
        for attempt in range(2):  # retry once per platform
            try:
                r = fn(wb, args.video)
                results[name] = r
                if r == "ok":
                    break
                print(f"  {name} failed (attempt {attempt+1}), retrying...")
                time.sleep(3)
            except Exception as e:
                results[name] = f"exception: {e}"
                print(f"  {name} exception (attempt {attempt+1}): {e}")
                time.sleep(3)
        time.sleep(2)

    print("\n=== SUMMARY ===")
    for name, r in results.items():
        print(f"  {name}: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
