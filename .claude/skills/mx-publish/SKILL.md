---
name: mx-publish
description: Publish the video queue (publish_queue.json) to 抖音 / 快手 / 微信视频号 through the real logged-in Chrome via CDP. Use whenever the user asks to 发布/上传/publish videos, resume a batch, retry failures, verify posts, or manage publish collections. Covers queue building, concurrent stable batch runs, server-truth verification, and reconciliation.
---

# mx-publish — stable multi-platform video publishing

## Rule zero

- **小红书 (XHS) is NEVER included in batch automation** (account-ban / AI-detection risk).
  It is force-skipped in `batch_publish.py`; only run it after an explicit per-task
  user whitelist, by itself, slowly.
- These are **real accounts**. Every "ok" must mean a real server-confirmed post.
  When unsure whether something posted, verify from server truth before retrying —
  never guess (a duplicate post is worse than a missing one).

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

### Platform quirks (verified 2026-09)

| | 抖音 douyin | 快手 kuaishou | 视频号 weixin |
|---|---|---|---|
| Host | creator.douyin.com (Semi UI) | cp.kuaishou.com (ant-ish) | channels.weixin.qq.com **Wujie** |
| Title | title field + desc | **no title field**, description only | **短标题 6–14 chars required** |
| Real submit | item/aweme create, must carry item_id/aweme_id, status_code 0 | `POST /rest/cp/works/v2/video/pc/submit`, result/status==1 | `POST .../mmfinderassistant-bin/post/post_create`, errCode 0 + data.baseResp.errcode 0 |
| Success page text | 作品发布成功 | 发布成功/已发布/审核中 or /article/manage | /post/list or 已发表 |
| First-clip cost | **~7 min** (upload + platform transcode) | seconds–3 min | seconds–3 min |
| Extras | AI-content declaration ON; auto-attached to daily `AI合集_YYYYMMDD` | #topics auto-recognized; per-date collection added post-publish | #话题/@, 合集 bind, 定时发表, 活动, 视频标注 |
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
extends with description tokens.

## Standard run

