"""Collection/Album helpers for Douyin + Kuaishou.

Douyin: native 合集 (mix). Create at /creator-micro/content/collection/create,
attach via "添加合集" on publish page. Post-publish reconcile is API-direct
(verified 2026-09): work_list (janus) for today's aweme ids, mix/list +
mix/item/list for membership, POST /web/api/mix/item/editlist/ urlencoded
(mix_id + full item_ids + add_item_ids; editlist REPLACES the list, union
first). See reconcile_douyin_daily_mix().

Kuaishou: 合集. Driven API-direct through the creator backend (verified 2026-09):
  POST /rest/cp/works/v2/collection/list   {count:20,page:1,type:0,...} -> all collections
  POST /rest/cp/works/v2/collection/add    {title,type:"",showType:1,photoIdList:[],
                                            showStatusType:1} -> data.collectionId
  POST /rest/cp/works/v2/collection/update {collectionId,tubeId,title,description,
                                            type:30,showType:1,photoIdList:[...]}
  POST /rest/cp/works/v2/collection/info   {collectionId,tubeId} -> full record
  POST /rest/cp/works/v2/collection/unrecorded/list/v2 {limit,cursor?}
       -> addable works: {photoId (numeric!),title,publishTime,available,filterReason}
Requests run as in-page fetch on a cp.kuaishou.com tab; session cookie auth is
enough, the kuaishou.web.cp.api_ph body field is optional. Note photoId in the
collection APIs is a 12-digit NUMBER (e.g. 208946344748), unrelated to the
base36 workId string used by the works-list endpoint.
Titles are capped at 12 chars by the platform; per-day name "AI合集MMDD" fits.

State persisted to collections_state.json so collections are created once.
"""
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.jsonio import atomic_write_json, read_json_safe  # noqa: E402

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE.parent / "collections_state.json"

CDP_URL_DEFAULT = "http://127.0.0.1:9222"
DY_COLLECTION_CREATE = "https://creator.douyin.com/creator-micro/content/collection/create"
KS_COLLECTION_MANAGE = "https://cp.kuaishou.com/article/manage/collection"
KS_API_BASE = "https://cp.kuaishou.com/rest/cp/works/v2"
KS_PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"
DY_PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/publish"

# Platform caps 合集标题 at 12 chars; create uses empty bizType, update uses 30 (默认).
KS_TITLE_MAX = 12
KS_BIZ_DEFAULT = 30
KS_LIST_PAGE_SIZE = 20
KS_UNRECORDED_LIMIT = 50


def _load_state() -> dict:
    state = read_json_safe(STATE_FILE, default={})
    if not isinstance(state, dict):
        return {"douyin": {}, "kuaishou": {}}
    state.setdefault("douyin", {})
    state.setdefault("kuaishou", {})
    return state


# Written from platform threads during the 合集 reconcile phase.
_state_lock = threading.Lock()


def _save_state(state: dict):
    with _state_lock:
        atomic_write_json(STATE_FILE, state)


