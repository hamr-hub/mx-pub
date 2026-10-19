#!/usr/bin/env python3
"""Content curation primitives for the publishing service.

Responsibilities:
- md5 content index of the images tree (exact-duplicate detection)
- topic metadata readers (all_providers.csv daily runs + series.json stories)
- live-server truth: title sets already posted on douyin/kuaishou/weixin
- token-signature matcher (robust against the platforms' truncated titles)
- deterministic never-posted selection (newest render per unique signature)

Design notes:
- A duplicate post is worse than a missing one: when unsure whether content
  already went live, exclude it. Callers verify from SERVER truth, never from
  local click history (old fast_mode runs clicked without confirmation).
- Same prompt re-rendered on different days is treated as duplicate content:
  signatures collapse, only the newest render survives selection.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Title normalization / token signatures
# ---------------------------------------------------------------------------

# Generic cinematic-direction vocabulary. These words describe HOW a shot was
# made and recur across unrelated prompts; they must not anchor a signature.
STOP_WORDS = set(["a", "an", "the", "of", "in", "on", "at", "over", "through", "into", "toward", "towards", "with", "and", "or", "his", "her", "their", "them", "this", "that", "these", "those", "it", "its", "as", "by", "from", "up", "out", "off", "slow", "motion", "cinematic", "shot", "shots", "close", "camera", "view", "first", "person", "second", "long", "lens", "wide", "track", "tracking", "crane", "dolly", "zoom", "whip", "pan", "time", "lapse", "timelapse", "macro", "orbit", "drone", "aerial", "handheld", "hand", "held", "gimbal", "photo", "photorealistic", "photography", "continuous", "reveal", "push", "pull", "back", "away", "across", "is", "are", "was", "were", "be", "been", "they", "she", "he", "we", "you", "i", "very", "low", "high", "big", "small", "old", "new", "during", "while", "after", "before", "under", "above", "below", "around", "behind", "front", "side", "professional", "high", "detail", "sharp", "focus", "smooth", "8k", "4k", "24fps", "120fps", "quality", "composition", "museum"])

_CJK_RE = re.compile(r"[^a-z0-9一-鿿]+")

# CJK has no spaces, so words are found with character bigrams — the standard
# approach for Chinese without a segmenter. Without this the whole curation
# layer was inert: every Chinese title produced an empty token list, so
# `signature()` returned "" for all of them and select_unpublished() rejected
# the entire corpus as "empty signature".
_CJK_RUN = re.compile(r"[一-鿿]+")
_ASCII_RUN = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, split on non-alnum/CJK, collapse whitespace."""
    text = (text or "").lower()
    return re.sub(r"\s+", " ", _CJK_RE.sub(" ", text)).strip()


def content_tokens(text: str) -> list[str]:
    """Distinctive ordered tokens: ASCII words + CJK character bigrams.

    ASCII runs keep the original rule (len>=4, minus the cinematic-direction
    stop words). CJK runs become overlapping character bigrams, which capture
    the subject matter ("深山瀑布" -> 深山/山瀑/瀑布) without needing a
    word segmenter. Tokens stay in text order so `signature()` reflects the
    topic, not an alphabetical accident.
    """
    text = (text or "").lower()
    tokens: list[str] = []
    pos = 0
    for m in re.finditer(r"[a-z0-9]+|[一-鿿]+", text):
        run = m.group(0)
        if m.start() < pos:                     # never happens, cheap guard
            continue
        pos = m.end()
        if _ASCII_RUN.fullmatch(run):
            if len(run) >= 4 and run not in STOP_WORDS:
                tokens.append(run)
        else:
            tokens.extend(_cjk_bigrams(run))
    return tokens


def _cjk_bigrams(run: str) -> list[str]:
    """Overlapping character bigrams; a 1-char run yields itself."""
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def signature(text: str, n: int = 7) -> str:
    """First n distinctive tokens — identity of a prompt's SUBJECT matter."""
    return " ".join(content_tokens(text)[:n])


def is_live_topic(topic: str, live_titles: list[str], *,
                  min_hits: int = 4, probe: int = 6) -> Optional[str]:
    """Return the matching live title when topic was already posted.

    Matches when >= min_hits of the topic's first `probe` distinctive tokens
    appear IN ORDER in a live title. Order matters so that "tiger chasing
    fisherman" does not match "fisherman chasing tiger". Truncated platform
    titles (old runs posted ~33-char heads) still hit because the probe takes
    the first few distinctive words, which fit inside those heads.
    """
    want = content_tokens(topic)[:probe]
    if not want:
        return None
    need = min(min_hits, len(want))
    for title in live_titles:
        seen = content_tokens(title)
        if not seen:
            continue
        i = 0
        hits = 0
        for word in seen:
            if i < len(want) and word == want[i]:
                hits += 1
                i += 1
        if hits >= need:
            return title
    return None