```bash
# 0. Preflight
curl -s http://127.0.0.1:9222/json/version | head -c 200    # CDP up
# Confirm no human is mid-edit in a publish tab; close stale publish forms.

# 1. (Re)build queue from generated videos + prompts.csv — only if new clips exist
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
- After editing any `scripts/**.py`, run `python3 -m py_compile` on it.
  For JS injected via Page.evaluate: extract and `node --check` — note JS has NO
  adjacent-string-literal concatenation (`'a' 'b'` is a SyntaxError; use `'a'+'b'`).

## Verify from server truth

- **Weixin** — on the channels tab:
  `POST /micro/content/cgi-bin/mmfinderassistant-bin/post/post_list`
  body `{"pageSize":30,"pageNum":0}`; title at `item.desc.description`
  (may be a JSON-encoded string). Compare today's titles; no duplicates.
- **Kuaishou** — open `https://cp.kuaishou.com/article/manage/video`, read
  `共N个作品` and each row's title + `已发布 YYYY-MM-DD HH:MM`.
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

## Reconcile & retry

1. After batches finish, recount `publish_queue.json` per platform and compare
   with server truth; merge truth into the file (`ok|blocked` are done).
2. `partial`/`fail` rows stay pending automatically; rerun that platform only.
3. Weixin partials historically meant either <6-char title or a swallowed click —
   both fixed in `weixin.py`; check log lines `[weixin] publish click:` and
   `button still disabled:` for the remaining cause.
4. Backups: copy the queue to `/tmp/queue_<phase>.json` before any parallel/restart run.

## Extra features (use them — the goal is maximal feature utilization)

- Topics/hashtags on every platform (queue `hashtags` / deterministic fallback).
- **Collections:** `scripts/mx_collections.py`, both reconciled **automatically
  by `batch_publish.py` after each platform's loop** (run-start day pinned so a
  batch crossing midnight keeps one consistent day; KS/DY threads connect even
  with zero pending to reconcile; best-effort, fatal CDP errors re-raised;
  only run-day `ok` titles are passed):
  - **Douyin** — publish-time dropdown attaches in `douyin.py`
    (`AI合集_YYYYMMDD`), and `reconcile_douyin_collection` repairs misses after
    the loop via API-direct in-page `fetch`. Endpoints (all cookie-auth,
    `status_code:0` = ok):
    - `GET /web/api/mix/list/?count=12&cursor=0&status=0,1,2,3,6`
      → `{mix_list:[{mix_id,mix_name,status,...}],has_more,total}`.
    - `GET /web/api/mix/item/list/?mix_id=..&count=50&cursor=..` →
      `{aweme_list,has_more,cursor,status_code}`.
    - **COMMIT:** `POST /web/api/mix/item/editlist/`,
      `application/x-www-form-urlencoded`, body
      `mix_id=<id>&item_ids=<FULL ordered comma list, commas %2C>&add_item_ids=<new comma list>`
      — it REPLACES membership, so always send the union; empty add list = no
      POST at all (idempotent no-op verified live: 20 members, 0 POSTs).
    - Reconcile resolves ids from work_list (day-filtered, prefix match on the
      doubled-title desc), unions, POSTs, then re-fetches members and verifies
      every added id is present (`added=X/Y total=Z verify=ok|FAIL`).
    - UI mechanics (only needed if API path breaks): 添加作品 opens a Semi
      **SideSheet** (`.semi-sidesheet.item-drawer-*` + mask), rows
      `.drawer-item-*`, plus control `.plus-area-*` — rows MUST be toggled
      with **trusted** Playwright clicks (JS `.click()` stages nothing); the
      sheet has no own footer, only the sticky header 保存 commits; its trusted
      click is mask-blocked, so commit with JS `btn.click()`.
  - **Kuaishou** — `attach_kuaishou_daily_collection` (`AI合集MMDD`),
    **API-direct via
  in-page `fetch` on a cp.kuaishou.com tab** (cookie auth; the
  `kuaishou.web.cp.api_ph` body field is optional, no __NS_sig3). Never drive
  the `/article/manage/collection` UI (trusted clicks are drawer-intercepted;
  the whole flow was replaced after live verification). KS endpoints (all
  `POST /rest/cp/works/v2/...`, envelope `result==1`):
  - `collection/list` body `{count:20,page:1,showOnProfileFilter:0,publishStatus:0,type:"0"}`
    — use count **20** (50 returns 参数传值有误); includes empty collections.
  - `collection/add` `{title,type:"",showType:1,photoIdList:[],showStatusType:1}`;
    server may answer collectionId:0 → re-list to confirm.
  - `collection/update` `{...,type:30,photoIdList:[...]}` **REPLACES the full
    list** — fetch `collection/info`, union existing ids first, never write a
    bare new list.
  - `collection/info` `{collectionId,tubeId:str(id)}`; `collection/photo/info`
    (chunk 50) for member titles; `collection/unrecorded/list/v2`
    `{limit:20,cursor?}` paged via `nextCursor`.
  - **ID duality:** works list uses base36 `workId`; collection APIs use
    12-digit numeric `photoId` (e.g. 208946344748). Membership is EXCLUSIVE:
    a work in another collection comes back `available:false` with filterReason
    "已添加到其它合集" → report blocked, do not retry.
  - Title capped at 12 chars; daily name `AI合集MMDD`. The add helper is
    idempotent: members matched by normalized title (`isalnum()` chars) count
    as already-attached; verify via re-fetched info count after update
    (`already=20 newly_added=0/20 blocked=[]`).
  State in `collections_state.json`.
- Weixin: `添加到合集`/`创建新合集` (title immutable), `定时发表`, `活动`,
  `视频标注`, #话题/@视频号. 带货商品 is disabled on this account.
- AI-generated content: Douyin declaration is set at publish time and **cannot
  be added retroactively** — report works missing it, never republish to fix
  (a duplicate is worse than a gap; 2026-09-14 batch: 18/20 declared). Consider
  Weixin 视频标注 for future queues.

## Key files

```
batch_publish.py                 stable concurrent batch driver (entrypoint)
build_publish_queue.py           rebuild queue from videos + prompts.csv
publish_queue.json               state (git-untracked); per-video per-platform status
scripts/platforms/{douyin,kuaishou,weixin,xhs}.py
scripts/core/watch.py            ApiWatch: POST capture, patterns, DOM waits
scripts/core/dialog_guard.py     beforeunload/dialog suppression
scripts/core/environment.py      Chrome discovery/launch
scripts/mx_collections.py        Douyin/Kuaishou collection helpers
```

Retired/legacy (moved to `archive/2026-09/`, do not use): `loop_publish_v2.py`
(old one-video-at-a-time orchestrator), `collections_driver.py` (superseded by
auto-attach in `batch_publish.py` + API helpers in `mx_collections.py`),
`run_picks.sh`, `scripts/platforms/_features.py` orphan, one-off
`*_0831*`/`build_5/10_queue.py`/`ks_phase3.py` scripts. `entrypoint.sh` and
README point at `batch_publish.py`.

## ICM (project-mandated)

Recall before starting (`icm recall "publish" -t context-mx-pub` etc.), and store
immediately: resolved errors → `errors-resolved`, design decisions →
`decisions-mx-pub`, completions → `context-mx-pub`.