def get_or_create_douyin_collection(page, *, name: str, cover_path: str, intro: str = "") -> Optional[str]:
    """Create a Douyin collection if not already. Returns collection_id (or name as ref)."""
    state = _load_state()
    if name in state["douyin"]:
        return state["douyin"][name].get("ref")

    page.goto(DY_COLLECTION_CREATE, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    # Dismiss any modals from previous session
    page.evaluate("""() => {
        document.querySelectorAll('.semi-modal-wrap, .semi-portal [role=\"dialog\"], .dy-creator-modal').forEach(e => e.remove());
    }""")
    time.sleep(1)

    # Fill title
    title_inp = page.locator("input[placeholder*='合集']").first
    if title_inp.count() == 0:
        return None
    title_inp.click()
    title_inp.fill(name)
    time.sleep(1)

    # Fill intro (optional)
    if intro:
        intro_inp = page.locator("textarea[placeholder*='合集']").first
        if intro_inp.count() > 0:
            intro_inp.click()
            intro_inp.fill(intro[:200])
            time.sleep(1)

    # Upload cover image (file input is hidden, use page.set_input_files)
    page.set_input_files("input[type=file]", cover_path, timeout=30000, no_wait_after=True)
    time.sleep(4)

    # Click 保存 (commits cover image)
    save_btn = page.locator("button:has-text('保存')").last
    if save_btn.count() > 0:
        save_btn.click(timeout=10000, force=True)
        time.sleep(3)

    # Click 创建
    btn = page.locator("button:has-text('创建')").last
    if btn.count() == 0:
        return None
    btn.click(timeout=15000, force=True)
    time.sleep(5)

    # Verify: either on manage page with collection listed, or url changed
    cur_url = page.url
    body = page.evaluate("() => document.body.innerText.slice(0, 500)")

    if name in body or "manage" in cur_url or "已创建" in body:
        state["douyin"][name] = {"created_at": time.time(), "ref": name, "url": cur_url}
        _save_state(state)
        return name

    state["douyin"][name] = {"created_at": time.time(), "ref": name, "url": cur_url, "verify_body": body[:200]}
    _save_state(state)
    return name


def attach_douyin_to_collection(page, *, collection_name: str) -> bool:
    """On current Douyin publish page, click '请选择合集' and select the named collection."""
    try:
        # Click placeholder "请选择合集" (the actual semi-select trigger)
        placeholder = page.locator("text=请选择合集").first
        if placeholder.count() > 0:
            placeholder.scroll_into_view_if_needed(timeout=5000)
            placeholder.click(timeout=10000, force=True)
            time.sleep(2)

        # Or click on the semi-select element directly
        sel = page.locator("[aria-haspopup='listbox']").first
        if sel.count() > 0:
            sel.click(timeout=10000, force=True)
            time.sleep(2)

        # Wait for listbox with options
        opt = page.locator(f"text={collection_name}").last
        if opt.count() > 0:
            opt.click(timeout=10000, force=True)
            time.sleep(2)
            return True

        print(f"[dy-attach] collection '{collection_name}' not in listbox")
        return False
    except Exception as e:
        print(f"[dy-attach] error: {e}")
        return False


# ---------------------------------------------------------------------------
# Douyin — API-direct reconcile (verified 2026-09)
#
# Publish-time attach goes through the publish-page dropdown above; these
# endpoints repair a mix after publishing (the early-2026-09 runs posted 5
# works before attach was reliable). All in-page fetch on creator.douyin.com.
#
#   GET  /janus/douyin/creator/pc/work_list?status=0&count=20&max_cursor=...
#        &scene=star_atlas&device_platform=android&aid=1128
#        -> {status_code:0, aweme_list:[{aweme_id,item_title,desc,create_time,...}]}
#   GET  /web/api/mix/list/?count=12&cursor=0&status=0,1,2,3,6
#        -> {mix_list:[{mix_id,mix_name,status:{status}}]}
#   GET  /web/api/mix/item/list/?mix_id=..&count=50&cursor=0
#        -> {aweme_list:[{aweme_id,...}],has_more,cursor}
#   POST /web/api/mix/item/editlist/  (urlencoded form!)
#        body: mix_id=..&item_ids=<full ordered member list>&add_item_ids=<new ids>
#        -> {status_code:0}. item_ids REPLACES the membership, so union first.
# ---------------------------------------------------------------------------

DY_WORK_LIST_PATH = ("/janus/douyin/creator/pc/work_list?status=0&count=20"
                     "&scene=star_atlas&device_platform=android&aid=1128")
DY_MIX_LIST_PATH = "/web/api/mix/list/?count=12&cursor=0&status=0,1,2,3,6"
DY_MIX_ITEM_LIST_PATH = "/web/api/mix/item/list/?count=50&cursor=0"
DY_MIX_EDITLIST_PATH = "/web/api/mix/item/editlist/"
DY_CREATOR_HOST = "https://creator.douyin.com"
DY_WORK_PAGE_SIZE = 20


_DY_FETCH_JS = r"""
async ({url, method, body}) => {
  const opt = {method: method || 'GET', headers: {'accept': 'application/json'}};
  if (body) {
    opt.headers['content-type'] = 'application/x-www-form-urlencoded;charset=UTF-8';
    opt.body = body;
  }
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => null);
  return {http: r.status, json: j};
}
"""


def _dy_ensure_host(page) -> None:
    if "creator.douyin.com" not in page.url:
        page.goto(DY_CREATOR_HOST + "/creator-micro/content/manage",
                  wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)


def _dy_get(page, path: str):
    _dy_ensure_host(page)
    url = path if path.startswith("http") else DY_CREATOR_HOST + path
    out = page.evaluate(_DY_FETCH_JS, {"url": url, "method": "GET"})
    j = (out or {}).get("json")
    if not j or j.get("status_code") not in (0, None):
        msg = (j or {}).get("status_msg") or f"http={out.get('http') if out else 'none'}"
        raise RuntimeError(f"dy api GET {path.split('?')[0]} failed: {msg}")
    return j


def _dy_post_form(page, path: str, body: str):
    _dy_ensure_host(page)
    out = page.evaluate(_DY_FETCH_JS,
                        {"url": DY_CREATOR_HOST + path, "method": "POST", "body": body})
    j = (out or {}).get("json")
    if not j or j.get("status_code") != 0:
        msg = (j or {}).get("status_msg") or f"http={out.get('http') if out else 'none'}"
        raise RuntimeError(f"dy api POST {path} failed: {msg}")
    return j


def dy_list_mixes(page) -> list:
    """All creator mixes, newest first."""
    return _dy_get(page, DY_MIX_LIST_PATH).get("mix_list") or []


def dy_mix_id_by_name(page, name: str):
    row = next((m for m in dy_list_mixes(page) if m.get("mix_name") == name), None)
    return str(row["mix_id"]) if row else None


def dy_mix_members(page, mix_id: str) -> list:
    """Ordered aweme_id list of a mix (paged)."""
    ids, cursor = [], 0
    for _ in range(20):
        j = _dy_get(page, f"{DY_MIX_ITEM_LIST_PATH}&mix_id={mix_id}&cursor={cursor}")
        rows = j.get("aweme_list") or []
        ids.extend(str(w.get("aweme_id")) for w in rows if w.get("aweme_id"))
        if not j.get("has_more") or not rows:
            break
        cursor = j.get("cursor") or 0
    return ids


def dy_today_works(page, *, day_start_ts: float, max_pages: int = 16) -> list:
    """Published works since day_start_ts (work_list is newest first), as
    [{aweme_id, head, create_time}] where head is title-normalized text."""
    works, cursor = [], 0
    for _ in range(max_pages):
        j = _dy_get(page, f"{DY_WORK_LIST_PATH}&max_cursor={cursor}")
        rows = j.get("aweme_list") or []
        if not rows:
            break
        stop = False
        for w in rows:
            ct = w.get("create_time") or 0
            if ct < day_start_ts:
                stop = True
                continue
            head = (w.get("item_title") or w.get("desc") or "").split("#")[0].strip()
            works.append({"aweme_id": str(w.get("aweme_id")), "head": head,
                          "desc": w.get("desc") or "", "create_time": ct})
        if stop or not j.get("has_more"):
            break
        cursor = j.get("max_cursor") or 0
        if not cursor:
            break
    return works


def reconcile_douyin_daily_mix(page, *, mix_name: str, wanted_titles: list,
                               day_start_ts: float = None) -> bool:
    """Attach any of wanted_titles (published since day_start_ts) missing from
    the mix. day_start_ts defaults to local midnight now; callers spanning
    midnight should pass the run-start day boundary.

    Idempotent: current members stay (editlist REPLACES item_ids, so the full
    ordered list is always sent with only the missing ids in add_item_ids).
    Returns True when the mix ends with all wanted works in it.
    """
    from urllib.parse import quote

    if day_start_ts is None:
        day_start_ts = time.mktime(
            time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    mix_id = dy_mix_id_by_name(page, mix_name)
    if not mix_id:
        print(f"[dy-mix] '{mix_name}' not found; skip reconcile (created at publish)")
        return False

    members = dy_mix_members(page, mix_id)
    member_set = set(members)
    works = dy_today_works(page, day_start_ts=day_start_ts)

    missing = []
    for title in wanted_titles:
        row = next((w for w in works
                    if w["head"] == title or w["head"].startswith(title)
                    or w["desc"].startswith(title)), None)
        if row and row["aweme_id"] not in member_set:
            missing.append(row)
    if not missing:
        print(f"[dy-mix] '{mix_name}' already has all {len(wanted_titles)} "
              f"wanted works (members={len(members)})")
        return True

    add_ids = [w["aweme_id"] for w in missing]
    full_ids = members + add_ids
    # The web client percent-encodes commas (%2C); keep the same encoding.
    body = ("mix_id=" + mix_id
            + "&item_ids=" + quote(",".join(full_ids))
            + "&add_item_ids=" + quote(",".join(add_ids)))
    _dy_post_form(page, DY_MIX_EDITLIST_PATH, body)
    time.sleep(1)

    after = set(dy_mix_members(page, mix_id))
    ok = all(i in after for i in add_ids)
    print(f"[dy-mix] '{mix_name}' added={len(add_ids)}/{len(wanted_titles)} "
          f"total={len(after)} titles={[w['head'][:10] for w in missing]} "
          f"verify={'ok' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# Kuaishou — API-direct (verified 2026-09, see module docstring for endpoints)
# ---------------------------------------------------------------------------

_KS_FETCH_JS = r"""
async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
  const j = await r.json().catch(() => null);
  return {http: r.status, json: j};
}
"""


def _ks_ensure_host(page) -> None:
    """Make sure the in-page fetch runs on a cp.kuaishou.com origin."""
    if "cp.kuaishou.com" not in page.url:
        page.goto(KS_COLLECTION_MANAGE, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)


def _ks_post(page, path: str, body: dict):
    """POST a KS creator REST endpoint via in-page fetch; return data dict."""
    _ks_ensure_host(page)
    out = page.evaluate(_KS_FETCH_JS, {"url": KS_API_BASE + path, "body": body})
    j = (out or {}).get("json")
    if not j or j.get("result") != 1:
        msg = (j or {}).get("message") or f"http={out.get('http') if out else 'no-response'}"
        raise RuntimeError(f"ks api {path} failed: {msg}")
    return j.get("data")


def ks_list_collections(page) -> list:
    """All collections (including empty ones), newest first."""
    data = _ks_post(page, "/collection/list", {
        "count": KS_LIST_PAGE_SIZE, "page": 1,
        "showOnProfileFilter": 0, "publishStatus": 0, "type": "0",
    })
    return (data or {}).get("list", [])


def ks_collection_info(page, collection_id: int) -> dict:
    """Full collection record (title, type, showType, photoIdList, ...)."""
    return _ks_post(page, "/collection/info", {
        "collectionId": collection_id, "tubeId": str(collection_id),
    })


def ks_unrecorded_works(page, max_pages: int = 20) -> list:
    """All works still addable to some collection, newest first (paged)."""
    rows = []
    cursor = None
    for _ in range(max_pages):
        body = {"limit": 20}
        if cursor:
            body["cursor"] = cursor
        data = _ks_post(page, "/collection/unrecorded/list/v2", body)
        page_rows = (data or {}).get("list", [])
        rows.extend(page_rows)
        cursor = (data or {}).get("nextCursor")
        if not cursor or not page_rows:
            break
    return rows


def _norm_title(s: str) -> str:
    """Collapse a KS title/caption to CJK+alnum chars for fuzzy matching."""
    return "".join(ch for ch in (s or "") if ch.isalnum())


def get_or_create_kuaishou_collection(page, *, name: str) -> Optional[str]:
    """Find by exact (12-char-capped) title, else create empty. Returns name."""
    short_name = name[:KS_TITLE_MAX]
    state = _load_state()
    existing = next((c for c in ks_list_collections(page)
                     if c.get("title") == short_name), None)
    if existing is None:
        data = _ks_post(page, "/collection/add", {
            "title": short_name,
            "type": "",
            "showType": 1,
            "photoIdList": [],
            "showStatusType": 1,
        })
        # Server sometimes returns collectionId: 0; resolve by re-listing.
        # The list read also lags creation by a few seconds (2026-09-15:
        # immediate re-list missed collections that DID exist), so poll.
        created_id = (data or {}).get("collectionId")
        existing = None
        for _ in range(5):
            time.sleep(1.5)
            existing = next((c for c in ks_list_collections(page)
                             if c.get("title") == short_name), None)
            if existing is not None:
                break
        if existing is None:
            print(f"[ks-coll] create '{short_name}' not confirmed "
                  f"(resp collectionId={created_id})")
            return None
    state["kuaishou"][name] = {
        "created_at": time.time(), "ref": name,
        "collection_id": existing["collectionId"], "short": short_name,
    }
    _save_state(state)
    return name


def _resolve_kuaishou_collection(page, collection_name: str) -> Optional[dict]:
    short_name = collection_name[:KS_TITLE_MAX]
    return next((c for c in ks_list_collections(page)
                 if c.get("title") == short_name), None)


def ks_photo_infos(page, photo_ids: list) -> list:
    """Title/metadata for photos already in a collection (photo/info, chunked)."""
    rows = []
    for i in range(0, len(photo_ids), 50):
        rows.extend(_ks_post(page, "/collection/photo/info",
                             {"photoIdList": photo_ids[i:i + 50]}) or [])
    return rows


def add_videos_to_kuaishou_collection(page, *, collection_name: str,
                                      video_titles: list) -> bool:
    """Attach works (matched by title prefix) to a collection via update API.

    Update replaces photoIdList, so the current list is fetched first and new
    ids are unioned in; existing members are never removed. Idempotent: works
    already in the collection count as satisfied without any write. A work that
    is unavailable because it belongs to ANOTHER collection is reported blocked
    (KS membership is exclusive).
    """
    coll = _resolve_kuaishou_collection(page, collection_name)
    if coll is None:
        print(f"[ks-coll-add] no collection '{collection_name[:KS_TITLE_MAX]}'")
        return False
    collection_id = coll["collectionId"]
    info = ks_collection_info(page, collection_id)
    photo_ids = list(info.get("photoIdList") or [])

    members = ks_photo_infos(page, photo_ids) if photo_ids else []
    member_keys = [_norm_title(m.get("title", "")) for m in members]

    works = ks_unrecorded_works(page)
    wanted = [_norm_title(t) for t in video_titles]
    already, added_titles, blocked = 0, [], []
    for key in wanted:
        if any(mk.startswith(key) for mk in member_keys):
            already += 1
            continue
        row = next((w for w in works
                    if _norm_title(w.get("title", "")).startswith(key)), None)
        if row is None:
            blocked.append(f"{key}:not_found")
        elif not row.get("available", True):
            blocked.append(f"{key}:{row.get('filterReason') or 'unavailable'}")
        elif row["photoId"] not in photo_ids:
            photo_ids.append(row["photoId"])
            added_titles.append(key)

    newly_added = len(added_titles)
    if newly_added:
        _ks_post(page, "/collection/update", {
            "collectionId": collection_id,
            "tubeId": str(collection_id),
            "title": info.get("title") or coll["title"],
            "description": info.get("description", ""),
            "type": info.get("type") or KS_BIZ_DEFAULT,
            "showType": info.get("showType", 1),
            "photoIdList": photo_ids,
        })
        # Membership reads lag the write by a second or two (verified 2026-09:
        # immediate re-fetch returned the old list while the member was in fact
        # attached), so poll instead of trusting the first read.
        got = -1
        for _ in range(4):
            time.sleep(1.5)
            got = len(ks_collection_info(page, collection_id).get("photoIdList") or [])
            if got == len(photo_ids):
                break
        if got != len(photo_ids):
            print(f"[ks-coll-add] verify FAIL want={len(photo_ids)} got={got}")
            return False

    print(f"[ks-coll-add] '{collection_name[:KS_TITLE_MAX]}' "
          f"already={already} newly_added={newly_added}/{len(wanted)} "
          f"total={len(photo_ids)} blocked={blocked}")
    return not blocked
