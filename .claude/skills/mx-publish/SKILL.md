---
name: mx-publish
description: Publish the video queue (publish_queue.json) to 抖音 / 快手 / 微信视频号 through the real logged-in Chrome via CDP. Use whenever the user asks to 发布/上传/publish videos, build a themed/curation batch, make 二创 derivative cuts, precreate Douyin 合集, resume a batch, retry failures, verify posts from server truth, or reconcile publish collections. Covers fail-closed curation gates, content-themed 合集, concurrent stable batch runs, and reconciliation.
---

# mx-publish — stable multi-platform video publishing

## Rule zero

- **小红书 (XHS) is NEVER included in batch automation** (account-ban / AI-detection risk).
  It is force-skipped in `batch_publish.py`; only run it after an explicit per-task
  user whitelist, by itself, slowly.
- **Clips shorter than 5 s are NEVER posted** (hard account rule, 2026-09-15).
  `core/media.py` ffprobe-preflights every clip and marks `blocked`
  (`duration_x.xxs_below_min_5s`) before any upload; unknown duration fails OPEN.
  Weixin silently rejects <3 s uploads (old runs burned 2×420 s timeouts there).
- These are **real accounts**. Every "ok" must mean a real server-confirmed post.
  When unsure whether something posted, verify from server truth before retrying —
  never guess (a duplicate post is worse than a missing one).
- **No other automation client on CDP 9222 during a batch.** A second Playwright
  client (e.g. an IDE playwright-mcp) competes for `Page.handleJavaScriptDialog`;
  the loser dies with an uncatchable node rejection and the whole platform
  thread's transport goes down. Check for stray clients before launching.

## Architecture (do not re-derive)

- One real Chrome with `--remote-debugging-port=9222`, logged in on all platforms.
  Playwright connects over CDP and reuses existing tabs. `scripts/core/environment.py`
  launches Chrome if absent.
- `batch_publish.py`: one CDP connection + one reused tab **per platform**, platforms
  run concurrently in threads, each queue processed sequentially. Signal-driven:
  upload-finish API → fill form → real publish API response (with DOM fallback).
- `scripts/core/dialog_guard.py`: must stay attached on EVERY platform page
  (`attach_dialog_guard(page)` in `_setup_page`). Native beforeunload panels
  crash a CDP node driver via the multi-client accept race (every Playwright
  client in the Chrome receives the dialog; the loser of
  `Page.handleJavaScriptDialog` dies with an uncatchable node rejection, taking
  the transport down). Four layers, all required: neutralize alert/confirm/
  prompt; patch `EventTarget.addEventListener` + `window.addEventListener` to
  drop `beforeunload`/`unload`; trap `Object.defineProperty` AND install a
  noop accessor on `onbeforeunload`/`onunload` (Douyin's tea/security bundles
  ASSIGN `window.onbeforeunload` directly ~50×/load — blocking
  addEventListener alone leaked panels); a 1s sweeper deletes any own-prop
  assignment that slipped through.
- Dead transport must fail FAST, not burn a 420s timeout per video:
  `is_fatal_connection_error(e)` in `core/watch.py` (Connection/Target/Browser/
  Session closed markers). All signal waits (`wait_url` heartbeat every 3s,
  `wait_dom`, `await_publish`) and platform upload/fill handlers re-raise on
  it; `batch_publish.py` then aborts the remaining videos for that platform
  (`fatal_connection_closed`) instead of retrying through a dead driver.
- ApiWatch captures POSTs; `MX_DEBUG=1` logs every captured POST.
- Status file `publish_queue.json`: `videos[].published.<platform>` with
  `status ∈ ok|partial|fail|blocked`; done = `ok|blocked`. `partial` = clicked but
  no server confirmation → verified-not-live ⇒ safe to retry.
