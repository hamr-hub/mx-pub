#!/usr/bin/env python3
"""Dry-run mode: generate AI content for one or more videos WITHOUT publishing.

Demonstrates the AI integration works:
- Shows environment detection
- Reads video metadata
- Calls AI provider (or heuristic fallback)
- Prints the generated title/description/hashtags per platform
- Does NOT touch any platform

Usage:
    python preview.py /path/to/video.mp4 [--platform xhs]
    python preview.py /path/to/videos_dir/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from ai.content import generate_content
from core.environment import get_env_summary

PLATFORMS = ["xhs", "douyin", "kuaishou", "weixin"]


def preview_one(video_path: str, platform: str) -> dict:
    """Generate content for one video on one platform. Returns dict."""
    return generate_content(video_path, platform=platform)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Video file or directory of videos")
    parser.add_argument("--platform", default=None, help="One platform (default: all 4)")
    parser.add_argument("--provider", default=None, help="Force AI provider (claude/openai/ollama)")
    parser.add_argument("--limit", type=int, default=3, help="Max videos to preview (when path is dir)")
    args = parser.parse_args()

    print("=" * 60)
    print("AI content preview (dry-run, no publishing)")
    print("=" * 60)

    # Environment
    env = get_env_summary()
    print(f"\n🌍 Environment:")
    print(f"  platform: {env['platform']}")
    print(f"  headless: {env['is_headless']}")
    print(f"  chrome_cdp: {env['cdp_url'] or 'not running (would auto-launch)'}")
    print(f"  ai_providers: {env['ai_providers']}")
    active_provider = args.provider or next(
        (k for k, v in env["ai_providers"].items() if v), None
    )
    if active_provider:
        print(f"  → will use: {active_provider}")
    else:
        print(f"  → no AI key configured, will use heuristic fallback (prompts.csv)")

    # Find videos
    path = Path(args.path)
    if path.is_dir():
        videos = sorted(path.glob("**/video_*.mp4"))[:args.limit]
    elif path.is_file():
        videos = [path]
    else:
        print(f"❌ path not found: {args.path}")
        sys.exit(1)

    if not videos:
        print(f"❌ no video_*.mp4 files found under {args.path}")
        sys.exit(1)

    print(f"\n📹 Found {len(videos)} video(s)")
    print("=" * 60)

    platforms = [args.platform] if args.platform else PLATFORMS

    for video in videos:
        print(f"\n🎬 {video.name}")
        print(f"   path: {video}")

        for platform in platforms:
            try:
                content = preview_one(str(video), platform=platform)
                title = content["title"]
                desc = content["description"][:120]
                tags = ", ".join(f"#{t}" for t in content["hashtags"][:5])

                print(f"\n   [{platform}]")
                print(f"     title ({len(title)} chars): {title!r}")
                print(f"     description: {desc!r}...")
                print(f"     tags: {tags}")
            except Exception as e:
                print(f"\n   [{platform}] ❌ {e}")

    print("\n" + "=" * 60)
    print("✅ Dry-run complete (nothing was published)")
    print("=" * 60)


if __name__ == "__main__":
    main()