#!/usr/bin/env python3
"""Build publish_queue.json with 10 next videos from 2026-08-31/videos/ (skip first 5)."""
import csv
import json
import re
from datetime import datetime
from pathlib import Path

VIDEO_DIR = Path("/Users/hyx/ssd/codespace/personal/images/2026-08-31/videos")
PROMPTS_CSV = Path("/Users/hyx/ssd/codespace/personal/images/2026-08-31/logs/platform-cash-batch-summary.csv")
QUEUE_FILE = Path("/Users/hyx/workspace/mx-pub/publish_queue.json")

SELECTED = [
    "cgt-20260831164214-42mpk",
    "cgt-20260831164211-484jf",
    "cgt-20260831164153-k6p5g",
    "cgt-20260831164153-zd5vc",
    "cgt-20260831164143-zdfxx",
    "cgt-20260831164142-lhvjk",
    "cgt-20260831164126-9wn6d",
    "cgt-20260831164123-vbz7n",
    "cgt-20260831164117-tlhrr",
    "cgt-20260831164115-24gds",
]


def extract_hashtags(prompt: str, max_tags: int = 5) -> list:
    keywords = ["cinematic", "macro", "wildlife", "timelapse", "duel", "battle",
                "fire", "storm", "lightning", "ocean", "mountain", "forest",
                "city", "night", "sunset", "portrait", "samurai", "wuxia",
                "fantasy", "scifi", "campfire", "hummingbird", "tokyo", "kyoto",
                "rain", "snow", "autumn", "winter", "summer", "spring",
                "harvest", "lighthouse", "bamboo", "lotus", "castle"]
    tags = []
    seen = set()
    text_lower = prompt.lower()
    for kw in keywords:
        if kw in text_lower and kw not in seen:
            tags.append("#" + kw)
            seen.add(kw)
            if len(tags) >= max_tags:
                break
    return tags


def main():
    prompts = {}
    with open(PROMPTS_CSV) as f:
        for row in csv.DictReader(f):
            tid = row.get("task_id", "").strip()
            if tid:
                prompts[tid] = row.get("prompt", "").strip()

    videos = []
    for tid in SELECTED:
        video_path = VIDEO_DIR / f"{tid}.mp4"
        if not video_path.exists():
            print(f"⚠ Missing: {video_path}")
            continue
        prompt = prompts.get(tid, "")
        title = prompt.split(",")[0].strip() if prompt else f"AI generated video - {tid}"
        hashtags = extract_hashtags(prompt)
        description = (prompt + " " + " ".join(hashtags)).strip()
        mtime = video_path.stat().st_mtime
        videos.append({
            "path": str(video_path),
            "name": video_path.name,
            "date": "2026-08-31",
            "mtime": mtime,
            "size": video_path.stat().st_size,
            "title": title[:60],
            "description": description[:500],
            "hashtags": hashtags,
            "published": {},
        })

    queue = {
        "generated_at": datetime.now().isoformat(),
        "total": len(videos),
        "published": 0,
        "remaining": len(videos),
        "videos": videos,
    }
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False))
    print(f"Built queue with {len(videos)} videos (3 platforms: douyin, kuaishou, weixin)")
    for v in videos:
        print(f"  {v['name']}  title={v['title'][:30]!r}  size={v['size']:,}B")


if __name__ == "__main__":
    main()