- **Collections follow CONTENT, not date.** Entries carrying a `collection`
  field are grouped by subject across production dates (`group_ok_works()`):
  KS content collections are created/updated API-direct post-loop; DY content
  mixes attach from the publish dropdown and reconcile over full history
  (`day_start_ts=0`) and therefore MUST be pre-created by
  `scripts/precreate_dy_mixes.py` before publishing. Legacy entries with no
  `collection` field keep the run-day fallback (`AI合集_YYYYMMDD` /
  `AI合集MMDD`). Full cookbook: `references/collections-api.md`.

### Platform quirks (verified 2026-09)

| | 抖音 douyin | 快手 kuaishou | 视频号 weixin |
|---|---|---|---|
| Host | creator.douyin.com (Semi UI) | cp.kuaishou.com (ant-ish) | channels.weixin.qq.com **Wujie** |
| Title | title field + desc | **no title field**, description only | **短标题 6–14 chars required** |
| Real submit | item/aweme create, must carry item_id/aweme_id, status_code 0 | `POST /rest/cp/works/v2/video/pc/submit`, result/status==1 | `POST .../mmfinderassistant-bin/post/post_create`, errCode 0 + data.baseResp.errcode 0 |
| Success page text | 作品发布成功 | 发布成功/已发布/审核中 or /article/manage | /post/list or 已发表 |
| First-clip cost | **~7 min** (upload + platform transcode) | seconds–3 min | seconds–3 min |
| Extras | AI-content declaration ON; content 合集 via dropdown (mix pre-created) | #topics hard cap **4**, embedded `#tag`s stripped; content 合集 API post-loop | #话题/@, 合集 bind by hand only, 定时发表, 活动, 视频标注 |
| Noise to ignore | config/entrance endpoints | realize/entrance, publish/time | helper_mmdata, hepler_merlin_*, helper_report, report-perf, search_collection, post_clip_video_result |

**Weixin Wujie (critical):** `<iframe name=content>` is only the JS sandbox
(`/empty.html`, 0×0) — never query it. The UI lives in `<wujie-app>`'s **open
shadow root**. Playwright CSS locators pierce it (`page.locator("wujie-app ...")`);
raw JS needs `document.querySelector('wujie-app').shadowRoot`. Mounts in ~5 s on
direct goto `/platform/post/create`. The 发表 button click MUST be a real Playwright
locator click (auto-scrolls the nested scroll container, trusted events); JS
`.click()` is silently ignored and raw `mouse.click(x,y)` misses (button at
y≈1260 outside viewport). If its class contains `weui-desktop-btn_disabled`,
usually the title fails the ≥6-char rule (`标题至少6个字`) — `_build_short_title()`
extends with description tokens. It also strips punctuation first: a middle
dot etc. (`圣像修复师·凝眸`) left 发表 disabled with NO validation toast —
author queue titles from CJK/letters/digits only.

## Themed curation batch (the normal path for new clips)

Do NOT hand-append clips to the queue. Author a spec and run the fail-closed
gates — they verify against three-platform server truth before anything posts:

```bash
# 1. Author curations/<batch>.json (schema + every gate documented in
#    references/curation-pipeline.md): path, collection, title(6–14 CJK),
#    description, hashtags per clip.
# 2. Build (dry-run by default; exits nonzero on ANY rejection)
python3 scripts/build_themed_queue.py curations/<batch>.json --apply

# 3. Douyin ONLY: pre-create content mixes with ffmpeg cover frames
#    (idempotent; KS needs no precreate; run BEFORE publishing or the
#     publish dropdown has nothing to attach and post-loop skips missing mixes)
python3 scripts/precreate_dy_mixes.py curations/<batch>.json

# 4. Publish as usual (see Standard run)
```

二创 derivative cuts (re-edited own high-VV works): `make_derivatives.py`
renders slow-cut/xfade variants (≥5 s, re-encoded) and EMITS a
`derivative: true` spec; then continue at step 2. Topic-live and csv-metadata
gates are deliberately bypassed for derivatives; md5 + CJK-title-collision +
copy gates still apply. Details: `references/curation-pipeline.md`.

## Standard run

