"""Per-platform analytics scraper.

Navigates to each platform's 数据中心 / 数据看板 page and extracts the
aggregate numbers (published count, total views, likes, comments, shares,
followers, etc.) into a JSON file under ``./_benchmark/stats/``.

Usage
-----
    python scripts/stats.py --platform douyin            # one platform
    python scripts/stats.py --platform all               # all configured
    python scripts/stats.py --platform douyin --days 7   # custom window (if UI supports)

Output
------
    _benchmark/stats/douyin_2026-08-25.json
    {
      "platform": "douyin",
      "captured_at": "2026-08-25T13:45:00",
      "metrics": {
        "published_count": 42,
        "total_views": 1234567,
        "total_likes": 9876,
        ...
      },
      "raw_snapshot_excerpt": "..."
    }

Each platform config in ``platforms.json`` may include an ``analytics`` block:
    "analytics": {
      "url": "https://...data-overview",
      "url_aliases": ["..."],
      "fields": [
        {"key": "published_count", "match": "作品数", "scope": "page"},
        {"key": "total_views",      "match": "总播放", "scope": "page"}
      ]
    }

If a platform has no analytics block, stats.py logs a warning and skips.
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
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from webbridge_client import WebBridge, WebBridgeError  # noqa: E402

DEFAULT_PLATFORMS_JSON = HERE / "platforms.json"
DEFAULT_STATE_DB = HERE / "publish_state.db"
OUTPUT_ROOT = HERE.parent / "_benchmark" / "stats"


def load_platforms(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_publish_counts(db_path: Path) -> dict[str, dict[str, int]]:
    """Read scripts/publish_state.db to get per-platform publish counts."""
    import sqlite3
    if not db_path.exists():
        return {}
    counts: dict[str, dict[str, int]] = {}
    con = sqlite3.connect(str(db_path))
    cur = con.execute(
        "SELECT platform, status, COUNT(*) FROM publishes GROUP BY platform, status"
    )
    for platform, status, n in cur.fetchall():
        counts.setdefault(platform, {})[status] = n
    con.close()
    return counts


# ---------------------------------------------------------------------------
# Generic field extractor: walks the page innerText and matches by keywords.
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*[kKmMwW%]?")


def parse_number(s: str) -> float | None:
    """Parse '1,234' / '12.3w' / '87%' → float (with suffix scaling)."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = re.match(r"^(-?\d+\.?\d*)([kKmMwW%])?$", s)
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2)
    if suffix in ("k", "K"):
        val *= 1_000
    elif suffix in ("m", "M"):
        val *= 1_000_000
    elif suffix in ("w", "W"):
        val *= 10_000
    return val


def extract_field(page_text: str, keyword: str) -> str | None:
    """Find a number that follows ``keyword`` in the page text (within ±80 chars)."""
    idx = page_text.find(keyword)
    if idx < 0:
        return None
    window = page_text[max(0, idx - 5): idx + 80]
    nums = NUMBER_RE.findall(window)
    if not nums:
        return None
    # skip the keyword itself if it accidentally looks numeric
    for n in nums:
        if n.replace(",", "").replace(".", "").isdigit() or re.search(r"[kKmMwW%]", n):
            return n
    return nums[0] if nums else None


# ---------------------------------------------------------------------------
# platform-specific navigators
# ---------------------------------------------------------------------------


def goto_analytics(wb: WebBridge, cfg: dict) -> tuple[str, str]:
    """Find or navigate to the analytics page; return (url, page_text)."""
    analytics = cfg.get("analytics") or {}
    candidates = [analytics.get("url")] + analytics.get("url_aliases", [])
    candidates = [u for u in candidates if u]
    cfg_url = cfg.get("publish_url")
    if cfg_url:
        candidates.append(cfg_url)

    # try to reuse an open session tab first
    own = wb.list_tabs()
    for u in candidates:
        if not u:
            continue
        root = u.split("?")[0]
        for t in own:
            if t.get("url", "").startswith(root):
                wb.find_tab(t["url"], active=False)
                break

    # last resort: navigate fresh
    target = candidates[0] if candidates else cfg.get("publish_url", "about:blank")
    wb.navigate(target, new_tab=True, group_title=f"stats-{cfg.get('_key','?')}")

    # grab body text
    try:
        body = wb.evaluate("document.body.innerText").get("value", "")
    except WebBridgeError:
        body = ""
    cur_url = ""
    try:
        cur_url = wb.evaluate("location.href").get("value", "")
    except WebBridgeError:
        pass
    return cur_url, body


