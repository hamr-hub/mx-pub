#!/usr/bin/env python3
"""
Build publish_queue.json: all videos sorted by date desc, with topic_seed from prompts.csv.
Mark files as published if they appear in publish_state.json history.
"""
import json
import csv
import re
from pathlib import Path
from datetime import datetime

IMAGES_DIR = Path("/Users/hyx/ssd/codespace/personal/images")
QUEUE_FILE = Path("/Users/hyx/workspace/mx-pub/publish_queue.json")
STATE_FILE = Path("/Users/hyx/workspace/mx-pub/publish_state.json")


def extract_title_from_prompt(prompt_text: str) -> str:
    """Extract a short title from prompt (first 30 chars before comma)."""
    if not prompt_text:
        return ""
    # First sentence/phrase (until comma or 40 chars)
    title = prompt_text.split(",")[0].strip()
    if len(title) > 50:
        title = title[:47] + "..."
    return title


def extract_hashtags(prompt_text: str, max_tags: int = 5) -> list:
    """Generate hashtags from prompt keywords."""
    if not prompt_text:
        return []
    # Take nouns / key phrases
    words = re.findall(r'\b[a-zA-Z]{4,}\b', prompt_text.lower())
    seen = set()
    tags = []
    keywords = ["cinematic", "macro", "wildlife", "timelapse", "duel", "battle", "fire",
                "storm", "lightning", "ocean", "mountain", "forest", "city", "night",
                "sunset", "portrait", "samurai", "wuxia", "fantasy", "scifi",
                "campfire", "hummingbird", "tokyo", "kyoto", "rain", "snow"]
    for kw in keywords:
        if kw in prompt_text.lower() and kw not in seen:
            tags.append("#" + kw)
            seen.add(kw)
            if len(tags) >= max_tags:
                break
    return tags


def build_video_metadata(video_path: Path) -> dict:
    """Build metadata for a video from prompts.csv."""
    # Find prompts.csv in same directory
    csv_path = video_path.parent / "prompts.csv"
    title = ""
    description = ""
    if csv_path.exists():
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                if row.get("file_path", "").endswith(video_path.name):
                    title = row.get("topic_seed", "")
                    description = row.get("topic_seed", "")
                    break

    if not title:
        # Fallback: derive from filename
        title = f"AI generated video - {video_path.parent.name}/{video_path.stem}"

    hashtags = extract_hashtags(description)
    return {
        "title": title[:60],
        "description": (description + " " + " ".join(hashtags))[:500] if description else title,
        "hashtags": hashtags,
    }


def main():
    print("Scanning for video files...")
    videos = []
    for video_path in IMAGES_DIR.glob("**/video_*.mp4"):
        if "_benchmark" in str(video_path):
            continue
        mtime = video_path.stat().st_mtime
        date_str = video_path.parent.parent.name if video_path.parent.name in ("minimax", "pollinations") else video_path.parent.name
        # If parent is "minimax" or "pollinations", date is grandparent
        if video_path.parent.name in ("minimax", "pollinations"):
            date_str = video_path.parent.parent.name
        else:
            date_str = video_path.parent.name
        meta = build_video_metadata(video_path)
        videos.append({
            "path": str(video_path),
            "name": video_path.name,
            "date": date_str,
            "mtime": mtime,
            "size": video_path.stat().st_size,
            "title": meta["title"],
            "description": meta["description"],
            "hashtags": meta["hashtags"],
            "published": {},
        })

    # Sort by mtime DESC (latest first)
    videos.sort(key=lambda v: v["mtime"], reverse=True)

    # Mark already-published videos from state.json history
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            for hist in state.get("history", []):
                # Skip if not ok status
                if hist.get("status") != "ok":
                    continue
                # Match by file in path if mentioned
                vid = hist.get("video") or hist.get("file") or ""
                if vid:
                    for v in videos:
                        if v["path"] == vid:
                            platform = hist.get("platform")
                            v["published"][platform] = {
                                "ts": hist.get("ts"),
                                "method": hist.get("method"),
                            }
        except Exception as e:
            print(f"Warning: failed to parse state.json: {e}")

    queue = {
        "generated_at": datetime.now().isoformat(),
        "total": len(videos),
        "published": sum(1 for v in videos if v["published"]),
        "remaining": sum(1 for v in videos if not all(p in v["published"] for p in ["xhs", "douyin", "kuaishou", "weixin"])),
        "videos": videos,
    }
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False))
    print(f"Built queue with {len(videos)} videos")
    print(f"  Total: {queue['total']}")
    print(f"  Published: {queue['published']}")
    print(f"  Remaining: {queue['remaining']}")
    print(f"  Latest: {videos[0]['path']}")
    print(f"  Oldest: {videos[-1]['path']}")


if __name__ == "__main__":
    main()