```bash
# 0. Preflight
curl -s http://127.0.0.1:9222/json/version | head -c 200    # CDP up
# Confirm no human is mid-edit in a publish tab; close stale publish forms.
# Confirm no OTHER playwright/automation client is attached to 9222.

# 1. (Re)build queue from generated videos + prompts.csv — legacy fallback only;
#    themed batches go through build_themed_queue.py (see above)
python3 build_publish_queue.py

# 2. Publish (background for long runs; douyin ~7 min/clip dominates)
ONLY_PLATFORMS=douyin,kuaishou,weixin MX_DEBUG=0 \
  nohup python3 -u batch_publish.py > /tmp/batch_$(date +%H%M).log 2>&1 &
echo $! > /tmp/batch.pid          # ALWAYS track the PID
tail -f /tmp/batch_*.log
```

Useful env: `ONLY_PLATFORMS`, `SKIP_PLATFORMS` (xhs already forced),
`LIMIT_PER_PLATFORM=n`, `RETRY_FAILED=1`, `CDP_URL`.

### Operational rules

- **Kill by exact PID (`kill $(cat /tmp/batch.pid)`) — never `pkill -f batch_publish.py`**;
  an anchored pattern kills every platform batch sharing that argv.
- Avoid two batch processes at once: each rewrites the whole queue file from its
  in-memory snapshot and clobbers other platforms' updates. If unavoidable
  (e.g. restarting one platform while another runs): back up the file first,
  then reconcile from server truth at the end. Don't republish from clobbered
  statuses.
- After editing any `scripts/**.py`, run `python3 -m py_compile` on it, and
  `python3 -m pytest tests/test_curation.py -q` after curation edits.
  For JS injected via Page.evaluate: extract and `node --check` — note JS has NO
  adjacent-string-literal concatenation (`'a' 'b'` is a SyntaxError; use `'a'+'b'`).

## Verify from server truth

- **Weixin** — on the channels tab:
  `POST /micro/content/cgi-bin/mmfinderassistant-bin/post/post_list`
  body `{"pageSize":30,"pageNum":0}`; title at `item.desc.description`
  (may be a JSON-encoded string). Compare today's titles; no duplicates.
  Offline, the trusted substitute is stable-batch `ok` rows in the queue
  (`curation.load_weixin_titles`); never use old fast_mode records.
