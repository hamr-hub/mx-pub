#!/usr/bin/env python3
"""Phase 3 only: create missing kuaishou collections + add videos by title match."""
import json
import re
import sys
import time
from pathlib import Path
from collections import defaultdict
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from mx_collections import (
    get_or_create_kuaishou_collection,
    add_videos_to_kuaishou_collection,
)

QUEUE = HERE / "publish_queue.json"


def norm(d):
    return d if re.fullmatch(r"\d{8}", d) else d.replace("-", "")


def main():
    queue = json.loads(QUEUE.read_text())
    by_date = defaultdict(list)
    for v in queue["videos"]:
        if "20260902" <= norm(v["date"]) <= "20260911":
            by_date[v["date"]].append(v)

    print(f"Dates: {sorted(by_date)}")
    for d, vs in sorted(by_date.items()):
        print(f"  {d}: {len(vs)} videos")

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
        ctx = b.contexts[0]

        # Phase 1: create missing kuaishou collections
        for date in sorted(by_date):
            cname = f"AI合集_KS_{date}"
            print(f"\n[1.create] {cname}")
            try:
                fresh = ctx.new_page()
                ref = get_or_create_kuaishou_collection(fresh, name=cname)
                print(f"  -> ref={ref}")
                try: fresh.close()
                except Exception: pass
            except Exception as e:
                print(f"  ⚠️ err: {e}")

        # Phase 2: add videos to each collection
        for date in sorted(by_date):
            cname = f"AI合集_KS_{date}"
            titles = [v["title"] for v in by_date[date]]
            print(f"\n[2.add] {cname}: {len(titles)} videos")
            try:
                fresh = ctx.new_page()
                ok = add_videos_to_kuaishou_collection(
                    fresh, collection_name=cname, video_titles=titles,
                )
                print(f"  -> ok={ok}")
                try: fresh.close()
                except Exception: pass
            except Exception as e:
                print(f"  ⚠️ err: {e}")

    print("\n✅ done")


if __name__ == "__main__":
    main()