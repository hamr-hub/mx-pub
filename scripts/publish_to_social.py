"""Reusable workflow: publish historical AIGC video/image assets to
抖音 / 快手 / 小红书 via the Kimi WebBridge daemon (http://127.0.0.1:10086).

Usage examples
--------------

# Sanity check — daemon reachable + config loaded
python scripts/publish_to_social.py --health

# Dry-run one video to 抖音 (no click on 发布, no DB writes)
python scripts/publish_to_social.py --platform douyin \\
    --assets 20260819/minimax/video_1.mp4 \\
    --title-template "AIGC 创作 · {date}" \\
    --description-template "AI 生成作品 · {provider} · {filename}" \\
    --tags "AIGC,AI创作,AI绘画" \\
    --dry-run

# Real publish: one video per platform, sampled from --since
python scripts/publish_to_social.py --platform all \\
    --since 20260819 --limit 1 \\
    --title-template "AIGC 创作 · {date}" \\
    --tags "AIGC,AI创作" --yes

# Resume: skip assets already in state DB
python scripts/publish_to_social.py --platform douyin --since 20260820 --limit 5 --yes

The script is safe to interrupt (Ctrl-C): the SQLite state records each
publish attempt (success / failure / skip) so re-runs won't double-publish.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Allow `python scripts/publish_to_social.py` from repo root
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from webbridge_client import WebBridge, WebBridgeError, make_backend  # noqa: E402

PROJECT_ROOT = HERE.parent
DEFAULT_PLATFORMS_JSON = HERE / "platforms.json"
DEFAULT_STATE_DB = HERE / "publish_state.db"

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------------------
# asset scanning + templating
# ---------------------------------------------------------------------------


@dataclass
class Asset:
    path: Path
    sha1: str = ""

    @property
    def kind(self) -> str:
        return "video" if self.path.suffix.lower() in VIDEO_EXTS else "image"

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    def parse_date(self) -> Optional[dt.date]:
        """Extract YYYYMMDD from leading path segment like 20260819/minimax/..."""
        m = re.match(r"^(\d{8})", self.path.parts[0] if self.path.parts else "")
        if not m:
            return None
        try:
            return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            return None

    def derive_metadata(self, title_tmpl: str, desc_tmpl: str, default_tags: list[str]) -> dict:
        date = self.parse_date()
        return {
            "date": date.isoformat() if date else "",
            "filename": self.path.name,
            "provider": self.path.parent.name if len(self.path.parts) >= 2 else "",
            "kind": self.kind,
            "title": title_tmpl.format(**self._template_ctx(date)),
            "description": desc_tmpl.format(**self._template_ctx(date)),
            "tags": list(default_tags),
        }

    def _template_ctx(self, date: Optional[dt.date]) -> dict:
        return {
            "date": date.isoformat() if date else "",
            "yymmdd": date.strftime("%y%m%d") if date else "",
            "filename": self.path.stem,
            "ext": self.path.suffix.lstrip("."),
            "provider": self.path.parent.name,
            "kind": self.kind,
        }


def hash_file(path: Path, chunk: int = 1 << 16) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def scan_assets(
    roots: Iterable[Path],
    *,
    since: Optional[str] = None,
    kinds: tuple[str, ...] = ("video", "image"),
    extra_globs: Iterable[str] = (),
    limit: Optional[int] = None,
) -> list[Asset]:
    """Walk given roots; return sorted (oldest→newest) list of Asset.

    ``since`` is YYYYMMDD; assets whose first path segment is older are skipped.
    """
    since_date: Optional[dt.date] = None
    if since:
        since_date = dt.datetime.strptime(since, "%Y%m%d").date()

    wanted_exts = set()
    if "video" in kinds:
        wanted_exts |= VIDEO_EXTS
    if "image" in kinds:
        wanted_exts |= IMAGE_EXTS

    candidates: list[Asset] = []
    seen: set[str] = set()
    globs = list(extra_globs) or ["**/*"]

    for root in roots:
        if not root.exists():
            continue
        for glob in globs:
            for p in root.glob(glob):
                if not p.is_file() or p.suffix.lower() not in wanted_exts:
                    continue
                if ".git" in p.parts or "node_modules" in p.parts:
                    continue
                key = str(p.resolve())
                if key in seen:
                    continue
                seen.add(key)
                a = Asset(path=p)
                d = a.parse_date()
                if since_date and d and d < since_date:
                    continue
                candidates.append(a)

    candidates.sort(key=lambda a: (a.parse_date() or dt.date(1970, 1, 1), a.path.name))
    if limit:
        candidates = candidates[:limit]
    return candidates


# ---------------------------------------------------------------------------
# state (SQLite)
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS publishes (
    platform     TEXT NOT NULL,
    asset_sha1   TEXT NOT NULL,
    asset_path   TEXT NOT NULL,
    asset_size   INTEGER NOT NULL,
    status       TEXT NOT NULL,           -- ok | failed | skipped | dry_run
    note         TEXT,
    published_at TEXT NOT NULL,
    PRIMARY KEY (platform, asset_sha1)
);
CREATE INDEX IF NOT EXISTS idx_published_at ON publishes(published_at);
"""


