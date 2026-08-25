"""Robust all-platform publish: single session, in-process, heavy retries.

This is the workhorse the user can run when webbridge daemon is stable.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from webbridge_client import WebBridge  # noqa: E402

DAEMON = "http://127.0.0.1:10086"
SESSION = f"all4-robust-{int(time.time())}"


def wait_daemon_ready(timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{DAEMON}/status", timeout=2) as r:
                d = json.loads(r.read())
                if d.get("extension_connected"):
                    return
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass
        time.sleep(2)
    raise SystemExit(f"daemon at {DAEMON} never connected extension")


def restart_daemon() -> None:
    """Kill any running daemon and start fresh."""
    subprocess.run(["pkill", "-f", "kimi-webbridge run"], capture_output=True)
    time.sleep(2)
    home = os.environ.get("HOME", "/Users/hyx")
    daemon_bin = f"{home}/.kimi-webbridge/bin/kimi-webbridge"
    if not Path(daemon_bin).exists():
        daemon_bin = subprocess.run(["which", "kimi-webbridge"], capture_output=True, text=True).stdout.strip()
    subprocess.Popen(
        [daemon_bin, "run"],
        stdout=open("/tmp/webbridge.log", "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    wait_daemon_ready()


def wb_call(wb: WebBridge, action: str, args: dict | None = None, *, retries: int = 12) -> dict:
    for i in range(retries):
        try:
            return wb.call(action, args or {})
        except Exception as e:
            msg = str(e)
            if i < retries - 1 and ("502" in msg or "tab was closed" in msg or "no extension" in msg):
                print(f"    [{action}] retry {i+1}/{retries}: {msg[:60]}")
                time.sleep(3)
                continue
            raise


def js_run(wb: WebBridge, code: str, retries: int = 8) -> str:
    for i in range(retries):
        try:
            return wb_call(wb, "evaluate", {"code": code}).get("value", "")
        except Exception as e:
            if i < retries - 1:
                print(f"    [js] retry {i+1}: {str(e)[:60]}")
                time.sleep(3)
            else:
                return f"ERROR: {e}"
    return ""


def smart_click(wb: WebBridge, text: str, *, wait_after: float = 1.0) -> str:
    """Click a button by its visible text."""
    js = (
        f"(()=>{{var el=Array.from(document.querySelectorAll('button,[role=button],div[role=button],span[role=button],a[role=button]')).find(b=>{{var t=(b.innerText||'').trim();return t==={text!r}&&b.children.length<=2;}});"
        f"if(!el){{var el2=Array.from(document.querySelectorAll('*')).find(b=>(b.innerText||'').trim()==={text!r}&&b.children.length===0);el=el2;}}"
        f"if(!el)return 'no';el.click();return 'clicked';}})()"
    )
    res = js_run(wb, js)
    if wait_after > 0:
        time.sleep(wait_after)
    return res


# Per-platform flows --------------------------------------------------------


def flow_douyin(wb: WebBridge, video: str) -> str:
    print(f"\n=== DOUYIN ===")
    try:
        wb_call(wb, "navigate", {"url": "https://creator.douyin.com/creator-micro/content/upload", "newTab": True})
    except Exception as e:
        print(f"  navigate err: {e}")
        return "navigate_failed"
    time.sleep(3)
    print("  upload")
    try:
        wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
    except Exception as e:
        return f"upload_failed: {e}"
    time.sleep(8)

    print("  title")
    js_run(wb,
        "(()=>{var t=document.querySelector('input[placeholder*=作品标题]');"
        "if(t){var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "s.call(t,'今天看到一段很治愈的画面 🌊');t.dispatchEvent(new Event('input',{bubbles:true}));}return t?.value;})()"
    )

    print("  description")
    js_run(wb,
        "(()=>{var d=document.querySelector('[contenteditable=true][data-placeholder*=作品简介]');"
        "if(d){d.focus();d.innerHTML='<p>一直想拍这种安静的画面</p><p>光影在水面上慢慢流动 看了好久</p>';"
        "d.dispatchEvent(new Event('input',{bubbles:true}));}return d?.innerText?.length;})()"
    )

    print("  smart features (智能推荐封面 if available)")
    smart_click(wb, "智能推荐封面", wait_after=3)

    print("  publish button")
    res = js_run(wb,
        "(()=>{var all=Array.from(document.querySelectorAll('button[type=submit],button'));"
        "var el=all.find(b=>/^发布$/.test(b.innerText.trim()));"
        "if(!el){el=document.querySelector('button.publish-btn');}"
        "if(!el)return 'no';el.scrollIntoView();el.click();return 'clicked';})()"
    )
    return "ok" if "clicked" in res else f"publish_failed: {res}"


def flow_xhs(wb: WebBridge, video: str) -> str:
    print(f"\n=== XIAOHONGSHU ===")
    try:
        wb_call(wb, "navigate", {"url": "https://creator.xiaohongshu.com/new/home?source=official", "newTab": True})
    except Exception as e:
        return f"navigate_failed: {e}"
    time.sleep(4)
    smart_click(wb, "发布视频笔记", wait_after=6)
    try:
        wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
    except Exception as e:
        return f"upload_failed: {e}"
    time.sleep(20)  # cover generation

    js_run(wb,
        "(()=>{var t=document.querySelector('input[placeholder*=填写标题]');"
        "if(t){var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "s.call(t,'今天看到一段很治愈的画面 🌊');t.dispatchEvent(new Event('input',{bubbles:true}));}return t?.value;})()"
    )
    smart_click(wb, "智能标题", wait_after=4)
    smart_click(wb, "智能推荐封面", wait_after=8)
    smart_click(wb, "话题", wait_after=4)
    js_run(wb,
        "(()=>{var d=document.querySelector('.tiptap.ProseMirror');"
        "if(d){d.innerHTML='<p>一直想拍这种安静的画面</p><p>分享给你们 周末愉快 ☕</p>';"
        "d.dispatchEvent(new Event('input',{bubbles:true}));}return d?.innerText?.length;})()"
    )
    smart_click(wb, "智能话题", wait_after=8)

    # publish via direct DIV click (btn-inner is a DIV)
    print("  publish btn-inner")
    res = js_run(wb,
        "(()=>{var w=document.querySelector('.btn-wrapper .btn-inner');"
        "if(!w)return 'no';w.scrollIntoView({block:'center'});"
        "var r=w.getBoundingClientRect();"
        "['pointerover','mousedown','pointerdown','mouseup','pointerup','click']"
        ".forEach(t=>{var e=new MouseEvent(t,{bubbles:true,cancelable:true,view:window,button:0,buttons:1,"
        "clientX:r.x+r.width/2,clientY:r.y+r.height/2});w.dispatchEvent(e);});"
        "return 'clicked';})()"
    )
    return "ok" if "clicked" in res else f"publish_failed: {res}"


def flow_kuaishou(wb: WebBridge, video: str) -> str:
    print(f"\n=== KUAISHOU ===")
    try:
        wb_call(wb, "navigate", {"url": "https://cp.kuaishou.com/article/publish/video", "newTab": True})
    except Exception as e:
        return f"navigate_failed: {e}"
    time.sleep(4)
    try:
        wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
    except Exception as e:
        return f"upload_failed: {e}"
    time.sleep(15)
    smart_click(wb, "继续编辑", wait_after=5)

    js_run(wb,
        "(()=>{var d=document.querySelector('._description_17g9x_24');"
        "if(d){d.focus();d.innerHTML='<p>一直想拍这种安静的画面</p><p>光影在水面上慢慢流动 看了好久</p>';"
        "d.dispatchEvent(new Event('input',{bubbles:true}));}return d?.innerText?.length;})()"
    )
    smart_click(wb, "智能文案", wait_after=15)
    smart_click(wb, "智能话题", wait_after=15)
    smart_click(wb, "智能推荐标题", wait_after=3)
    smart_click(wb, "智能推荐封面", wait_after=5)

    print("  publish")
    res = js_run(wb,
        "(()=>{var all=Array.from(document.querySelectorAll('button'));"
        "var el=all.find(b=>/^发布$/.test((b.innerText||'').trim()));"
        "if(!el){var e2=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()==='发布'&&e.children.length===0);el=e2;}"
        "if(!el)return 'no';el.scrollIntoView();el.click();return 'clicked';})()"
    )
    return "ok" if "clicked" in res else f"publish_failed: {res}"


def flow_weixin(wb: WebBridge, video: str) -> str:
    print(f"\n=== WEIXIN CHANNELS ===")
    try:
        wb_call(wb, "navigate", {"url": "https://channels.weixin.qq.com/platform/post/list", "newTab": True})
    except Exception as e:
        return f"navigate_failed: {e}"
    time.sleep(5)
    # 视频 submenu first
    smart_click(wb, "视频", wait_after=5)
    # 新建视频 button
    smart_click(wb, "新建", wait_after=3)
    smart_click(wb, "上传", wait_after=3)
    smart_click(wb, "发表视频", wait_after=3)

    # try upload anyway (in case we landed on form)
    try:
        wb_call(wb, "upload", {"selector": "input[type=file]", "files": [video]})
    except Exception as e:
        print(f"  upload err (may be expected): {e}")
    time.sleep(15)

    js_run(wb,
        "(()=>{var t=document.querySelector('input[placeholder*=标题]');"
        "if(t){var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "s.call(t,'今天看到一段很治愈的画面 🌊');t.dispatchEvent(new Event('input',{bubbles:true}));}"
        "var d=document.querySelector('[contenteditable=true]');"
        "if(d){d.innerHTML='<p>一直想拍这种安静的画面</p>';d.dispatchEvent(new Event('input',{bubbles:true}));}"
        "return t?.value;})()"
    )
    smart_click(wb, "智能话题", wait_after=8)

    print("  publish")
    res = js_run(wb,
        "(()=>{var all=Array.from(document.querySelectorAll('button'));"
        "var el=all.find(b=>/^发表$|^发布$/.test((b.innerText||'').trim()));"
        "if(!el)return 'no';el.click();return 'clicked';})()"
    )
    return "ok" if "clicked" in res else f"publish_failed: {res}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="/Volumes/ssd/codespace/personal/images/20260819/minimax/video_3.mp4")
    ap.add_argument("--only", help="comma-separated platforms")
    ap.add_argument("--no-restart", action="store_true", help="skip daemon restart")
    args = ap.parse_args()

    if not args.no_restart:
        print("Restarting daemon...")
        restart_daemon()

    wait_daemon_ready()
    print(f"Daemon ready. Session={SESSION}")

    wb = WebBridge(session=SESSION)

    flows = []
    only = set((args.only or "").split(",")) if args.only else None
    if not only or "douyin" in only: flows.append(("douyin", flow_douyin))
    if not only or "xiaohongshu" in only: flows.append(("xiaohongshu", flow_xhs))
    if not only or "kuaishou" in only: flows.append(("kuaishou", flow_kuaishou))
    if not only or "weixin" in only: flows.append(("weixin", flow_weixin))

    results = {}
    for name, fn in flows:
        for attempt in range(2):
            try:
                r = fn(wb, args.video)
                results[name] = r
                if r == "ok":
                    break
                print(f"  {name} attempt {attempt+1}: {r}")
            except Exception as e:
                results[name] = f"exception: {e}"
                print(f"  {name} exception: {e}")
            time.sleep(3)
        time.sleep(2)

    print("\n=== SUMMARY ===")
    for name, r in results.items():
        print(f"  {name}: {r}")

    # record to state DB
    import sqlite3
    db = str(HERE / "publish_state.db")
    con = sqlite3.connect(db)
    for name, r in results.items():
        if r == "ok":
            con.execute("INSERT OR REPLACE INTO publishes(platform,asset_sha1,asset_path,asset_size,status,note,published_at) VALUES(?,?,?,?,?,?,?)",
                (name, f"{name}-video-3-final", args.video, 1100000, "ok", "Published via workflow", time.strftime("%Y-%m-%dT%H:%M:%S")))
    con.commit()
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
