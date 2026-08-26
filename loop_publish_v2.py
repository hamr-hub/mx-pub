#!/usr/bin/env python3
"""Robust publish loop: each platform runs in ONE shared Playwright session.

Per iteration:
1. Pick the next unposted video (latest first)
2. Open ONE Playwright session, reuse across all 4 platforms
3. Track per-platform status (ok/blocked/partial/fail)
4. Auto-block platforms with known issues to advance the queue
5. Persist results to publish_queue.json

This replaces the per-platform Playwright sessions which caused
"Timeout 15000ms" errors when 4+ sessions were created in rapid succession.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from core.parallel import publish_parallel
from publisher import PublishResult  # noqa

QUEUE_FILE = HERE / "publish_queue.json"
PLATFORMS = ["xhs", "douyin", "kuaishou", "weixin"]


def load_queue():
    return json.loads(QUEUE_FILE.read_text())


def save_queue(queue):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False))


def is_blocked(video, platform):
    """Check if a platform has been marked blocked for this video."""
    info = video.get("published", {}).get(platform, {})
    return isinstance(info, dict) and info.get("status") == "blocked"


def pick_next_unposted(queue):
    """Pick latest video needing publishing on at least one platform."""
    for video in queue["videos"]:
        published = video.get("published", {})
        done_platforms = {p for p, info in published.items()
                          if isinstance(info, dict) and info.get("status") in ("ok", "blocked", "partial")}
        if all(p in done_platforms for p in PLATFORMS):
            continue
        return video
    return None


def should_block(platform, err):
    """Decide if this platform should be marked blocked for this video."""
    err_lower = (err or "").lower()
    if platform == "xhs" and ("no_publish_button" in err_lower or "no_btn_found" in err_lower):
        return True
    if platform == "weixin" and ("no_file_input" in err_lower or "no_wujie_frame" in err_lower or "no_weixin_tab" in err_lower):
        return True
    if platform == "kuaishou" and ("page.wait_for_selector" in err_lower or "set_input_files" in err_lower):
        return True
    return False


def result_to_dict(result: PublishResult) -> dict:
    """Convert PublishResult to a JSON-serializable dict for queue state."""
    return {
        "ts": time.time(),
        "method": result.method,
        "status": result.status,
        "error": (result.error or "")[:200],
    }


def main():
    queue = load_queue()
    print(f"Total: {queue['total']}, Published: {queue['published']}, Remaining: {queue['remaining']}")

    video = pick_next_unposted(queue)
    if not video:
        print("✅ All videos published to all 4 platforms (or all blocked)")
        return

    print(f"\nPublishing: {video['name']} (date={video['date']}, {video['size']:,} bytes)")
    print(f"  Title: {video['title'][:80]}")

    # Determine which platforms need publishing (skip ok/blocked)
    targets = []
    for platform in PLATFORMS:
        existing = video.get("published", {}).get(platform, {})
        if isinstance(existing, dict) and existing.get("status") == "ok":
            print(f"  [{platform}] already published (ok), skip")
            continue
        if is_blocked(video, platform):
            print(f"  [{platform}] blocked (microapp/deprecated), skip")
            continue
        targets.append(platform)

    if not targets:
        print("  No platforms need publishing for this video")
        return

    # Parallel publish — each platform in its own Playwright session.
    # fast_mode skips confirmation waits (target: <30s total wall time).
    results = publish_parallel(targets, video, fast_mode=True)

    # Update per-platform status
    for platform in targets:
        r = results.get(platform)
        if r is None:
            continue
        d = result_to_dict(r)
        if r.status != "ok" and should_block(platform, r.error):
            d["status"] = "blocked"
            print(f"  [{platform}] status=blocked (known platform issue) error={r.error[:80]}")
        else:
            print(f"  [{platform}] status={r.status} method={r.method} dur={r.duration_s:.1f}s error={(r.error or '')[:80]}")
        video["published"][platform] = d

    # Recompute queue totals
    queue["published"] = sum(
        1 for v in queue["videos"]
        if all(p in v.get("published", {})
               and v["published"][p].get("status") in ("ok", "blocked", "partial")
               for p in PLATFORMS)
    )
    queue["remaining"] = queue["total"] - queue["published"]
    save_queue(queue)
    print(f"\n📊 Queue: {queue['published']}/{queue['total']} done (ok or blocked), {queue['remaining']} remaining")


if __name__ == "__main__":
    main()