class State:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def already_done(self, platform: str, sha1: str, *, status: str = "ok") -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM publishes WHERE platform=? AND asset_sha1=? AND status=?",
            (platform, sha1, status),
        )
        return cur.fetchone() is not None

    def record(self, platform: str, asset: Asset, *, status: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO publishes(platform,asset_sha1,asset_path,asset_size,status,note,published_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                platform,
                asset.sha1 or hash_file(asset.path),
                str(asset.path),
                asset.size_bytes,
                status,
                note,
                dt.datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def today_count(self, platform: str) -> int:
        today = dt.date.today().isoformat()
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM publishes WHERE platform=? AND status='ok' AND substr(published_at,1,10)=?",
            (platform, today),
        )
        return int(cur.fetchone()[0])

    def last_publish_at(self, platform: str) -> Optional[dt.datetime]:
        cur = self.conn.execute(
            "SELECT published_at FROM publishes WHERE platform=? AND status='ok' "
            "ORDER BY published_at DESC LIMIT 1",
            (platform,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dt.datetime.fromisoformat(row[0])

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# per-platform publisher
# ---------------------------------------------------------------------------


@dataclass
class PlatformCfg:
    raw: dict

    @property
    def name(self) -> str:
        return self.raw.get("display_name", self.name_key)

    @property
    def name_key(self) -> str:
        return self.raw.get("_key", "?")

    @property
    def url(self) -> str:
        return self.raw["publish_url"]

    @property
    def selectors(self) -> dict:
        return self.raw.get("selectors", {})

    @property
    def limits(self) -> dict:
        return self.raw.get("limits", {})

    @property
    def settle_seconds(self) -> int:
        return int(self.raw.get("upload_settle_seconds", 8))

    @property
    def publish_wait_text_gone(self) -> str:
        return self.raw.get("after_publish_text_gone", "上传中")

    @property
    def anti_spam(self) -> dict:
        return self.raw.get("anti_spam", {})


class Publisher:
    def __init__(self, key: str, cfg: PlatformCfg, wb, state: State,
                 *, dry_run: bool, log=print):
        self.key = key
        self.cfg = cfg
        self.wb = wb
        self.state = state
        self.dry_run = dry_run
        self.log = log
        self.backend_name = "cdp" if wb.__class__.__name__ == "WebBridgeCdp" else "webbridge"

    # ---- guards ---------------------------------------------------------

    def _check_quota(self) -> Optional[str]:
        a = self.cfg.anti_spam
        max_per_day = int(a.get("max_per_day", 999))
        used = self.state.today_count(self.key)
        if used >= max_per_day:
            return f"今日 {self.cfg.name} 已发 {used}/{max_per_day}，拒绝继续"
        min_interval = int(a.get("min_publish_interval_seconds", 0))
        last = self.state.last_publish_at(self.key)
        if last and min_interval:
            wait = (last + dt.timedelta(seconds=min_interval)) - dt.datetime.now()
            if wait.total_seconds() > 0:
                secs = int(wait.total_seconds()) + 1
                return f"距上次 {self.cfg.name} 发布 {secs}s，< {min_interval}s 节流"
        return None

    def _check_size(self, asset: Asset) -> Optional[str]:
        if asset.kind != "video":
            return None
        cap = int(self.cfg.limits.get("video_max_size_mb", 128))
        if asset.size_mb > cap:
            return f"video {asset.size_mb:.1f}MB 超过 {self.cfg.name} 上限 {cap}MB"
        return None

    # ---- main step ------------------------------------------------------

    def publish_one(self, asset: Asset, meta: dict) -> str:
        if not asset.sha1:
            asset.sha1 = hash_file(asset.path)

        if self.state.already_done(self.key, asset.sha1, status="ok"):
            self.log(f"  · skip 已发布: {asset.path}")
            return "skipped"

        # asset sanity
        if not asset.path.exists():
            note = f"missing on disk: {asset.path}"
            self.state.record(self.key, asset, status="failed", note=note)
            return "failed"
        if (msg := self._check_size(asset)):
            self.state.record(self.key, asset, status="failed", note=msg)
            self.log(f"  ✗ size check: {msg}")
            return "failed"
        if (msg := self._check_quota()):
            self.state.record(self.key, asset, status="failed", note=msg)
            self.log(f"  ✗ quota: {msg}")
            return "failed"

        self.log(f"→ {self.cfg.name} :: {asset.path.name} ({asset.size_mb:.1f}MB)")
        self.log(f"  标题: {meta['title']}")
        self.log(f"  标签: {','.join(meta['tags'])}")

        try:
            self._open_publish_tab()
            self._upload(asset)
            self._fill_metadata(meta)
            if self.dry_run:
                shot = HERE.parent / "_benchmark" / f"publish_{self.key}_{asset.path.stem}_dryrun.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.wb.screenshot(path=str(shot), format="png")
                except WebBridgeError:
                    pass
                self.log(f"  · dry-run — 已截图 {shot.name}, 未点击发布")
                self.state.record(self.key, asset, status="dry_run", note="dry-run")
                return "dry_run"

            self._click_publish()
            self.wb.wait_for(text_gone=self.cfg.publish_wait_text_gone, seconds=30)
            self.state.record(self.key, asset, status="ok")
            self.log(f"  ✓ 发布成功")
            return "ok"
        except WebBridgeError as e:
            self.state.record(self.key, asset, status="failed", note=str(e))
            self.log(f"  ✗ webbridge error: {e}")
            return "failed"
        except Exception as e:  # last-ditch — don't let one bad asset kill the loop
            self.state.record(self.key, asset, status="failed", note=f"unexpected: {e!r}")
            self.log(f"  ✗ unexpected: {e!r}")
            return "failed"

    # ---- DOM interactions ----------------------------------------------

    def _ensure_foreground_tab(self) -> None:
        """Make sure the user's foreground tab is on this platform's publish URL.

        With CDP backend, this just switches the session's current Page to the
        matching one (no foreground dance). With webbridge backend, prompts
        the user to switch tabs since ``evaluate`` only runs on foreground.
        """
        if self.backend_name == "cdp":
            target = self.cfg.url.split("?")[0]
            aliases = [a.split("?")[0] for a in (self.cfg.raw.get("publish_url_aliases") or [])]
            for u in [target] + aliases:
                hit = self.wb.find_tab(u, active=False)
                if hit:
                    return
            self.wb.navigate(self.cfg.url, new_tab=True, group_title=f"publish-{self.key}")
            return

        expected_roots = [self.cfg.url.split("?")[0]] + (self.cfg.raw.get("publish_url_aliases") or [])
        expected_roots = [r for r in expected_roots if r]
        try:
            cur = self.wb.evaluate("location.href").get("value", "")
        except WebBridgeError:
            cur = ""

        if any(cur.startswith(r) for r in expected_roots):
            return  # already on the right tab

        if os.environ.get("MX_PUB_AUTO_FOREGROUND") == "1":
            self.log(f"  · auto-foreground → {self.cfg.url}")
            self.wb.navigate(self.cfg.url, new_tab=False, group_title=f"publish-{self.key}")
            self.wb.wait_for(seconds=self.cfg.settle_seconds)
            return

        # print a friendly prompt and wait for the user
        self.log("")
        self.log(f"  ┌─ 需要你手动切 tab ─────────────────────────────────")
        self.log(f"  │  平台: {self.cfg.name} ({self.key})")
        self.log(f"  │  把以下任一 URL 对应的 tab 切到前台:")
        for r in expected_roots[:3]:
            self.log(f"  │    {r}")
        self.log(f"  │  切好后回到这里按回车继续…")
        self.log(f"  └────────────────────────────────────────────────────")
        try:
            input()
        except EOFError:
            pass
        # re-verify
        try:
            cur = self.wb.evaluate("location.href").get("value", "")
        except WebBridgeError:
            cur = ""
        if not any(cur.startswith(r) for r in expected_roots):
            raise WebBridgeError(
                f"foreground tab 仍是 {cur!r}，不是 {self.cfg.name}。请切到正确 tab 后重试。"
            )

    def _open_publish_tab(self) -> None:
        # Try to find an existing session tab first (cheap reuse).
        own = self.wb.list_tabs()
        for t in own:
            if t.get("url", "").startswith(self.cfg.url.split("?")[0]):
                self._ensure_foreground_tab()
                return
        self.log(f"  · navigate → {self.cfg.url}")
        self.wb.navigate(self.cfg.url, new_tab=True, group_title=f"publish-{self.key}")
        self._ensure_foreground_tab()

    def _upload(self, asset: Asset) -> None:
        sel = self.cfg.selectors.get("upload_input")
        if not sel:
            raise WebBridgeError(f"{self.key} 缺 upload_input selector")
        self.log(f"  · upload({asset.path.name})")
        self.wb.upload(sel, [str(asset.path)])
        self.wb.wait_for(seconds=self.cfg.settle_seconds)

    def _fill_metadata(self, meta: dict) -> None:
        s = self.cfg.selectors
        title = meta["title"][: int(self.cfg.limits.get("title_max_chars", 30))]
        desc = meta["description"][: int(self.cfg.limits.get("description_max_chars", 1000))]
        self.log(f"  · fill title ({len(title)} chars)")
        self.wb.fill(s["title_input"], title)
        self.log(f"  · fill desc ({len(desc)} chars)")
        self.wb.fill(s["description_input"], desc)
        for tag in meta["tags"][: int(self.cfg.limits.get("tags_max_count", 10))]:
            self.log(f"  · tag: {tag}")
            self.wb.fill(s["tag_input"], tag)
            self.wb.wait_for(seconds=0.6)
            # press Enter / click suggestion
            try:
                self.wb.click(s["tag_suggestion_first"])
            except WebBridgeError:
                pass
            self.wb.wait_for(seconds=0.3)

    def _click_publish(self) -> None:
        s = self.cfg.selectors.get("publish_button")
        if not s:
            raise WebBridgeError(f"{self.key} 缺 publish_button selector")
        self.log(f"  · click 发布")
        self.wb.click(s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_tags(s: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\s]+", s.strip()) if t.strip()]


def load_platforms(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for key, val in data.items():
        if key.startswith("_"):
            continue
        val["_key"] = key
        out[key] = val
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", required=False,
                    help="douyin | kuaishou | xiaohongshu | all (default: all)")
    ap.add_argument("--assets", help="comma-separated paths; if set, --since ignored")
    ap.add_argument("--assets-root", action="append", default=[str(PROJECT_ROOT)],
                    help=f"root to scan (default: {PROJECT_ROOT}); repeatable")
    ap.add_argument("--since", help="YYYYMMDD lower bound for date-segmented assets")
    ap.add_argument("--limit", type=int, default=1, help="per-platform cap (default: 1)")
    ap.add_argument("--kinds", default="video", help="video|image|both (default: video)")
    ap.add_argument("--title-template", default="AIGC 创作 · {date}",
                    help="supports {date} {filename} {provider} {kind}")
    ap.add_argument("--description-template",
                    default="AI 生成作品 · {provider} · {filename}\n#AIGC #AI创作")
    ap.add_argument("--tags", default="AIGC,AI创作,AI绘画",
                    help="comma/space separated")
    ap.add_argument("--platforms-json", default=str(DEFAULT_PLATFORMS_JSON))
    ap.add_argument("--state-db", default=str(DEFAULT_STATE_DB))
    ap.add_argument("--session", default=None, help="webbridge session id (default: publish-<ts>)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fill the form + screenshot, do NOT click 发布")
    ap.add_argument("--yes", action="store_true", help="skip interactive confirm")
    ap.add_argument("--auto-foreground", action="store_true",
                    help="navigate the user's foreground tab (set MX_PUB_AUTO_FOREGROUND=1)")
    ap.add_argument("--backend", choices=("auto", "cdp", "webbridge", "cdp-only"), default="auto",
                    help="auto = try CDP then fall back to webbridge (default); "
                         "cdp-only = require Playwright/CDP; webbridge = use kimi-webbridge only")
    ap.add_argument("--health", action="store_true", help="only print daemon + config status, exit 0")
    args = ap.parse_args(argv)

    if args.auto_foreground:
        os.environ["MX_PUB_AUTO_FOREGROUND"] = "1"

    if args.health:
        info = WebBridge(session=args.session or "publish-health").health()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        cfgs = load_platforms(Path(args.platforms_json))
        print(f"\nLoaded {len(cfgs)} platforms: {', '.join(cfgs.keys())}")
        for k, v in cfgs.items():
            sel = v.get("selectors", {})
            miss = [name for name in ("upload_input", "title_input", "description_input",
                                       "publish_button") if not sel.get(name)]
            tag = "OK" if not miss else f"missing selectors: {miss}"
            print(f"  · {k}: {tag}")
        return 0

    cfgs = load_platforms(Path(args.platforms_json))
    platforms = list(cfgs.keys()) if args.platform in (None, "all") else [args.platform]
    for p in platforms:
        if p not in cfgs:
            ap.error(f"unknown platform: {p}; known: {list(cfgs)}")

    # asset selection
    if args.assets:
        explicit = [Path(p.strip()) for p in args.assets.split(",") if p.strip()]
        assets = [Asset(path=p.resolve()) for p in explicit if p.exists()]
        if not assets:
            print("No matching assets on disk for --assets", file=sys.stderr)
            return 2
    else:
        kinds = ("video",) if args.kinds == "video" else \
                ("image",) if args.kinds == "image" else ("video", "image")
        assets = scan_assets(
            [Path(p) for p in args.assets_root],
            since=args.since,
            kinds=kinds,
            limit=args.limit * len(platforms),  # overall cap, sliced per platform below
        )
        if not assets:
            print("No assets matched (try --since YYYYMMDD or --assets-root)", file=sys.stderr)
            return 2

    default_tags = parse_tags(args.tags)

    print("=== Publish workflow ===")
    print(f"Platforms:    {platforms}")
    print(f"Assets:       {len(assets)} candidate(s)")
    print(f"Per platform: up to {args.limit}")
    print(f"Title tmpl:   {args.title_template}")
    print(f"Desc  tmpl:   {args.description_template}")
    print(f"Tags:         {default_tags}")
    print(f"Dry-run:      {args.dry_run}")
    print()

    if not args.yes and not args.dry_run:
        try:
            ans = input("Continue? [y/N] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower() != "y":
            print("Aborted.")
            return 1

    state = State(Path(args.state_db))
    prefer = "auto" if args.backend == "auto" else args.backend
    wb, backend_name = make_backend(prefer=prefer, session=args.session, verbose=True)

    summary = {}
    cursor = 0
    for pkey in platforms:
        cfg = PlatformCfg(raw=cfgs[pkey])
        pub = Publisher(pkey, cfg, wb, state, dry_run=args.dry_run)
        # slice assets fairly: round-robin
        slice_ = assets[cursor::len(platforms)][: args.limit]
        cursor += 1
        if not slice_:
            print(f"[{pkey}] no assets for this slice")
            continue
        results = []
        for a in slice_:
            meta = a.derive_metadata(args.title_template, args.description_template, default_tags)
            results.append(pub.publish_one(a, meta))
            # soft rate limit even between assets
            if not args.dry_run:
                interval = int(cfg.anti_spam.get("min_publish_interval_seconds", 0))
                if interval:
                    print(f"  · sleep {interval}s (anti-spam)")
                    time.sleep(interval)
        summary[pkey] = dict(zip([a.path.name for a in slice_], results))

    state.close()
    print("\n=== Summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
