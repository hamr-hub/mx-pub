# Themed curation pipeline

How a new batch of clips becomes `publish_queue.json` entries with a CONTENT
collection (collections group by SUBJECT across production dates — never by
date). All gates are **fail-closed: a duplicate post is worse than a miss.**

## Flow (order matters)

```
curations/<batch>.json        human-written spec (copy + collection per clip)
        │
        ▼  scripts/build_themed_queue.py <spec>            # dry-run default
        │  resolves metadata · md5 · server truth · copy rules
        ▼  --apply (writes queue, /tmp backup)
publish_queue.json
        │
        ▼  scripts/precreate_dy_mixes.py <spec>            # Douyin only
        │  ffmpeg cover frame + create named mixes BEFORE publishing
        ▼
batch_publish.py               publish-time dropdown attach (DY),
                               post-loop API reconcile (DY + KS)
```

Weixin has NO collection automation: bind 合集 by hand in the Wujie form when
the user asks; otherwise report it as a best-effort gap, never silently skip.

## Spec schema (`curations/*.json`)

```json
{
  "batch": "20260915_content_themed",
  "created": "2026-09-15",
  "images_root": "/Volumes/ssd/codespace/personal/images",
  "collections": {"奇幻视界": "intro text (used as DY mix intro, ≤200 chars)"},
  "videos": [
    {
      "path": "20260912/minimax/video_1.mp4",
      "collection": "奇幻视界",
      "title": "火星落日独行",
      "description": "……正文…… #火星 #宇航员",
      "hashtags": ["火星", "宇航员"]
    }
  ]
}
```

- `path` is relative to `images_root` for render-tree clips; series eps live
  under `series/<story>_20260826_*/ep_NNN/video.mp4` (batch_hint fixed at
  `20260826` — the two render batches per story are the same scene, never mix).
- `collection` is required and becomes the queue entry's `collection` field.
  Name ≤ 12 CJK chars (KS hard cap; validator rejects longer).
- Top-level `"derivative": true` flips the pipeline into derivative mode
  (see below); individual entries may also set `"derivative": true`.

## build_themed_queue gates (`scripts/curation.py`)

Every entry must pass ALL of:

1. **Metadata resolution** — path exists AND its topic is found in
   `all_providers.csv` (daily) or `series.json` (series). Clips without topic
   metadata are unverifiable → rejected.
2. **md5** — unique inside the batch; with `--md5-index <file>` (`md5 -r`
   output from the images tree) also unique against every historical render.
3. **Server truth** — live title sets from all three targets BEFORE building:
   - douyin: paged `work_list` in-page fetch (see SKILL.md verify section);
   - kuaishou: unrecorded works (paged to exhaustion) + every collection's
     members, so works already inside a 合集 count as live;
   - weixin: no list API — titles come from stable-batch `ok` rows in the
     CURRENT queue file (`load_weixin_titles`; only trustworthy because
     batch_publish marks ok on real post_create errcode 0).
   - Browser-level CDP first; on "Browser context management is not
     supported" it automatically falls back to per-target raw CDP
     (`scripts/core/raw_cdp.py`, used when another automation client owns the
     browser-level websocket).
4. **Topic signature** — first 7 distinctive English tokens (cinematic
   vocabulary like `slow/motion/cinematic/drone/8k` is stop-worded). Already
   live when ≥4 of the first 6 probe tokens appear IN ORDER in any live title
   on ANY platform. Order matters ("tiger chasing fisherman" ≠ "fisherman
   chasing tiger"); the probe head still matches old ~33-char truncated
   titles. Same signature re-rendered on different days = duplicate: only the
   NEWEST render survives.
5. **CJK title collision** — 4+ CJK-char spine of the title must not be a
   substring of any live title's spine (either direction).
6. **Copy rules** — collection present & ≤12 CJK; title 6–14 CJK chars (WX
   短标题 window — note WX later strips punctuation, so prefer CJK/letters/
   digits at authoring time); non-empty description; ≥1 hashtag.
7. **Series episodes** — same `(story, episode)` listed twice is rejected.

Dry-run exit code: `0` clean, `1` nothing accepted or any rejection. With
`--apply`: writes atomically (`.tmp` rename), backs the old queue to
`/tmp/queue_<batch>_<HHMMSS>.json`, exit `2` when it wrote but also rejected
something. Merge never clobbers: `ok|blocked` entries untouched; pending
duplicates get refreshed copy/collection but keep their attempt history.

## Derivatives (`scripts/make_derivatives.py`)

Re-edits of validated high-VV OWN works (the five topics that repeatedly hit
the ~3k distribution ceiling: Shibuya drift, bamboo samurai, tuna chef,
biolum reef, hummingbird). Re-uploading the same file is useless — these are
genuinely re-rendered cuts:

- **variant A** — single source, 1.30× slow + grade/vignette + fades + CJK
  hook title card via drawtext (PingFang); ~7.6 s output.
- **variant B** — two DIFFERENT renders of the same prompt joined by 0.8 s
  xfade with one unified grade/title (~11–14 s); same-md5 pair is skipped.
- libx264 re-encode, no audio; anything under 5 s is not emitted (account
  floor); outputs in `artifacts/derivatives/out/` (git-ignored).
- Emits a `derivative: true` curation spec. In that mode build_themed_queue
  skips gates 1 (csv metadata), 3-signature and 7 (topic is DELIBERATELY
  revisited; absolute paths outside the images tree); md5, CJK title
  collision and copy rules still apply. Queue entries carry
  `derivative: true` + `sources[]` provenance.

Sources are listed in `artifacts/derivatives/src/sources.json` (downloaded
from KS `unrecorded/list` playUrls). PLANS (copy, ids, collection per topic)
lives at the top of `make_derivatives.py` — edit there for new topics.

## Tests

`tests/test_curation.py` (10 tests): normalization, stop-word tokens,
signature order, truncated-title match, unrelated-topic rejection, per-platform
cleanliness, newest-render selection. Run after touching curation code:

```bash
python3 -m pytest tests/test_curation.py -q
```