# ---------------------------------------------------------------------------
# platform-specific click-through (for SPAs that hide data behind menus)
# ---------------------------------------------------------------------------


def click_through(wb: WebBridge, cfg: dict) -> None:
    """If config declares ``analytics.entry_clicks``, click each in order.

    Used when the analytics page is reached via menu navigation rather than a
    direct URL (e.g. 视频号 hides 数据中心 behind a left sidebar).
    """
    analytics = cfg.get("analytics") or {}
    for sel in analytics.get("entry_clicks", []):
        try:
            wb.evaluate(
                f"var el=Array.from(document.querySelectorAll('*')).find(e=>e.innerText==='{sel}'&&e.children.length===0);"
                f"if(el)el.click();'clicked={sel} result='+(!!el)"
            ).get("value", "")
            wb.wait_for(seconds=1.0)
        except WebBridgeError:
            pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def collect_one(wb: WebBridge, key: str, cfg: dict, publish_counts: dict) -> dict[str, Any]:
    print(f"\n=== [{key}] {cfg.get('display_name', key)} ===")
    analytics = cfg.get("analytics")
    if not analytics:
        print(f"  · no analytics block in config — skipping")
        return {"platform": key, "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
                "skipped": True}

    url, body = goto_analytics(wb, cfg)
    click_through(wb, cfg)
    # refresh body after clicks
    try:
        body = wb.evaluate("document.body.innerText").get("value", "")
    except WebBridgeError:
        pass

    fields = analytics.get("fields", [])
    metrics: dict[str, Any] = {}
    matched: dict[str, str] = {}
    for f in fields:
        k = f["key"]
        kw = f["match"]
        raw = extract_field(body, kw)
        if raw is None:
            metrics[k] = None
            continue
        matched[k] = raw
        if f.get("as") == "str":
            metrics[k] = raw
        else:
            metrics[k] = parse_number(raw)

    # augment with publish counts from state DB
    pstats = publish_counts.get(key, {})
    metrics["publish_attempts_total"] = sum(pstats.values())
    metrics["publish_attempts_ok"] = pstats.get("ok", 0)
    metrics["publish_attempts_failed"] = pstats.get("failed", 0)
    metrics["publish_attempts_skipped"] = pstats.get("skipped", 0)
    metrics["publish_attempts_dry_run"] = pstats.get("dry_run", 0)

    payload = {
        "platform": key,
        "display_name": cfg.get("display_name", key),
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "page_url": url,
        "metrics": metrics,
        "matched_raw": matched,
        "page_text_excerpt": body[:1500],
    }
    print(f"  · page={url}")
    for k, v in metrics.items():
        print(f"    {k}: {v}")
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", default="all")
    ap.add_argument("--platforms-json", default=str(DEFAULT_PLATFORMS_JSON))
    ap.add_argument("--state-db", default=str(DEFAULT_STATE_DB))
    ap.add_argument("--out-dir", default=str(OUTPUT_ROOT))
    ap.add_argument("--session", default=None)
    args = ap.parse_args(argv)

    cfgs = load_platforms(Path(args.platforms_json))
    platforms = list(cfgs.keys()) if args.platform == "all" else [args.platform]
    for p in platforms:
        if p not in cfgs:
            ap.error(f"unknown platform: {p}; known: {list(cfgs)}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    wb = WebBridge(session=args.session)
    counts = load_publish_counts(Path(args.state_db))

    today = dt.date.today().isoformat()
    for key in platforms:
        cfg = cfgs[key]
        cfg["_key"] = key
        try:
            payload = collect_one(wb, key, cfg, counts)
        except WebBridgeError as e:
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