- **Kuaishou** — open `https://cp.kuaishou.com/article/manage/video`, read
  `共N个作品` and each row's title + `已发布 YYYY-MM-DD HH:MM`. Also check
  collection membership via the collection APIs (works INSIDE any 合集 count
  as live — curation's truth fetch unions unrecorded + all collection members).
- **Douyin** — in-page `fetch` on creator.douyin.com (cookie auth; the
  response listener can miss this call, that's why in-page fetch is used):
  `GET /janus/douyin/creator/pc/work_list?status=0&count=20&max_cursor=<ts>&scene=star_atlas&device_platform=android&aid=1128`
  → top-level `{status_code:0,total,has_more,max_cursor,aweme_list}`. Row:
  `aweme_id, item_title, desc, create_time, status_value(102=published)`,
  `status.{in_reviewing,is_prohibited,is_delete}`, `risk_infos.content`
  (`"作者声明：内容由 AI 生成"` = AIGC declared). **desc doubles the title**
  ("标题 标题,雨夜 #话题") → match by `item_title`/desc-before-`#` prefix,
  never exact equality. Page newest-first via max_cursor timestamp. Mix
  truth: `GET /web/api/mix/item/list/?mix_id=..&count=50&cursor=0` →
  `{aweme_list,cursor,has_more,status_code}` (schema is `aweme_list`).
- **Browser-level CDP blocked** ("Browser context management is not
  supported", another automation client owns the browser endpoint):
  `curation.live_titles_raw()` / `scripts/core/raw_cdp.py` attaches
  per-target `/devtools/page/<id>` websockets for read-only in-page fetch —
  no browser-level session, no dialog race.

## Reconcile & retry

1. After batches finish, recount `publish_queue.json` per platform and compare
   with server truth; merge truth into the file (`ok|blocked` are done).
2. `partial`/`fail` rows stay pending automatically; rerun that platform only.
   `blocked` rows (e.g. <5 s floor) stay done — never force-retry without user.
3. Collection reconcile converges every run: themed groups across full
   history, daily fallback for run-day legacy clips. Missing DY mix → precreate
   then rerun the reconcile (batch loop or the helpers directly).
4. Weixin partials historically meant either <6-char title or a swallowed click —
   both fixed in `weixin.py` (plus punctuation strip); check log lines
   `[weixin] publish click:` and `button still disabled:` for any remaining cause.
5. Backups: copy the queue to `/tmp/queue_<phase>.json` before any
   parallel/restart run (build_themed_queue `--apply` does this automatically).

## Extra features (use them — the goal is maximal feature utilization)

- Topics/hashtags on every platform (queue `hashtags` / deterministic fallback).
  KS hard-caps at 4 total: embedded `#tag`s are stripped from the description
  and at most 4 smart topics inserted.
- **Collections** (content-themed primary, daily fallback): reconciled
  automatically by `batch_publish.py` after each platform's loop. Endpoints,
  union/editlist semantics, ID duality, cover precreate and UI-fallback
  mechanics all live in **`references/collections-api.md`**.
  Weixin collection binding is manual only (`添加到合集`/`创建新合集`,
  title immutable) — report it as a best-effort gap.
- Other Weixin features: `定时发表`, `活动`, `视频标注`, #话题/@视频号.
  带货商品 is disabled on this account.
- AI-generated content: Douyin declaration is set at publish time and **cannot
  be added retroactively** — report works missing it, never republish to fix
  (a duplicate is worse than a gap; 2026-09-14 batch: 18/20 declared). Consider
  Weixin 视频标注 for future queues.

## Key files

```
batch_publish.py                 stable concurrent batch driver (entrypoint)
build_publish_queue.py           legacy queue rebuild from videos + prompts.csv
publish_queue.json               state (git-untracked); per-video per-platform status
curations/<batch>.json           human-authored themed/derivative batch specs
scripts/curation.py              md5 index, topic signatures, 3-platform server truth
scripts/build_themed_queue.py    fail-closed spec -> queue gates (dry-run default)
scripts/precreate_dy_mixes.py    pre-publish Douyin mix creation (ffmpeg covers)
scripts/make_derivatives.py      二创 re-cuts -> derivative curation spec
scripts/platforms/{douyin,kuaishou,weixin,xhs}.py
scripts/mx_collections.py        Douyin/Kuaishou collection helpers
scripts/core/watch.py            ApiWatch: POST capture, patterns, DOM waits
scripts/core/dialog_guard.py     beforeunload/dialog suppression
scripts/core/environment.py      Chrome discovery/launch
scripts/core/media.py            ffprobe ≥5s preflight (blocked before upload)
scripts/core/raw_cdp.py          per-target CDP truth fetch when browser ws is owned
tests/test_curation.py           curation gate unit tests
references/curation-pipeline.md  spec schema, gates, derivatives (read when curating)
references/collections-api.md    DY/KS 合集 endpoints and semantics
```

Retired/legacy (moved to `archive/2026-09/`, do not use): `loop_publish_v2.py`
(old one-video-at-a-time orchestrator; `MX_PUB_NORMAL_MODE`/fast_mode lived
there), `collections_driver.py` (superseded by auto-attach in
`batch_publish.py` + API helpers in `mx_collections.py`), `run_picks.sh`,
`scripts/platforms/_features.py` orphan, one-off
`*_0831*`/`build_5/10_queue.py`/`ks_phase3.py` scripts. `entrypoint.sh` and
README point at `batch_publish.py`.

## ICM (project-mandated)

Recall before starting (`icm recall "publish" -t context-mx-pub` etc.), and store
immediately: resolved errors → `errors-resolved`, design decisions →
`decisions-mx-pub`, completions → `context-mx-pub`.
