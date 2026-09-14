#!/usr/bin/env python3
"""Stable batch publisher — one CDP connection per platform, one tab reused
for the whole queue. Each platform runs in its own thread and processes its
own pending videos sequentially; platforms run concurrently with each other.

Flow per video is signal-driven (see scripts/platforms/*.py): upload finish
API -> fill form -> topics/AI-declaration/collection -> publish API response.
After each platform's loop, the run-day ok works are reconciled into its
daily 合集 API-direct, idempotently and best-effort (Kuaishou union update,
Douyin mix editlist; see scripts/mx_collections.py). The run day is pinned
at run start so a batch crossing midnight keeps one consistent day.

Env:
  SKIP_PLATFORMS   comma-separated, e.g. SKIP_PLATFORMS=xhs
  ONLY_PLATFORMS   comma-separated whitelist
  CDP_URL          default http://127.0.0.1:9222
  RETRY_FAILED=1   retry videos whose last attempt was a transient fail
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from publisher import PublishResult, track, _get_module, is_transient_error
from core.environment import ensure_chrome, get_env_summary
from core.watch import is_fatal_connection_error

QUEUE_FILE = HERE / "publish_queue.json"
DEFAULT_PLATFORMS = ["douyin", "kuaishou", "weixin", "xhs"]
DONE_STATUSES = ("ok", "blocked")

_state_lock = threading.Lock()


def load_queue() -> dict:
    return json.loads(QUEUE_FILE.read_text())


def save_queue(queue: dict) -> None:
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(queue, indent=2, ensure_ascii=False))
    tmp.replace(QUEUE_FILE)


def resolve_platforms() -> list:
    only = os.environ.get("ONLY_PLATFORMS", "").strip()
    if only:
        plats = [p.strip() for p in only.split(",") if p.strip()]
    else:
        skip = {p.strip() for p in os.environ.get("SKIP_PLATFORMS", "").split(",") if p.strip()}
        # XHS profile is logged out — skip unless explicitly whitelisted
        skip.add("xhs")
        plats = [p for p in DEFAULT_PLATFORMS if p not in skip]
    return plats


def derive_topics(video: dict) -> list:
    """Deterministic topic fallback from theme + title when queue has none."""
    existing = [t.lstrip("#") for t in (video.get("hashtags") or []) if t.strip()]
    if existing:
        return existing[:3]
    topics = []
    theme = (video.get("_theme") or "").replace("/", " ").split()
    for word in theme + [video.get("title", "")]:
        word = word.strip()
        if word and word not in topics:
            topics.append(word)
    return topics[:3]


def needs_publish(video: dict, platform: str) -> bool:
    info = video.get("published", {}).get(platform, {})
    if isinstance(info, dict) and info.get("status") in DONE_STATUSES:
        return False
    return True


def result_dict(result: PublishResult) -> dict:
    return {
        "ts": time.time(),
        "method": result.method,
        "status": result.status,
        "error": (result.error or "")[:200],
        "duration_s": round(result.duration_s or 0, 1),
    }


def attach_kuaishou_daily_collection(page, queue: dict, *, coll_name: str,
                                     day_start: float) -> None:
    """After KS publishes, attach run-day ok works to daily 合集 API-direct.

    KS membership is exclusive and update replaces photoIdList; the helper
    unions existing members and is idempotent, so attaching all of the day's
    works on every run is safe. Best-effort: never alters publish statuses.
    """
    from mx_collections import (
        add_videos_to_kuaishou_collection,
        get_or_create_kuaishou_collection,
    )

    titles = [
        v.get("title", "")
        for v in queue["videos"]
        if (info := v.get("published", {}).get("kuaishou"))
        and isinstance(info, dict)
        and info.get("status") == "ok"
        and (info.get("ts") or 0) >= day_start
    ]
    if not titles:
        print("[kuaishou] no works published on the run-start day; skip 合集 attach")
        return
    try:
        if get_or_create_kuaishou_collection(page, name=coll_name):
            add_videos_to_kuaishou_collection(
                page, collection_name=coll_name, video_titles=titles)
    except Exception as e:
        if is_fatal_connection_error(e):
            raise
        print(f"[kuaishou] daily collection attach failed (non-fatal): "
              f"{str(e)[:150]}")


def reconcile_douyin_collection(page, queue: dict, *, coll_name: str,
                                day_start: float) -> None:
    """After DY publishes, repair the daily mix: attach any run-day ok work
    whose publish-time dropdown attach missed. API-direct, idempotent,
    best-effort; never alters publish statuses.
    """
    from mx_collections import reconcile_douyin_daily_mix

    titles = [
        v.get("title", "")
        for v in queue["videos"]
        if (info := v.get("published", {}).get("douyin"))
        and isinstance(info, dict)
        and info.get("status") == "ok"
        and (info.get("ts") or 0) >= day_start
    ]
    if not titles:
        print("[douyin] no works published on the run-start day; skip mix reconcile")
        return
    try:
        reconcile_douyin_daily_mix(
            page, mix_name=coll_name, wanted_titles=titles,
            day_start_ts=day_start)
    except Exception as e:
        if is_fatal_connection_error(e):
            raise
        print(f"[douyin] mix reconcile failed (non-fatal): {str(e)[:150]}")


def run_platform(platform: str, queue: dict, cdp_url: str) -> dict:
    """Own connection + own tab; iterate every pending video for this platform."""
    from playwright.sync_api import sync_playwright

    summary = {"ok": 0, "fail": 0, "partial": 0, "errors": []}
    videos = [v for v in queue["videos"] if needs_publish(v, platform)]
    limit = os.environ.get("LIMIT_PER_PLATFORM", "").strip()
    if limit.isdigit():
        videos = videos[:int(limit)]
    # KS/DY still connect with zero pending to reconcile the run-day 合集.
    # Pin the day at run start: a long batch can cross midnight, and the
    # collection name/ts filter must not flip halfway through the run.
    day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"),
                                          "%Y-%m-%d"))
    today_coll = f"AI合集_{time.strftime('%Y%m%d')}"
    ks_coll_name = f"AI合集{time.strftime('%m%d')}"
    if not videos and platform not in ("kuaishou", "douyin"):
        return summary

    print(f"[{platform}] {len(videos)} videos pending")
    mod = _get_module(platform)
    topics_map = {id(v): derive_topics(v) for v in videos}

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=20000)
    except Exception as e:
        print(f"[{platform}] connect failed: {e}")
        summary["fail"] = len(videos)
        summary["errors"].append(f"connect: {str(e)[:120]}")
        return summary
    ctx = browser.contexts[0]

    try:
        page = None
        if hasattr(mod, "_match_url"):
            for pg in ctx.pages:
                try:
                    if mod._match_url()(pg.url):
                        page = pg
                        break
                except Exception:
                    continue
        if page is None:
            page = ctx.new_page()

        connection_dead = False
        for idx, video in enumerate(videos, 1):
            if connection_dead:
                break
            name = video["name"]
            title = video.get("title", "")
            short = f"{title} ({name[:20]})"
            t0 = time.time()
            result = None
            collection = video.get("collection")
            if platform == "douyin" and not collection:
                collection = today_coll
            for attempt in (1, 2):
                try:
                    result = mod.publish_on_page(
                        page,
                        title=title,
                        description=video.get("description", ""),
                        video=video["path"],
                        topics=topics_map[id(video)],
                        location=video.get("location"),
                        collection=collection,
                    )
                    result.method = result.method or "cdp"
                except Exception as e:
                    dead = is_fatal_connection_error(e)
                    result = PublishResult(platform, "fail", method="cdp",
                                           error=("fatal_connection_closed" if dead
                                                  else f"exception: {str(e)[:180]}"))
                    if dead:
                        result.duration_s = time.time() - t0
                        print(f"[{platform}] CDP connection dead; aborting "
                              f"remaining videos: {e}", flush=True)
                        connection_dead = True
                        break
                result.duration_s = time.time() - t0
                if result.status == "ok" or attempt == 2:
                    break
                if not (is_transient_error(result.error)
                        or result.error.startswith(("upload:", "fill_form:", "upload_not_ready"))):
                    break
                print(f"[{platform}] {idx}/{len(videos)} retry after: "
                      f"{(result.error or '')[:80]}")
                time.sleep(3)
                try:
                    if hasattr(mod, "_setup_page"):
                        mod._setup_page(page)
                except Exception as se:
                    if is_fatal_connection_error(se):
                        connection_dead = True
                        break

            with _state_lock:
                video.setdefault("published", {})[platform] = result_dict(result)
                save_queue(queue)
                track(platform, result)

            summary[result.status if result.status in summary else "fail"] += 1
            mark = {"ok": "✅", "partial": "⚠️ ", "fail": "❌"}.get(result.status, "?")
            print(f"[{platform}] {idx}/{len(videos)} {mark} {result.status} "
                  f"{result.duration_s:.0f}s {short} "
                  f"{(result.error or result.details.get('final') or '')[:70]}")

        if platform == "kuaishou" and not connection_dead:
            attach_kuaishou_daily_collection(
                page, queue, coll_name=ks_coll_name, day_start=day_start)
        elif platform == "douyin" and not connection_dead:
            reconcile_douyin_collection(
                page, queue, coll_name=today_coll, day_start=day_start)
    finally:
        try:
            pw.stop()
        except Exception:
            pass

    return summary


def recompute_totals(queue: dict, platforms: list) -> None:
    queue["published"] = sum(
        1 for v in queue["videos"]
        if all(isinstance(v.get("published", {}).get(p), dict)
               and v["published"][p].get("status") in DONE_STATUSES for p in platforms)
    )
    queue["remaining"] = queue["total"] - queue["published"]


def main() -> int:
    env = get_env_summary()
    cdp_url = ensure_chrome(headless=env["is_headless"])
    print(f"🌍 cdp={cdp_url} headless={env['is_headless']}")

    queue = load_queue()
    platforms = resolve_platforms()
    pending = {p: sum(1 for v in queue["videos"] if needs_publish(v, p)) for p in platforms}
    print(f"📋 {queue['total']} videos; pending: {pending}")

    results = {}
    with ThreadPoolExecutor(max_workers=len(platforms)) as pool:
        futs = {pool.submit(run_platform, p, queue, cdp_url): p for p in platforms}
        for fut in as_completed(futs):
            platform = futs[fut]
            try:
                results[platform] = fut.result()
            except Exception as e:
                results[platform] = {"ok": 0, "fail": 1, "errors": [str(e)[:150]]}

    with _state_lock:
        recompute_totals(queue, platforms)
        save_queue(queue)

    print("\n========== SUMMARY ==========")
    for p in platforms:
        r = results.get(p, {})
        print(f"  {p:9s} ok={r.get('ok', 0)} partial={r.get('partial', 0)} "
              f"fail={r.get('fail', 0)} {'; '.join(r.get('errors', []))[:100]}")
    print(f"📊 queue: {queue['published']}/{queue['total']} fully done, "
          f"{queue['remaining']} remaining")
    return 0 if all(r.get("fail", 0) == 0 for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
