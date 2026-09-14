#!/usr/bin/env python3
"""Driver: per-date collection, then publish each video with collection/topic attach.

Phases:
1. For each date in [20260902, 20260911]:
   a. Create Douyin collection (with cover) if not exists
2. For each video in queue (recent):
   a. Publish to Douyin + attach to that date's collection
   b. Publish to Kuaishou + add 话题 (topic)
3. Persist publish results to publish_state.json + queue.json
"""
import json
import re
import sys
import time
from pathlib import Path
from collections import defaultdict
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from platforms.douyin import publish_on_page as publish_douyin
from platforms.kuaishou import publish_on_page as publish_kuaishou
from mx_collections import (
    get_or_create_douyin_collection,
    attach_douyin_to_collection,
    get_or_create_kuaishou_collection,
    add_videos_to_kuaishou_collection,
)
from publisher import PublishResult

QUEUE = HERE / "publish_queue.json"
STATE = HERE / "publish_state.json"
COVERS = Path("/tmp/covers")


def norm(d):
    return d if re.fullmatch(r"\d{8}", d) else d.replace("-", "")


def load_queue():
    return json.loads(QUEUE.read_text())


def save_queue(q):
    QUEUE.write_text(json.dumps(q, indent=2, ensure_ascii=False))


def record_result(queue, video_path, platform, result: PublishResult):
    """Update queue's video.published and append to publish_state.json history."""
    info = {
        "ts": time.time(),
        "method": result.method,
        "status": result.status,
        "error": (result.error or "")[:200],
        "duration_s": result.duration_s,
        **result.details,
    }
    for v in queue["videos"]:
        if v["path"] == video_path:
            v.setdefault("published", {})[platform] = info
            break

    state = json.loads(STATE.read_text())
    state.setdefault("platforms", {}).setdefault(platform, {
        "attempts": 0, "successes": 0, "methods": {}, "last_status": "", "last_updated": 0,
    })
    p = state["platforms"][platform]
    p["attempts"] = p.get("attempts", 0) + 1
    if result.status == "ok":
        p["successes"] = p.get("successes", 0) + 1
    p["methods"] = p.get("methods", {})
    p["methods"][result.method] = p["methods"].get(result.method, 0) + 1
    p["last_status"] = result.status
    p["last_updated"] = time.time()
    state.setdefault("history", []).append({
        "ts": time.time(), "platform": platform, "status": result.status,
        "method": result.method, "duration_s": result.duration_s,
        "error": (result.error or "")[:200], "video": video_path,
        **result.details,
    })
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def find_page(ctx, url_substr, *, new_if_missing=True):
    """Find a page containing url_substr, else return a fresh page on the expected URL."""
    for pg in ctx.pages:
        if url_substr in pg.url:
            try:
                # Check if page is alive
                _ = pg.url
                return pg
            except Exception:
                continue
    if new_if_missing:
        try:
            return ctx.new_page()
        except Exception:
            return None
    return None


