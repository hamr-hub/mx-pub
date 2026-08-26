#!/usr/bin/env python3
"""
Robust publish loop: each platform independent, 1 video per batch.

Per platform:
- xhs: new URL + navigate; if no publish button, mark blocked (microapp)
- douyin: navigate to /content/publish
- kuaishou: navigate to /article/publish/video; click "立即发布"
- weixin: navigate to /platform/post/create; if no file_input, mark blocked

Continues to next video regardless of failures.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from publisher import publish, PublishResult  # noqa

QUEUE_FILE = HERE / "publish_queue.json"
PLATFORMS = ["xhs", "douyin", "kuaishou", "weixin"]
MAX_RETRIES_BEFORE_BLOCK = 2  # mark platform as blocked after N consecutive fails


def load_queue():
    return json.loads(QUEUE_FILE.read_text())


def save_queue(queue):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False))


def is_blocked(video, platform):
    """Check if a platform has been marked blocked for this video (avoid re-trying forever)."""
    info = video.get("published", {}).get(platform, {})
    return isinstance(info, dict) and info.get("status") == "blocked"


def pick_next_unposted(queue):
    """Pick latest video needing publishing on at least one platform."""
    for video in queue["videos"]:
        published = video.get("published", {})
        done_platforms = {p for p, info in published.items()
                          if isinstance(info, dict) and info.get("status") in ("ok", "blocked", "partial")}
        # If all 4 platforms are done (ok/blocked/partial), skip this video
        if all(p in done_platforms for p in PLATFORMS):
            continue
        return video
    return None


def publish_to_platform(platform, video):
    """Publish video to one platform. Returns dict with status, error, method."""
    video_path = video["path"]
    title = video.get("title", "")
    description = video.get("description", "")
    hashtags = video.get("hashtags", [])

    print(f"  [{platform}] publishing...")
    try:
        result = publish(
            platform,
            title=title,
            description=description,
            video=video_path,
            topics=hashtags,
        )
        result.tokens_used = 0
        return {
            "ts": time.time(),
            "method": result.method,
            "status": result.status,
            "error": (result.error or "")[:200],
        }
    except Exception as e:
        return {
            "ts": time.time(),
            "method": "exception",
            "status": "fail",
            "error": str(e)[:200],
        }


def should_block(video, platform, err):
    """Decide if this platform should be marked blocked for this video."""
    err_lower = (err or "").lower()
    # XHS publish button is micro-frontend placeholder
    if platform == "xhs" and ("no_publish_button" in err_lower or "no_btn_found" in err_lower):
        return True
    # Weixin: web uploader completely removed OR no weixin tab in browser
    if platform == "weixin" and ("no_file_input" in err_lower or "no_wujie_frame" in err_lower or "no_weixin_tab" in err_lower):
        return True
    # Douyin: only works when user has manually opened the page (CDP upload timeout otherwise)
    if platform == "douyin" and "timeout_waiting_for_publish_confirm" in err_lower:
        return False  # keep retrying; sometimes succeeds
    # Kuaishou: file input never appears (no active browser session) — block so loop advances
    if platform == "kuaishou" and ("page.wait_for_selector" in err_lower or "set_input_files" in err_lower):
        return True
    return False


def main():
    queue = load_queue()
    print(f"Total: {queue['total']}, Published: {queue['published']}, Remaining: {queue['remaining']}")

    video = pick_next_unposted(queue)
    if not video:
        print("✅ All videos published to all 4 platforms (or all blocked)")
        return

    print(f"\nPublishing: {video['name']} (date={video['date']}, {video['size']:,} bytes)")
    print(f"  Title: {video['title'][:80]}")

    results = {}
    for platform in PLATFORMS:
        existing = video.get("published", {}).get(platform, {})
        # Skip if already ok
        if isinstance(existing, dict) and existing.get("status") == "ok":
            print(f"  [{platform}] already published (ok), skip")
            results[platform] = existing
            continue
        # Skip if blocked
        if is_blocked(video, platform):
            print(f"  [{platform}] blocked (microapp/deprecated), skip")
            results[platform] = existing
            continue

        result = publish_to_platform(platform, video)
        # Auto-block after threshold for known platform issues
        if result["status"] != "ok" and should_block(video, platform, result.get("error", "")):
            result["status"] = "blocked"
            print(f"  [{platform}] status=blocked (known platform issue) error={result.get('error','')[:80]}")
        else:
            print(f"  [{platform}] status={result['status']} method={result['method']} error={result.get('error','')[:80]}")
        results[platform] = result

    # Update queue
    video["published"].update(results)
    queue["published"] = sum(
        1 for v in queue["videos"]
        if all(
            p in v.get("published", {})
            and v["published"][p].get("status") in ("ok", "blocked", "partial")
            for p in PLATFORMS
        )
    )
    queue["remaining"] = queue["total"] - queue["published"]
    save_queue(queue)
    print(f"\n📊 Queue: {queue['published']}/{queue['total']} done (ok or blocked), {queue['remaining']} remaining")


if __name__ == "__main__":
    main()
