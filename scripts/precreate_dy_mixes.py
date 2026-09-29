#!/usr/bin/env python3
"""Pre-create Douyin content mixes (合集) for a themed curation spec.

Douyin mixes require a cover image at creation; the KS API path needs none,
so this script exists only for douyin. Each mix cover is a frame grabbed
from the first queue member of that collection (ffmpeg). Idempotent: mixes
that already exist (by exact mix_name) are left untouched and re-verified.

Run AFTER build_themed_queue.py --apply and BEFORE batch_publish.py so the
publish-page dropdown can attach every clip at publish time.

Usage:
  python3 scripts/precreate_dy_mixes.py curations/20260915_content_themed.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REPO = HERE.parent
QUEUE_FILE = REPO / "publish_queue.json"


def extract_cover(video_path: Path, out_path: Path) -> bool:
    """Grab one jpeg frame; try multiple offsets (series eps are only 2s).

    Fast input-seek first at several offsets; output-seek (decode from start)
    is the last resort for files whose index does not support input seek.
    """
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    attempts = [
        [ffmpeg, "-y", "-ss", "1", "-i", str(video_path),
         "-frames:v", "1", "-q:v", "3", str(out_path)],
        [ffmpeg, "-y", "-ss", "0.5", "-i", str(video_path),
         "-frames:v", "1", "-q:v", "3", str(out_path)],
        [ffmpeg, "-y", "-i", str(video_path), "-ss", "0.5",
         "-frames:v", "1", "-q:v", "3", str(out_path)],
    ]
    last_err = ""
    for cmd in attempts:
        if out_path.exists():
            out_path.unlink()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True
        last_err = proc.stderr[-200:]
    print(f"[cover] ffmpeg failed for {video_path.name}: {last_err}")
    return False


def first_member_paths(queue: dict, names: list[str]) -> dict[str, Path]:
    """First queue video (spec order) per collection, only when it exists."""
    out: dict[str, Path] = {}
    for video in queue.get("videos", []):
        coll = video.get("collection")
        if coll in names and coll not in out:
            path = Path(video["path"])
            if path.exists():
                out[coll] = path
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", type=Path)
    ap.add_argument("--queue", type=Path, default=QUEUE_FILE)
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    args = ap.parse_args()

    spec = json.loads(args.spec.read_text())
    names = list(dict.fromkeys(v["collection"] for v in spec["videos"]))
    intros = spec.get("collections", {})
    queue = json.loads(args.queue.read_text())
    members = first_member_paths(queue, names)
    missing = [n for n in names if n not in members]
    if missing:
        print(f"❌ spec collections without a queued member file: {missing}")
        return 1

    from playwright.sync_api import sync_playwright
    from mx_collections import (
        dy_mix_id_by_name,
        get_or_create_douyin_collection,
    )

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dy_covers_") as tmpdir, \
            sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp_url, timeout=60000)
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "creator.douyin.com" in p.url), None)
        if page is None:
            page = ctx.new_page()

        for name in names:
            try:
                existing = dy_mix_id_by_name(page, name)
            except Exception as e:
                print(f"⚠️  list mixes failed for '{name}': {str(e)[:120]}")
                existing = None
            if existing:
                print(f"✅ mix '{name}' exists (mix_id={existing})")
                continue

            cover = Path(tmpdir) / f"{name}.jpg"
            if not extract_cover(members[name], cover):
                failures.append(name)
                continue
            print(f"➕ creating mix '{name}' (cover from "
                  f"{members[name].parent.name}/{members[name].name})…")
            ref = get_or_create_douyin_collection(
                page, name=name, cover_path=str(cover),
                intro=intros.get(name, "")[:200])
            time.sleep(2)
            confirmed = None
            try:
                confirmed = dy_mix_id_by_name(page, name)
            except Exception as e:
                print(f"⚠️  re-list failed: {str(e)[:120]}")
            if confirmed:
                print(f"✅ mix '{name}' created (mix_id={confirmed})")
            else:
                print(f"❌ mix '{name}' NOT confirmed in mix list "
                      f"(helper ref={ref})")
                failures.append(name)

    if failures:
        print(f"\n❌ unconfirmed mixes: {failures}")
        return 2
    print("\n🎬 all content mixes ready for publish-time attach")
    return 0


if __name__ == "__main__":
    sys.exit(main())