# ---------------------------------------------------------------------------
# Filesystem content index
# ---------------------------------------------------------------------------


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class Clip:
    path: Path
    day: str            # 20260914 / series:<story>
    name: str
    topic: str          # English prompt / topic seed (empty when unknown)
    size: int = 0
    md5: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_series(self) -> bool:
        return self.day.startswith("series:")


def md5_index(root: Path) -> dict[str, list[Path]]:
    """Hash -> paths for every real mp4 under root (skips ._ AppleDouble)."""
    index: dict[str, list[Path]] = {}
    for path in root.rglob("*.mp4"):
        if path.name.startswith("._") or "_benchmark" in path.parts:
            continue
        index.setdefault(md5_of(path), []).append(path)
    return index


# Renders are either the legacy video_N.mp4 tree or provider/version-prefixed
# batches such as minimax-h3's pro_video_NN.mp4 (tracked in prompts.csv) and
# the mmx_h3 rerun series v2_video_NN.mp4 (tracked in v2_prompts.csv).
RENDER_NAME_RE = r"(?:[a-z0-9]+_)?video_\d+\.mp4"


def read_daily_topics(day_dir: Path) -> dict[str, str]:
    """video_N.mp4 -> topic_seed from all_providers.csv / prompts.csv.

    Failed rows carry an empty file_path but keep their index + topic_seed;
    fall back to video_<index>.mp4 so renders that landed despite a reported
    failure still get metadata. prompts.csv (mmx_h3 layout) carries the
    English prompt in a `prompt` column instead of `topic_seed`.
    """
    topics: dict[str, str] = {}
    manifests = [
        ("all_providers.csv", "topic_seed"),
        ("prompts.csv", "prompt"),
        # versioned rerun manifests (v2_prompts.csv, v4_prompts.csv, ...) live
        # beside the renders they produced.
        ("*_prompts.csv", "prompt"),
    ]
    for manifest, seed_col in manifests:
        for csv_path in day_dir.glob(f"**/{manifest}"):
            # AppleDouble stubs (._foo.csv) are binary and would blow up the
            # csv reader; _benchmark holds throwaway fixtures.
            if csv_path.name.startswith("._") or "_benchmark" in csv_path.parts:
                continue
            with open(csv_path, newline="", encoding="utf-8-sig",
                      errors="replace") as f:
                for row in csv.DictReader(f):
                    if row.get("kind") != "video" or not row.get(seed_col):
                        continue
                    base = os.path.basename(row.get("file_path") or "")
                    if re.fullmatch(RENDER_NAME_RE, base):
                        topics.setdefault(base, row[seed_col])
                    elif row.get("index", "").isdigit():
                        topics.setdefault(f"video_{row['index']}.mp4",
                                          row[seed_col])
    return topics


def daily_clips(root: Path) -> list[Clip]:
    """All renders in YYYYMMDD / YYYY-MM-DD day directories.

    Covers both layouts: legacy video_N.mp4 and provider batches
    (pro_video_NN.mp4 under e.g. 20261014/mmx_h3).
    """
    clips: list[Clip] = []
    for day_dir in sorted(p for p in root.iterdir() if p.is_dir()
                         and re.fullmatch(r"2026[-]?\d{4}", p.name)):
        topics = read_daily_topics(day_dir)
        renders = list(day_dir.glob("**/video_*.mp4"))
        renders += list(day_dir.glob("**/*_video_*.mp4"))
        for mp4 in sorted(set(renders)):
            if mp4.name.startswith("._"):
                continue
            clips.append(Clip(path=mp4, day=day_dir.name.replace("-", ""),
                              name=mp4.name, topic=topics.get(mp4.name, ""),
                              size=mp4.stat().st_size, md5=md5_of(mp4)))
    return clips


def series_clips(root: Path, *, batch_hint: str = "20260826") -> list[Clip]:
    """Episodic story clips under root/series/<story>_<batch>/ep_NNN/video.mp4.

    Two batches exist per story (20260826 = 2 episodes, 20260831 = re-rendered
    episode 1). Content is the same scene across batches, so callers pick one
    batch via batch_hint and never mix them.
    """
    series_root = root / "series"
    if not series_root.is_dir():
        return []
    clips: list[Clip] = []
    for story_dir in sorted(p for p in series_root.iterdir() if p.is_dir()):
        meta_path = story_dir / "series.json"
        episodes: dict[int, str] = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            for ep in meta.get("episodes", []):
                idx = ep.get("index")
                if isinstance(idx, int):
                    episodes[idx] = ep.get("prompt", "")
        if batch_hint and batch_hint not in story_dir.name:
            continue
        for ep_dir in sorted(story_dir.glob("ep_*")):
            mp4 = ep_dir / "video.mp4"
            if not mp4.exists():
                continue
            m = re.fullmatch(r"ep_(\d+)", ep_dir.name)
            idx = int(m.group(1)) if m else 0
            story = re.sub(r"_\d{8}_\d+$", "", story_dir.name)
            clips.append(Clip(path=mp4, day=f"series:{story}",
                              name=f"{ep_dir.name}.mp4",
                              topic=episodes.get(idx, ""),
                              size=mp4.stat().st_size, md5=md5_of(mp4),
                              extra={"story": story, "episode": idx,
                                     "batch": story_dir.name}))
    return clips

