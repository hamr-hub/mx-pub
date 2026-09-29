# 合集 (collections) — model + API cookbook

Two collection systems, reconciled automatically by `batch_publish.py` after
each platform's loop (run-start day pinned; KS/DY threads connect even with
zero pending; best-effort, fatal CDP errors re-raise). Helpers:
`scripts/mx_collections.py`. State: `collections_state.json`.

## Content collections vs daily fallback

- Queue entries with an explicit **`collection`** field go to that CONTENT
  collection — grouped by SUBJECT across all production dates
  (`group_ok_works()` in `batch_publish.py`). Themed DY reconcile runs with
  `day_start_ts=0` (full work-history match), so re-running a day later still
  repairs a missed attach.
- Entries without `collection` (legacy) fall back to the run-day collection:
  DY `AI合集_YYYYMMDD`, KS `AI合集MMDD`, and only that day's `ok` works count.
- **Douyin content mixes must PRE-EXIST** — run `precreate_dy_mixes.py`
  before the batch (see below). Post-loop reconcile SKIPS a missing mix
  (`'mix' not found; skip reconcile`); it never auto-creates. The publish
  page dropdown can only attach mixes that already exist.
- **Kuaishou** collections are created on demand by the API path (no cover
  needed) — no precreate step.
- Only run-day `ok` titles feed the daily fallback; themed groups pass every
  `ok` work carrying that collection regardless of date.

## Douyin — API-direct in-page `fetch` (cookie auth, `status_code:0` = ok)

- Mix list:
  `GET /web/api/mix/list/?count=12&cursor=0&status=0,1,2,3,6`
  → `{mix_list:[{mix_id,mix_name,status,...}],has_more,total}`
- Members:
  `GET /web/api/mix/item/list/?mix_id=..&count=50&cursor=..`
  → `{aweme_list,has_more,cursor,status_code}` (schema is `aweme_list`).
- **COMMIT:** `POST /web/api/mix/item/editlist/`,
  `application/x-www-form-urlencoded`, body
  `mix_id=<id>&item_ids=<FULL ordered comma list, commas %2C>&add_item_ids=<new comma list>`
  — it REPLACES membership, so always send the union; empty add list = no
  POST at all (idempotent no-op verified live: 20 members, 0 POSTs).
- Reconcile resolves ids from `work_list` (themed: day-filter off, prefix
  match on the doubled-title desc — desc reads "标题 标题,雨夜 #话题"),
  unions, POSTs, then re-fetches members and verifies every added id is
  present (`added=X/Y total=Z verify=ok|FAIL`).
- Precreate (`scripts/precreate_dy_mixes.py`): idempotent by exact mix_name;
  cover = ffmpeg frame from the first queued member (tries 1 s / 0.5 s input
  seek then output seek — 2 s series eps need the fallbacks); intro from the
  spec's `collections` map (≤200 chars); creation confirmed by re-listing.
- Publish-time attach: the 合集 dropdown in `douyin.py` attaches the queued
  collection while posting; reconcile only repairs dropdown misses.
- UI mechanics (only if API path breaks): 添加作品 opens a Semi **SideSheet**
  (`.semi-sidesheet.item-drawer-*` + mask), rows `.drawer-item-*` plus
  control `.plus-area-*` — rows MUST be toggled with **trusted** Playwright
  clicks (JS `.click()` stages nothing); the sheet has no own footer, only
  the sticky header 保存 commits; its trusted click is mask-blocked, so
  commit with JS `btn.click()`.

## Kuaishou — API-direct in-page `fetch` on a cp.kuaishou.com tab

Cookie auth suffices; the `kuaishou.web.cp.api_ph` body field is optional, no
`__NS_sig3`. Never drive the `/article/manage/collection` UI (trusted clicks
are drawer-intercepted; the whole UI flow was retired after live
verification). Endpoints: all `POST https://cp.kuaishou.com/rest/cp/works/v2/...`,
envelope `result==1`.

- `collection/list` `{count:20,page:1,showOnProfileFilter:0,publishStatus:0,type:"0"}`
  — use count **20** (50 returns 参数传值有误); includes empty collections.
- `collection/add` `{title,type:"",showType:1,photoIdList:[],showStatusType:1}`;
  server may answer collectionId:0 → re-list to confirm. Read lags create by
  a few seconds: helper polls 5×1.5 s re-list.
- `collection/update` `{...,type:30,photoIdList:[...]}` **REPLACES the full
  list** — always `collection/info` first and union existing ids.
- `collection/info` `{collectionId,tubeId:str(id)}`; member title reads via
  `collection/photo/info` (chunk 50); unrecorded works via
  `collection/unrecorded/list/v2` `{limit:20,cursor?}` paged by `nextCursor`.
- Post-update verification polls 4×1.5 s (membership reads lag the write).
- **ID duality:** works list uses base36 `workId`; collection APIs use
  12-digit numeric `photoId` (e.g. 208946344748). Membership is EXCLUSIVE:
  a work already in another collection returns `available:false` with
  filterReason "已添加到其它合集" → report blocked, never retry.
- Title cap 12 chars; normalize to `isalnum()` chars for title matching
  (`ks_caption_key` / `_strip_embedded_hashtags` in `kuaishou.py`): the live
  caption STARTS WITH THE DESCRIPTION, not the queue title — KS has no title
  field, and publishing strips the embedded `#tag` tail then inserts smart
  topics. Matching on the short title silently attaches nothing.
- Hard topic cap: a caption with >4 topics total fails submit. Queue
  descriptions carry embedded `#tag`s; `_fill_form` strips them and inserts
  at most 4 smart topics.

## Operational notes

- Helpers are idempotent: members matched by normalized title count as
  already-attached; verify via re-fetched info after update
  (`already=20 newly_added=0/20 blocked=[]`).
- Before any manual/parallel collection run, back the queue up
  (`/tmp/queue_<phase>.json`); two batch processes clobber each other's
  in-memory queue snapshots.
- AI-generated content declaration on Douyin is set ONLY at publish time and
  can't be added via collection APIs or retroactively — report gaps, never
  republish to fix.
