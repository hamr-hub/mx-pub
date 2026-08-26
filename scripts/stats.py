"""Per-platform analytics scraper.

Connects to Chrome via CDP (port 9222), opens each platform's analytics
page in a new tab, scrapes aggregate numbers (published count, total
views, likes, comments, shares, followers), and writes one JSON per
platform to ``_benchmark/stats/<platform>_<date>.json``.

Uses Playwright CDP directly — does NOT depend on webbridge_client.

Usage
-----
    python scripts/stats.py --platform douyin          # one platform
    python scripts/stats.py --platform all             # all configured
    python scripts/stats.py --platform douyin,kuaishou  # comma list

Output
------
    _benchmark/stats/douyin_2026-08-26.json
    {
      "platform": "douyin",
      "captured_at": "2026-08-26T11:30:00",
      "page_url": "https://creator.douyin.com/creator-micro/data-overview",
      "metrics": {"published_count": 42, "total_views": 1234567, ...},
      "matched_raw": {"published_count": "42", ...},
      "page_text_excerpt": "..."
    }
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

DEFAULT_PLATFORMS_JSON = HERE / "platforms.json"
OUTPUT_ROOT = PROJECT_ROOT / "_benchmark" / "stats"
CDP_URL = "http://127.0.0.1:9222"

NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*[kKmMwW%]?")


def load_platforms(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def parse_number(s: str) -> float | None:
    """Parse '1,234' / '12.3w' / '87%' → float (suffix scaled)."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = re.match(r"^(-?\d+\.?\d*)([kKmMwW%])?$", s)
    if not m:
        return None
    val = float(m.group(1))
    suf = m.group(2)
    if suf in ("k", "K"):
        val *= 1_000
    elif suf in ("m", "M"):
        val *= 1_000_000
    elif suf in ("w", "W"):
        val *= 10_000
    return val


def extract_field(page_text: str, keyword: str) -> str | None:
    """Find the FIRST number that comes IMMEDIATELY after `keyword` (within 30 chars)."""
    idx = page_text.find(keyword)
    if idx < 0:
        return None
    # Look at the 30 chars AFTER the keyword
    after = page_text[idx + len(keyword): idx + len(keyword) + 30]
    # Skip leading whitespace, separator chars (·:：, 、)
    cleaned = re.sub(r"^[\s·:：,、()（）【】\[\]【]+", "", after)
    m = re.match(r"(-?\d[\d,]*\.?\d*[kKmMwW%]?)", cleaned)
    if m:
        return m.group(1)
    # Fallback: any number in next 80 chars
    nums = NUMBER_RE.findall(page_text[idx + len(keyword): idx + len(keyword) + 80])
    return nums[0] if nums else None


def collect_one(key: str, cfg: dict) -> dict[str, Any]:
    """Scrape one platform via CDP."""
    from playwright.sync_api import sync_playwright

    analytics = cfg.get("analytics") or {}
    if not analytics:
        return {"platform": key, "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
                "skipped": True}

    target = analytics.get("url")
    print(f"\n=== [{key}] {cfg.get('display_name', key)} → {target} ===")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        ctx = browser.contexts[0]

        # Reuse existing tab whose URL already starts with target root
        page = None
        if target:
            target_root = target.split("?")[0].rsplit("/", 1)[0]
            for t in ctx.pages:
                if t.url.startswith(target_root):
                    page = t
                    break

        if page is None:
            page = ctx.new_page()

        try:
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            return {"platform": key, "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "error": f"nav: {e}"}

        page.bring_to_front()
        # give SPA time to render
        page.wait_for_timeout(3000)

        # click through menu if needed (e.g. weixin 视频号)
        for sel in analytics.get("entry_clicks", []):
            try:
                page.evaluate(
                    f"var el=Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()==='{sel}'&&e.children.length===0);"
                    f"if(el)el.click();"
                )
                page.wait_for_timeout(1500)
            except Exception:
                pass

        try:
            body = page.evaluate("document.body.innerText")
        except Exception as e:
            return {"platform": key, "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "error": f"eval: {e}"}

        cur_url = page.url

    metrics: dict[str, Any] = {}
    matched_raw: dict[str, str] = {}
    for f in analytics.get("fields", []):
        k = f["key"]
        kw = f["match"]
        raw = extract_field(body, kw)
        if raw is None:
            metrics[k] = None
            continue
        matched_raw[k] = raw
        metrics[k] = parse_number(raw) if f.get("as") != "str" else raw

    payload = {
        "platform": key,
        "display_name": cfg.get("display_name", key),
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "page_url": cur_url,
        "metrics": metrics,
        "matched_raw": matched_raw,
        "page_text_excerpt": body[:1500],
    }
    print(f"  · page={cur_url}")
    for k, v in metrics.items():
        print(f"    {k}: {v}")
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scrape per-platform analytics via CDP")
    ap.add_argument("--platform", default="all")
    ap.add_argument("--platforms-json", default=str(DEFAULT_PLATFORMS_JSON))
    ap.add_argument("--out-dir", default=str(OUTPUT_ROOT))
    args = ap.parse_args(argv)

    cfgs = load_platforms(Path(args.platforms_json))
    if args.platform == "all":
        platforms = list(cfgs.keys())
    else:
        platforms = [p.strip() for p in args.platform.split(",")]
    for p in platforms:
        if p not in cfgs:
            ap.error(f"unknown platform: {p}; known: {list(cfgs)}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    today = dt.date.today().isoformat()
    for key in platforms:
        cfg = cfgs[key]
        try:
            payload = collect_one(key, cfg)
        except Exception as e:
            print(f"  ✗ {key}: {e}")
            payload = {"platform": key, "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "error": str(e)}
        out_path = out_dir / f"{key}_{today}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  · saved → {out_path}")

    print(f"\nWrote {len(platforms)} JSON files to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())