def main():
    queue = load_queue()
    # Filter to recent dates
    by_date = defaultdict(list)
    for v in queue["videos"]:
        if "20260902" <= norm(v["date"]) <= "20260911":
            by_date[v["date"]].append(v)

    print(f"Dates in scope: {sorted(by_date)}")
    for d, vs in sorted(by_date.items()):
        print(f"  {d}: {len(vs)} videos")

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
        ctx = b.contexts[0]

        # Phase 1: create Douyin collections per date (resilient, fresh page each)
        for date in sorted(by_date):
            cover_path = COVERS / f"{date}.jpg"
            if not cover_path.exists():
                print(f"  ⚠️ no cover for {date}, skip collection create")
                continue
            cname = f"AI合集_{date}"
            print(f"\n[1.create] douyin collection: {cname}")
            try:
                # Use a fresh page for each creation to avoid accumulated state
                fresh_page = ctx.new_page()
                ref = get_or_create_douyin_collection(
                    fresh_page, name=cname,
                    cover_path=str(cover_path),
                    intro=f"{date} 自动创建合集",
                )
                print(f"  -> ref={ref}")
                try:
                    fresh_page.close()
                except Exception:
                    pass
            except Exception as e:
                print(f"  ⚠️ create failed for {cname}: {e}")
                continue

        # Phase 1b: create Kuaishou collections (empty)
        for date in sorted(by_date):
            cname = f"AI合集_KS_{date}"
            print(f"\n[1.create] kuaishou collection: {cname}")
            try:
                fresh_page = ctx.new_page()
                ref = get_or_create_kuaishou_collection(fresh_page, name=cname)
                print(f"  -> ref={ref}")
                try:
                    fresh_page.close()
                except Exception:
                    pass
            except Exception as e:
                print(f"  ⚠️ ks create failed for {cname}: {e}")
                continue

        # Phase 2: publish each video (douyin with collection attach, kuaishou plain)
        for date in sorted(by_date):
            cname = f"AI合集_{date}"

            def make_dy_hook(cname):
                def hook(page):
                    page.evaluate("""() => {
                        document.querySelectorAll('.semi-modal-wrap, .semi-portal [role=\"dialog\"]').forEach(e => e.remove());
                    }""")
                    return attach_douyin_to_collection(page, collection_name=cname)
                return hook

            for v in by_date[date]:
                pub = v.get("published", {})
                # Douyin (with collection attach)
                if pub.get("douyin", {}).get("status") != "ok":
                    print(f"\n[2.dy] {v['name']} ({date}) → {cname}", flush=True)
                    try:
                        dy_pub_page = find_page(ctx, "creator.douyin.com/content/publish")
                        if dy_pub_page is None:
                            dy_pub_page = ctx.new_page()
                        res = publish_douyin(
                            dy_pub_page,
                            title=v["title"], description=v["description"],
                            video=v["path"], fast_mode=True,
                            pre_publish=make_dy_hook(cname),
                        )
                        record_result(queue, v["path"], "douyin", res)
                        save_queue(queue)
                        print(f"  -> {res.status} err={(res.error or '')[:80]}", flush=True)
                    except Exception as e:
                        print(f"  dy exception: {e}", flush=True)
                # Kuaishou (no collection attach at publish — added later via collection edit)
                if pub.get("kuaishou", {}).get("status") != "ok":
                    print(f"\n[2.ks] {v['name']} ({date})", flush=True)
                    try:
                        ks_page = find_page(ctx, "cp.kuaishou.com/article/publish")
                        if ks_page is None:
                            ks_page = ctx.new_page()
                        res = publish_kuaishou(
                            ks_page,
                            title=v["title"], description=v["description"],
                            video=v["path"], fast_mode=True,
                        )
                        record_result(queue, v["path"], "kuaishou", res)
                        save_queue(queue)
                        print(f"  -> {res.status} err={(res.error or '')[:80]}", flush=True)
                    except Exception as e:
                        print(f"  ks exception: {e}", flush=True)
                # Force close all extra pages to avoid OOM
                try:
                    keep_urls = ("creator.douyin.com", "cp.kuaishou.com", "channels.weixin")
                    for pg in list(ctx.pages):
                        keep = any(u in pg.url for u in keep_urls)
                        if not keep:
                            try: pg.close()
                            except Exception: pass
                except Exception:
                    pass
            save_queue(queue)

        # Phase 3: add videos to Kuaishou collections (by title match)
        print("\n[3.add-videos] kuaishou collections")
        for date in sorted(by_date):
            ks_cname = f"AI合集_KS_{date}"
            titles = [v["title"][:25] for v in by_date[date]]
            print(f"\n[3.add] {ks_cname}: {len(titles)} videos")
            try:
                fresh_page = ctx.new_page()
                ok = add_videos_to_kuaishou_collection(
                    fresh_page, collection_name=ks_cname, video_titles=titles,
                )
                print(f"  -> ok={ok}")
                try:
                    fresh_page.close()
                except Exception:
                    pass
            except Exception as e:
                print(f"  ⚠️ add failed: {e}")
                continue

    print("\n✅ done")


if __name__ == "__main__":
    main()