# ---------------------------------------------------------------------------
# Server truth (CDP in-page fetch; cookie auth)
# ---------------------------------------------------------------------------


DY_WORK_LIST = ("/janus/douyin/creator/pc/work_list?status=0&count=20"
                "&scene=star_atlas&device_platform=android&aid=1128")
KS_API = "https://cp.kuaishou.com/rest/cp/works/v2"
KS_UNRECORDED = "/collection/unrecorded/list/v2"
KS_LIST = "/collection/list"
KS_PHOTO_INFO = "/collection/photo/info"


def _douyin_titles(page, *, max_pages: int = 20) -> list[str]:
    """All published work heads (item_title/desc before first '#')."""
    titles: list[str] = []
    cursor = 0
    for _ in range(max_pages):
        body = page.evaluate("""async (path) => {
          const r = await fetch(path, {credentials:'include',
            headers:{'accept':'application/json'}});
          return await r.json();
        }""", f"{DY_WORK_LIST}&max_cursor={cursor}")
        rows = body.get("aweme_list") or []
        if not rows:
            break
        for work in rows:
            head = (work.get("item_title") or work.get("desc") or "")
            titles.append(head.split("#")[0].strip())
        if not body.get("has_more"):
            break
        cursor = body.get("max_cursor") or 0
        if not cursor:
            break
    return titles


def _kuaishou_titles(page) -> list[str]:
    """Unrecorded works (paged to exhaustion) + every collection's members."""
    def ks_post(path: str, payload: dict) -> dict:
        return page.evaluate("""async ({url, body}) => {
          const r = await fetch(url, {method:'POST', credentials:'include',
            headers:{'content-type':'application/json'},
            body: JSON.stringify(body)});
          return await r.json();
        }""", {"url": KS_API + path, "body": payload})

    if "cp.kuaishou.com" not in page.url:
        page.goto("https://cp.kuaishou.com/article/manage/video",
                  wait_until="domcontentloaded", timeout=30000)

    titles: list[str] = []
    cursor = None
    for _ in range(30):
        payload = {"limit": 20}
        if cursor:
            payload["cursor"] = cursor
        data = (ks_post(KS_UNRECORDED, payload).get("data") or {})
        rows = data.get("list") or []
        titles.extend(r.get("title") or "" for r in rows)
        cursor = data.get("nextCursor")
        if not cursor or not rows:
            break

    colls = ((ks_post(KS_LIST, {"count": 20, "page": 1,
                                "showOnProfileFilter": 0,
                                "publishStatus": 0, "type": "0"})
              .get("data") or {}).get("list") or [])
    for coll in colls:
        cid = coll.get("collectionId")
        info = ks_post("/collection/info",
                       {"collectionId": cid, "tubeId": str(cid)})
        ids = ((info.get("data") or {}).get("photoIdList")) or []
        for i in range(0, len(ids), 50):
            resp = ks_post(KS_PHOTO_INFO, {"photoIdList": ids[i:i + 50]})
            data = resp.get("data")
            rows = data if isinstance(data, list) else (
                (data or {}).get("list") or (data or {}).get("photoList") or [])
            titles.extend(r.get("title") or "" for r in rows)
    return titles


def _platform_page(ctx, host: str, url: str, timeout_ms: int):
    """Return a tab on `host`, opening one at `url` when none is open.

    A missing tab used to yield an empty title set, which is indistinguishable
    from "this account has published nothing" — the duplicate gate then
    approved everything and the batch double-posted. Navigate instead: the
    fetchers run same-origin `fetch()` and need the page to be on the host.
    """
    page = next((p for p in ctx.pages if host in p.url), None)
    if page is None:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    return page


