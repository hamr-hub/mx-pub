#!/usr/bin/env python3
"""Build a content-themed publish queue from a curated spec.

Pipeline (all gates are fail-closed — a duplicate post is worse than a miss):
  1. resolve every spec path via all_providers.csv / series.json metadata
  2. md5: batch-unique, and unique against a global hash index when given
  3. server truth: douyin + kuaishou live title sets via CDP in-page fetch;
     weixin titles from stable-batch 'ok' rows of the CURRENT queue file
  4. reject when the clip's topic signature already went live on ANY target,
     or when its Chinese title collides with a live title
  5. validate platform copy rules (KS 合集 <=12 chars; WX 短标题 6-14 chars)
  6. merge survivors into publish_queue.json (existing entries preserved),
     ordered collection-by-collection, series episodes in episode order

Default is dry-run; --apply writes (with a /tmp backup of the old queue).

Usage:
  python3 scripts/build_themed_queue.py curations/20260915_content_themed.json
  python3 scripts/build_themed_queue.py <spec> --apply [--md5-index /tmp/all_md5.txt]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from curation import (  # noqa: E402
    daily_clips,
    is_live_topic,
    load_weixin_titles,
    live_titles,
    md5_of,
    series_clips,
    signature,
)

REPO = HERE.parent
QUEUE_FILE = REPO / "publish_queue.json"
KS_COLL_MAX = 12
WX_TITLE_MIN = 6
WX_TITLE_MAX = 14
TARGET_PLATFORMS = ("douyin", "kuaishou", "weixin")


def load_global_md5(path: Path) -> dict[str, str]:
    """Parse `md5 -r` output (\"<hash> <path>\") into hash -> path."""
    index: dict[str, str] = {}
    for line in path.read_text(errors="ignore").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{32}", parts[0]):
            index[parts[0]] = parts[1]
    return index


def clip_index(root: Path) -> dict[str, object]:
    """Relative-path -> Clip for every daily render and 20260826 series ep."""
    clips: dict[str, object] = {}
    for clip in daily_clips(root):
        clips[clip.path.relative_to(root).as_posix()] = clip
    for clip in series_clips(root, batch_hint="20260826"):
        rel = clip.path.relative_to(root).as_posix()
        clips[rel] = clip
    return clips


def cjk_len(text: str) -> int:
    return len(re.findall(r"[一-鿿]", text))


def cjk_collision(title: str, live: list[str]) -> str:
    """Substring collision on the CJK spine of the title (4+ CJK chars)."""
    spine = "".join(re.findall(r"[一-鿿]", title))
    if len(spine) < 4:
        return ""
    for other in live:
        other_spine = "".join(re.findall(r"[一-鿿]", other))
        if len(other_spine) >= 4 and (spine in other_spine or other_spine in spine):
            return other
    return ""


def local_ok_titles(queue_path: Path, platform: str) -> list[str]:
    """Titles this queue records as server-confirmed posts on `platform`."""
    queue = json.loads(queue_path.read_text())
    return [v.get("title", "") for v in queue.get("videos", [])
            if isinstance(v.get("published", {}).get(platform), dict)
            and v["published"][platform].get("status") == "ok"]


def validate_copy(entry: dict) -> list[str]:
    problems = []
    coll = entry.get("collection", "")
    if not coll:
        problems.append("missing collection")
    elif cjk_len(coll) > KS_COLL_MAX:
        problems.append(f"collection '{coll}' exceeds KS {KS_COLL_MAX}-char cap")
    title = entry.get("title", "")
    n = cjk_len(title)
    if not (WX_TITLE_MIN <= n <= WX_TITLE_MAX):
        problems.append(f"title '{title}' has {n} CJK chars; WX requires "
                        f"{WX_TITLE_MIN}-{WX_TITLE_MAX}")
    if not entry.get("description"):
        problems.append("missing description")
    if len(entry.get("hashtags") or []) == 0:
        problems.append("missing hashtags")
    return problems


def verify(spec: dict, *, md5_index: dict[str, str] | None,
           truth: dict[str, list[str]]) -> tuple[list[dict], list[dict]]:
    """Return (accepted entries with resolved metadata, rejection records)."""
    root = Path(spec["images_root"])
    clips = clip_index(root)
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_md5: dict[str, str] = {}
    seen_episodes: dict[tuple[str, int], str] = {}

    spec_derivative = bool(spec.get("derivative"))
    for entry in spec["videos"]:
        rel = entry["path"]
        reasons: list[str] = []
        problems = validate_copy(entry)
        is_der = spec_derivative or bool(entry.get("derivative"))

        # Derivatives live outside the images render tree (artifacts/), carry
        # their own English topic, and DELIBERATELY revisit live topics, so the
        # csv-metadata and live-topic gates do not apply.
        raw_path = Path(rel)
        abs_path = raw_path if raw_path.is_absolute() else root / rel
        clip = None if is_der else clips.get(rel)
        if not abs_path.exists():
            reasons.append("file missing")
        if not is_der and clip is None:
            reasons.append("metadata not found in csv/series.json")

        digest = md5_of(abs_path) if abs_path.exists() else ""
        if digest:
            if digest in seen_md5:
                reasons.append(f"md5 duplicate of {seen_md5[digest]}")
            else:
                seen_md5[digest] = rel
            if md5_index is not None and not is_der:
                # Index paths may be relative to the images root (`md5 -r`
                # was run there) or absolute; resolve both before comparing.
                other = md5_index.get(digest)
                if other:
                    other_path = Path(other)
                    if not other_path.is_absolute():
                        other_path = root / other_path
                    if other_path.resolve() != abs_path.resolve():
                        reasons.append(f"md5 duplicate of global file {other}")

        if is_der:
            topic = entry.get("topic", "")
            if not topic:
                reasons.append("derivative without topic seed")
        else:
            topic = getattr(clip, "topic", "") if clip else ""
            if clip is not None and not topic:
                reasons.append("empty topic metadata")
        for platform in TARGET_PLATFORMS:
            if not is_der:
                match = is_live_topic(topic, truth.get(platform, [])) if topic else None
                if match:
                    reasons.append(f"topic already live on {platform}: {match[:60]}")
            title_hit = cjk_collision(entry["title"], truth.get(platform, []))
            if title_hit:
                reasons.append(f"title collides on {platform}: {title_hit}")

        if is_der:
            story = episode = None
        else:
            story = getattr(clip, "extra", {}).get("story") if clip else None
            episode = getattr(clip, "extra", {}).get("episode") if clip else None
        if story is not None:
            key = (story, episode)
            if key in seen_episodes:
                reasons.append(f"series ep listed twice ({story} ep{episode})")
            seen_episodes[key] = rel

        if problems:
            reasons.extend(problems)
        if reasons:
            rejected.append({"path": rel, "reasons": reasons})
            continue

        stat = abs_path.stat()
        queue_entry = {
            "path": str(abs_path),
            "name": abs_path.name,
            "date": time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "title": entry["title"],
            "description": entry["description"],
            "hashtags": entry["hashtags"],
            "collection": entry["collection"],
            "_theme": entry["collection"],
            "_topic": topic,
            "_sig": signature(topic),
            "published": {},
        }
        if is_der:
            # Provenance for re-edits of validated high-VV own works.
            queue_entry["derivative"] = True
            queue_entry["sources"] = entry.get("sources", [])
        accepted.append(queue_entry)

    return accepted, rejected


def merge_queue(existing: dict, accepted: list[dict], batch: str) -> dict:
    """Append accepted clips not already present; never touch done entries."""
    videos = existing.get("videos", [])
    known = {v.get("path"): v for v in videos}
    added = 0
    for entry in accepted:
        old = known.get(entry["path"])
        if old is not None:
            done = any(
                isinstance(old.get("published", {}).get(p), dict)
                and old["published"][p].get("status") in ("ok", "blocked")
                for p in TARGET_PLATFORMS)
            if not done:
                # Pending duplicate: refresh copy/collection, keep attempts.
                published = old.get("published", {})
                entry["published"] = published
                old.clear()
                old.update(entry)
            continue
        videos.append(entry)
        added += 1
    total = len(videos)
    done = sum(
        1 for v in videos
        if all(isinstance(v.get("published", {}).get(p), dict)
               and v["published"][p].get("status") in ("ok", "blocked")
               for p in TARGET_PLATFORMS))
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "curation": batch,
        "total": total,
        "published": done,
        "remaining": total - done,
        "videos": videos,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", type=Path)
    ap.add_argument("--apply", action="store_true",
                    help="write publish_queue.json (default: dry-run report)")
    ap.add_argument("--md5-index", type=Path, default=None,
                    help="global `md5 -r` index for exact-duplicate gate")
    ap.add_argument("--queue", type=Path, default=QUEUE_FILE)
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    args = ap.parse_args()

    spec = json.loads(args.spec.read_text())
    global_index = load_global_md5(args.md5_index) if args.md5_index else None
    if global_index is not None:
        print(f"🔐 global md5 index: {len(global_index)} hashes")

    print("🔎 fetching server truth (douyin + kuaishou via CDP, "
          "weixin from queue ok rows)…")
    try:
        truth = live_titles(args.cdp_url, timeout_ms=60000)
    except Exception as e:
        print(f"   browser-level CDP unavailable ({str(e)[:80]}); "
              "falling back to per-target raw CDP…")
        from curation import live_titles_raw
        truth = live_titles_raw(args.cdp_url)
    truth["weixin"] = load_weixin_titles(args.queue)
    for platform in TARGET_PLATFORMS:
        print(f"   {platform}: {len(truth.get(platform, []))} live titles")

    # A platform whose live fetch came back empty while THIS queue records
    # confirmed posts there means the fetch silently failed — the duplicate
    # gate would then approve every clip as new. A duplicate post is worse
    # than a miss, so stop instead of building blind.
    blind = [p for p in TARGET_PLATFORMS
             if not truth.get(p) and local_ok_titles(args.queue, p)]
    if blind:
        sys.exit("[truth] live fetch returned 0 titles for "
                 f"{', '.join(blind)} although the queue records confirmed "
                 "posts there — refusing to build with a blind duplicate gate.")

    accepted, rejected = verify(spec, md5_index=global_index, truth=truth)

    print(f"\n✅ accepted: {len(accepted)}")
    coll_count: dict[str, int] = {}
    for v in accepted:
        coll_count[v["collection"]] = coll_count.get(v["collection"], 0) + 1
        print(f"   [{v['collection']}] {v['title']}  ←  {Path(v['path']).parent.name}/{v['name']}")
    for coll, n in coll_count.items():
        print(f"   合集 {coll}: {n}")

    if rejected:
        print(f"\n🚫 rejected: {len(rejected)}")
        for rec in rejected:
            print(f"   {rec['path']}")
            for reason in rec["reasons"]:
                print(f"      - {reason}")

    if not args.apply:
        print("\n(dry-run; pass --apply to write the queue)")
        return 0 if accepted and not rejected else 1

    existing = json.loads(args.queue.read_text()) if args.queue.exists() else {}
    backup = Path("/tmp") / f"queue_{spec['batch']}_{time.strftime('%H%M%S')}.json"
    shutil.copy2(args.queue, backup)
    merged = merge_queue(existing, accepted, spec["batch"])
    tmp = args.queue.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    tmp.replace(args.queue)
    print(f"\n💾 wrote {args.queue} (backup: {backup})")
    print(f"📊 total={merged['total']} published={merged['published']} "
          f"remaining={merged['remaining']}")
    return 0 if not rejected else 2


if __name__ == "__main__":
    sys.exit(main())
