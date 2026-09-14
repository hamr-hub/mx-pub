#!/usr/bin/env python3
"""
Dedup 2026-08-31 videos (strip -1/-2 duplicates) and append to publish_queue.json.

Strategy:
- csv `task_id` is the base name (matches `cgt-{ts}-{hash}.mp4`)
- Keep only succeeded tasks (rc==0)
- For each base name, prefer the file without -1/-2 suffix; if absent, pick the one with smallest mtime
- Merge into existing publish_queue.json (preserve already-published status)
"""
import csv
import json
import re
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
IMAGES_DIR = Path("/Users/hyx/ssd/codespace/personal/images")
DATE_DIR = IMAGES_DIR / "2026-08-31"
VIDEOS_DIR = DATE_DIR / "videos"
CSV_FILE = DATE_DIR / "logs" / "platform-cash-batch-summary.csv"
QUEUE_FILE = HERE / "publish_queue.json"

# tag extractor (mirrors build_publish_queue.py)
TAG_KEYWORDS = [
    "cinematic", "macro", "wildlife", "timelapse", "duel", "battle", "fire",
    "storm", "lightning", "ocean", "mountain", "forest", "city", "night",
    "sunset", "portrait", "samurai", "wuxia", "fantasy", "scifi",
    "campfire", "hummingbird", "tokyo", "kyoto", "rain", "snow", "sunrise",
    "cherry", "blossom", "sakura", "beach", "desert", "canyon", "aurora",
    "moon", "stars", "milkyway", "underwater", "dragon", "kite", "child",
    "running", "butterfly", "dandelion", "willow", "bamboo", "wolf", "eagle",
    "festival", "lantern", "snowfall", "fog", "mist",
]


def extract_hashtags(prompt: str, max_tags: int = 5) -> list:
    if not prompt:
        return []
    lower = prompt.lower()
    tags = []
    seen = set()
    for kw in TAG_KEYWORDS:
        if kw in lower and kw not in seen:
            tags.append("#" + kw)
            seen.add(kw)
            if len(tags) >= max_tags:
                break
    return tags


def load_csv_prompts() -> dict:
    """base_name -> prompt (only succeeded)."""
    out = {}
    with open(CSV_FILE) as f:
        for row in csv.DictReader(f):
            if row.get("status") != "succeeded":
                continue
            task_id = row.get("task_id", "").strip()
            prompt = row.get("prompt", "").strip().strip('"')
            if task_id and prompt:
                out[task_id] = prompt
    return out


def dedup_videos() -> dict:
    """base_name -> Path (prefer no-suffix, else smallest mtime)."""
    by_base: dict[str, list[Path]] = {}
    for p in VIDEOS_DIR.glob("cgt-*.mp4"):
        # base = remove -1/-2 before .mp4
        m = re.match(r"^(cgt-\d+-[a-z0-9]+)(?:-\d)?$", p.stem)
        if not m:
            continue
        by_base.setdefault(m.group(1), []).append(p)

    chosen: dict[str, Path] = {}
    for base, paths in by_base.items():
        # Prefer the one whose stem == base (no -1/-2)
        primary = [p for p in paths if p.stem == base]
        if primary:
            chosen[base] = primary[0]
        else:
            chosen[base] = min(paths, key=lambda x: x.stat().st_mtime)
    return chosen


def main():
    prompts = load_csv_prompts()
    print(f"[csv] {len(prompts)} succeeded prompts")

    by_base = dedup_videos()
    print(f"[dedup] {sum(len(v) for v in [])}  unique base names: {len(by_base)}")

    # Keep only bases with a succeeded prompt
    matched = []
    for base, path in by_base.items():
        prompt = prompts.get(base)
        if not prompt:
            continue
        title = prompt.split(",")[0].strip()[:60]
        hashtags = extract_hashtags(prompt)
        desc = (prompt + " " + " ".join(hashtags))[:500]
        stat = path.stat()
        matched.append({
            "path": str(path),
            "name": path.name,
            "date": "2026-08-31",
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "title": title,
            "description": desc,
            "hashtags": hashtags,
            "published": {},
        })

    matched.sort(key=lambda v: v["mtime"], reverse=True)
    print(f"[match] {len(matched)} videos have succeeded prompt")

    # Load existing queue
    queue = json.loads(QUEUE_FILE.read_text())
    existing_paths = {v["path"] for v in queue["videos"]}
    new_videos = [v for v in matched if v["path"] not in existing_paths]
    print(f"[queue] existing: {len(queue['videos'])}, new from 2026-08-31: {len(new_videos)}")

    queue["videos"] = new_videos + queue["videos"]  # newest first
    queue["generated_at"] = datetime.now().isoformat()
    queue["total"] = len(queue["videos"])
    queue["published"] = sum(
        1 for v in queue["videos"] if v.get("published") and any(
            isinstance(info, dict) and info.get("status") == "ok"
            for info in v["published"].values()
        )
    )
    queue["remaining"] = sum(
        1 for v in queue["videos"]
        if not all(p in v.get("published", {}) for p in ["xhs", "douyin", "kuaishou", "weixin"])
    )

    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False))
    print(f"[write] queue.total = {queue['total']}, remaining = {queue['remaining']}")
    print(f"[write] {QUEUE_FILE}")


if __name__ == "__main__":
    main()