def live_titles(cdp_url: str = "http://127.0.0.1:9222",
                timeout_ms: int = 60000) -> dict[str, list[str]]:
    """Connect to the logged-in Chrome and return live title sets per platform.

    Weixin has no readable list API without anti-bot tokens; callers pass the
    stable-batch confirmed titles separately (see load_weixin_titles).
    """
    from playwright.sync_api import sync_playwright

    targets = (
        ("douyin", "creator.douyin.com",
         "https://creator.douyin.com/creator-micro/content/manage",
         _douyin_titles),
        ("kuaishou", "cp.kuaishou.com",
         "https://cp.kuaishou.com/article/manage/video",
         _kuaishou_titles),
    )

    out: dict[str, list[str]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
        ctx = browser.contexts[0]
        for key, host, url, fetch in targets:
            page = _platform_page(ctx, host, url, timeout_ms)
            try:
                out[key] = fetch(page)
            except Exception as e:
                # Fail LOUD: an empty set here silently disables the
                # duplicate gate that is the whole point of this call.
                raise RuntimeError(
                    f"server truth for {key} failed; refusing to hand back an "
                    f"empty title set: {str(e)[:150]}") from e
    return out


class _RawPage:
    """Adapt core.raw_cdp.PageSession to the Playwright evaluate(js, arg)
    calling convention used by the platform title fetchers."""

    def __init__(self, session, url: str):
        self._s = session
        self.url = url

    def evaluate(self, js: str, arg=None):
        if arg is None:
            expression = f"({js})()"
        else:
            arg_json = json.dumps(arg)
            expression = (
                "(async (argJson) => { const arg = JSON.parse(argJson);"
                f" const fn = {js}; return await fn(arg);"
                "})(" + json.dumps(arg_json) + ")")
        return self._s.evaluate(expression)

    def goto(self, url: str, **_kw):
        self._s.goto(url)
        self.url = url


def live_titles_raw(cdp_http: str = "http://127.0.0.1:9222"
                    ) -> dict[str, list[str]]:
    """Server truth over per-target websockets (no browser-level session).

    Use when connect_over_cdp fails with 'Browser context management is not
    supported' because another automation client owns the browser endpoint.
    """
    from core.raw_cdp import attach

    out: dict[str, list[str]] = {}
    try:
        session = attach(
            "creator.douyin.com",
            ensure_url="https://creator.douyin.com/creator-micro/content/manage")
        try:
            page = _RawPage(session, session.evaluate("() => location.href"))
            out["douyin"] = _douyin_titles(page)
        finally:
            session.close()
    except Exception as e:
        print(f"[truth] douyin fetch failed: {str(e)[:150]}")
        out["douyin"] = []
    try:
        session = attach(
            "cp.kuaishou.com",
            ensure_url="https://cp.kuaishou.com/article/manage/video")
        try:
            page = _RawPage(session, session.evaluate("() => location.href"))
            out["kuaishou"] = _kuaishou_titles(page)
        finally:
            session.close()
    except Exception as e:
        print(f"[truth] kuaishou fetch failed: {str(e)[:150]}")
        out["kuaishou"] = []
    return out


def load_weixin_titles(queue_path: Path) -> list[str]:
    """Weixin truth source: stable-batch 'ok' titles in the local queue file.

    Only trusted because batch_publish marks ok on real post_create errcode 0;
    never feed this from old fast_mode records.
    """
    queue = json.loads(queue_path.read_text())
    return [v.get("title", "") for v in queue.get("videos", [])
            if isinstance(v.get("published", {}).get("weixin"), dict)
            and v["published"]["weixin"].get("status") == "ok"]

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_unpublished(clips: list[Clip], live: dict[str, list[str]],
                       *, platforms: tuple = ("douyin", "kuaishou", "weixin")
                       ) -> tuple[list[Clip], list[tuple[Clip, str, str]]]:
    """Split clips into (clean, exclusions).

    Exclusion entry: (clip, platform, matched_live_title).
    - md5-identical content collapses first (caller passes deduped clips).
    - Unknown topic (no metadata) is excluded as unverifiable.
    - Same signature keeps only the NEWEST day's render.
    """
    rejected: list[tuple[Clip, str, str]] = []
    newest: dict[str, Clip] = {}
    for clip in clips:
        if not clip.topic:
            rejected.append((clip, "metadata", "topic unknown"))
            continue
        live_hit = False
        for platform in platforms:
            match = is_live_topic(clip.topic, live.get(platform, []))
            if match:
                rejected.append((clip, platform, match))
                live_hit = True
                break
        if live_hit:
            continue
        sig = signature(clip.topic)
        if not sig:
            rejected.append((clip, "metadata", "empty signature"))
            continue
        if sig in newest:
            if clip.day > newest[sig].day:
                # replace and record the older render as duplicate
                older = newest[sig]
                rejected.append((older, "duplicate-topic", clip.path.name))
                newest[sig] = clip
            else:
                rejected.append((clip, "duplicate-topic", newest[sig].path.name))
        else:
            newest[sig] = clip
    return list(newest.values()), rejected
