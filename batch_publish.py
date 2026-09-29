#!/usr/bin/env python3
"""Stable batch publisher — one CDP connection per platform, one tab reused
for the whole queue. Each platform runs in its own thread and processes its
own pending videos sequentially; platforms run concurrently with each other.

Flow per video is signal-driven (see scripts/platforms/*.py): upload finish
API -> fill form -> topics/AI-declaration/collection -> publish API response.
After each platform's loop, ok works are reconciled into 合集 API-direct,
idempotently and best-effort (Kuaishou union update, Douyin mix editlist;
see scripts/mx_collections.py). Videos with an explicit `collection` field
go to their CONTENT collection (grouped by subject across production dates);
undated videos fall back to the run-day AI合集. The run day is pinned at
start so a batch crossing midnight keeps one consistent daily fallback.

Env:
  SKIP_PLATFORMS   comma-separated, e.g. SKIP_PLATFORMS=xhs
  ONLY_PLATFORMS   comma-separated whitelist
  CDP_URL          default http://127.0.0.1:9222
  RETRY_FAILED=1   retry videos whose last attempt was a transient fail
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

from publisher import (
    PublishResult,
    is_retryable_failure,
    track,
    _get_module,
)
from core.environment import ensure_chrome, get_env_summary
from core.watch import is_fatal_connection_error
from core.media import is_publishable_duration, missing_media_reason
from core.jsonio import atomic_write_json, read_json_safe

QUEUE_FILE = Path(os.environ.get("MX_QUEUE_FILE", HERE / "publish_queue.json"))
DEFAULT_PLATFORMS = ["douyin", "kuaishou", "weixin", "xhs"]
DONE_STATUSES = ("ok", "blocked")

_state_lock = threading.Lock()


def load_queue() -> dict:
    return read_json_safe(QUEUE_FILE, default={}) or {
        "total": 0, "videos": [], "published": 0, "remaining": 0}


def save_queue(queue: dict) -> None:
    # Atomic: a crash mid-write must never truncate the publish queue, and
    # platform threads save after every video.
    atomic_write_json(QUEUE_FILE, queue)


def resolve_platforms() -> list:
    only = os.environ.get("ONLY_PLATFORMS", "").strip()
    if only:
        plats = [p.strip() for p in only.split(",") if p.strip()]
    else:
        skip = {p.strip() for p in os.environ.get("SKIP_PLATFORMS", "").split(",") if p.strip()}
        # XHS profile is logged out — skip unless explicitly whitelisted
        skip.add("xhs")
        plats = [p for p in DEFAULT_PLATFORMS if p not in skip]
    return plats


def derive_topics(video: dict) -> list:
    """Deterministic topic fallback from theme + title when queue has none."""
    existing = [t.lstrip("#") for t in (video.get("hashtags") or []) if t.strip()]
    if existing:
        return existing[:3]
    topics = []
    theme = (video.get("_theme") or "").replace("/", " ").split()
    for word in theme + [video.get("title", "")]:
        word = word.strip()
        if word and word not in topics:
            topics.append(word)
    return topics[:3]


def needs_publish(video: dict, platform: str,
                  *, retry_failed: bool = False) -> bool:
    """Should this video be published to `platform` on this run?

    Three tiers, matching the taxonomy in scripts/core/errors.py:

    * ``ok`` / ``blocked``  — terminal, never re-attempted. A blocked row is a
      short clip or a safety veto; re-uploading it is pure waste.
    * ``fail`` + transient  — re-armed by default. A dead CDP transport or a
      locator timeout says nothing about the video, and a fresh session
      routinely publishes what the last one could not.
    * ``fail`` + permanent  — terminal by default. Content rejections, account
      limits and banned uploads do not become publishable by trying harder;
      re-arming them on every run is how an account gets flagged. Set
      RETRY_FAILED=1 to force them anyway.
    """
    info = video.get("published", {}).get(platform, {})
    if not isinstance(info, dict):
        return True
    status = info.get("status")
    if status in DONE_STATUSES:
        return False
    if status != "fail":
        return True
    if retry_failed:
        return True
    return is_retryable_failure(info.get("error"))


def result_dict(result: PublishResult) -> dict:
    return {
        "ts": time.time(),
        "method": result.method,
        "status": result.status,
        "error": (result.error or "")[:200],
        "duration_s": round(result.duration_s or 0, 1),
    }


def group_ok_works(queue: dict, platform: str,
                   day_start: float, key_fn=None) -> tuple[dict, list]:
    """Split ok works into content-themed groups and the legacy daily list.

    Group entries are MATCH KEYS for the platform's work list, not always the
    queue title: KS has no title field and the live caption starts with the
    description, so callers pass a caption-derived key_fn. A video carrying an
    explicit `collection` field belongs to that CONTENT collection (collections
    group by subject, not production date) and is reconciled regardless of when
    it went live: re-running next day must still repair a missed attach. Videos
    without `collection` keep the old run-day daily-collection fallback.
    """
    def key(v: dict) -> str:
        return key_fn(v) if key_fn else v.get("title", "")

    themed: dict[str, list[str]] = {}
    daily: list[str] = []
    for v in queue["videos"]:
        info = v.get("published", {}).get(platform)
        if not (isinstance(info, dict) and info.get("status") == "ok"):
            continue
        collection = v.get("collection")
        if collection:
            themed.setdefault(collection, []).append(key(v))
        elif (info.get("ts") or 0) >= day_start:
            daily.append(key(v))
    return themed, daily


def ks_caption_key(v: dict) -> str:
    """Prefix the live KS caption actually starts with.

    KS folds everything into the description editor; publish strips the
    embedded '#tag' tail (hard 4-topic cap) and re-inserts smart topics after
    the text, so the caption begins with the stripped description — never with
    the queue's short title.
    """
    from platforms.kuaishou import _strip_embedded_hashtags

    return _strip_embedded_hashtags(v.get("description") or v.get("title", ""))


def attach_kuaishou_collections(page, queue: dict, *, coll_name: str,
                                day_start: float) -> None:
    """After KS publishes, attach ok works to 合集 API-direct.

    Themed videos go to their content collection (created on demand via API);
    undated videos fall back to the run-day AI合集MMDD. KS membership is
    exclusive and update replaces photoIdList; the helper unions existing
    members and is idempotent, so converging every run is safe. Best-effort:
    never alters publish statuses.
    """
    from mx_collections import (
        add_videos_to_kuaishou_collection,
        get_or_create_kuaishou_collection,
    )

    themed, daily = group_ok_works(queue, "kuaishou", day_start,
                                   key_fn=ks_caption_key)
    groups = [(name, titles) for name, titles in themed.items()]
    if daily:
        groups.append((coll_name, daily))
    if not groups:
        print("[kuaishou] no ok works to attach; skip 合集 attach")
        return

    for name, titles in groups:
        try:
            if get_or_create_kuaishou_collection(page, name=name):
                add_videos_to_kuaishou_collection(
                    page, collection_name=name, video_titles=titles)
        except Exception as e:
            if is_fatal_connection_error(e):
                raise
            print(f"[kuaishou] collection '{name}' attach failed (non-fatal): "
                  f"{str(e)[:150]}")


def reconcile_douyin_collections(page, queue: dict, *, coll_name: str,
                                 day_start: float) -> None:
    """After DY publishes, repair mix membership for every ok work.

    Themed mixes are reconciled across full work history (day_start_ts=0: a
    content mix spans production dates); the undated daily mix keeps the
    run-day window. Themed mixes must pre-exist (precreate_dy_mixes.py with a
    cover image) — a missing mix is skipped, never auto-created. API-direct,
    idempotent, best-effort; never alters publish statuses.
    """
    from mx_collections import reconcile_douyin_daily_mix

    themed, daily = group_ok_works(queue, "douyin", day_start)
    targets = [(name, titles, 0.0) for name, titles in themed.items()]
    if daily:
        targets.append((coll_name, daily, day_start))
    if not targets:
        print("[douyin] no ok works to reconcile; skip mix reconcile")
        return

    for name, titles, since in targets:
        try:
            reconcile_douyin_daily_mix(
                page, mix_name=name, wanted_titles=titles,
                day_start_ts=since)
        except Exception as e:
            if is_fatal_connection_error(e):
                raise
            print(f"[douyin] mix '{name}' reconcile failed (non-fatal): "
                  f"{str(e)[:150]}")


def run_platform(platform: str, queue: dict, cdp_url: str,
                  *, _stats=None, _pacing=None,
                  _risk_force: bool = False,
                  _retry_failed: bool = False) -> dict:
    """Own connection + own tab; iterate every pending video for this platform."""
    from playwright.sync_api import sync_playwright
    if _HAS_NEXT_GEN:
        if _maybe_risk_block(platform, _stats, force=_risk_force):
            print(f"[{platform}] ⛔ 风控 critical, 跳过本次批次", flush=True)
            return {"ok": 0, "partial": 0, "fail": 0, "errors": ["risk_blocked"]}
        if _pacing is not None and not _pacing_gate(platform, _pacing):
            return {"ok": 0, "partial": 0, "fail": 0,
                    "errors": ["pacing_blocked"]}

    summary = {"ok": 0, "fail": 0, "partial": 0, "blocked": 0, "errors": []}
    videos = [v for v in queue["videos"]
              if needs_publish(v, platform, retry_failed=_retry_failed)]
    limit = os.environ.get("LIMIT_PER_PLATFORM", "").strip()
    if limit.isdigit():
        videos = videos[:int(limit)]
    # KS/DY still connect with zero pending to reconcile the run-day 合集.
    # Pin the day at run start: a long batch can cross midnight, and the
    # collection name/ts filter must not flip halfway through the run.
    day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"),
                                          "%Y-%m-%d"))
    today_coll = f"AI合集_{time.strftime('%Y%m%d')}"
    ks_coll_name = f"AI合集{time.strftime('%m%d')}"
    if not videos and platform not in ("kuaishou", "douyin"):
        return summary

    print(f"[{platform}] {len(videos)} videos pending")
    mod = _get_module(platform)
    topics_map = {id(v): derive_topics(v) for v in videos}

    def _stop_pw(driver) -> None:
        if driver is None:
            return
        try:
            driver.stop()
        except Exception:
            pass

    def _open_session(driver):
        """Start playwright, connect CDP, bind the platform tab.

        Always stops the node driver on connect failure: a leaked driver keeps
        a half-open browser ws that wedges Chrome's browser endpoint for later
        clients ("Browser context management is not supported").
        """
        # Playwright's CDP handshake hangs when Chrome has zero page targets
        # (observed Chrome 154: ws connects, init never completes). Ensure one
        # tab via the CDP HTTP endpoint before attaching.
        try:
            from core.raw_cdp import list_tabs, new_tab
            if not list_tabs():
                new_tab("about:blank")
        except Exception:
            pass
        driver = sync_playwright().start()
        try:
            br = driver.chromium.connect_over_cdp(cdp_url, timeout=20000)
        except Exception:
            _stop_pw(driver)
            raise
        context = br.contexts[0]
        target = None
        if hasattr(mod, "_match_url"):
            for pg in context.pages:
                try:
                    if mod._match_url()(pg.url):
                        target = pg
                        break
                except Exception:
                    continue
        if target is None:
            target = context.new_page()
        if hasattr(mod, "_setup_page"):
            mod._setup_page(target)
        return driver, br, target

    def _recover_session(driver):
        """Self-heal a dead CDP transport mid-batch.

        2026-09-15: on this machine the automation Chrome is terminated by an
        external signal ~6-7 min into douyin uploads; relaunch + reconnect and
        let the loop continue with the NEXT video. The in-flight video stays
        recorded fail and is reconciled from server truth afterwards — never
        auto-retried (a duplicate is worse than a miss).
        """
        from core.environment import cdp_alive, hard_restart_chrome

        if not cdp_alive(cdp_url):
            print(f"[{platform}] Chrome is gone; relaunching…", flush=True)
            ensure_chrome(cdp_url, headless=False)
            time.sleep(2)
        elif os.environ.get("MX_HARD_RESTART") == "1":
            # Wedged browser endpoint with a live process. Single-platform
            # runs only: shared Chrome would kill sibling platform threads.
            print(f"[{platform}] CDP wedged with Chrome alive; hard restart",
                  flush=True)
            hard_restart_chrome(cdp_url)
            time.sleep(2)
        return _open_session(driver)

    max_recoveries = int(os.environ.get("MX_MAX_RECOVERY", "6"))
    try:
        pw, browser, page = _open_session(None)
    except Exception as e:
        print(f"[{platform}] connect failed: {e}")
        summary["fail"] = len(videos)
        summary["errors"].append(f"connect: {str(e)[:120]}")
        return summary

    try:
        connection_dead = False
        recoveries = 0
        idx = 0
        while idx < len(videos):
            if connection_dead:
                break
            video = videos[idx]
            idx += 1
            name = video["name"]
            title = video.get("title", "")
            short = f"{title} ({name[:20]})"
            t0 = time.time()
            result = None
            # Initialised here, not in the retry loop: pre-flight blocks
            # (short clip / safety gate) skip the loop entirely, and the
            # telemetry below reads `attempt` for every result.
            attempt = 1
            safety_abort = False
            collection = video.get("collection")
            if platform == "douyin" and not collection:
                collection = today_coll
            video_path = (video.get("platform_paths") or {}).get(platform) \
                or video["path"]
            # Fail fast when the source media is gone (unmounted volume,
            # moved file, truncated copy). is_publishable_duration() fails
            # open on unreadable media, so without this the row reached
            # Playwright and came back as `upload: [Errno 2] ...`, which the
            # classifier scored unknown/retry — burning a browser round-trip
            # per row, every run, until the risk monitor aborted the platform.
            media_problem = missing_media_reason(video_path)
            allowed, dur = (False, 0.0) if media_problem else \
                is_publishable_duration(video_path)
            if media_problem:
                # "fail", not "blocked": a missing file is very often a
                # temporarily-unmounted volume, and a blocked row is terminal
                # forever. The classifier reads file_missing as transient, so
                # the row is retried on a later run without spinning here.
                result = PublishResult(platform, "fail", method="preflight",
                                       error=f"file_missing:{media_problem}")
                print(f"[{platform}] {idx}/{len(videos)} 🚫 skipped {short} "
                      f"({media_problem}: {video_path})", flush=True)
            elif not allowed:
                # Account rule: never post <5s clips. Block before any upload
                # (WX silently rejects <3s; old runs burned 2x420s here).
                result = PublishResult(platform, "blocked", method="preflight",
                                       error=f"duration_{dur:.2f}s_below_min_5s")
                print(f"[{platform}] {idx}/{len(videos)} 🚫 blocked {short} "
                      f"({dur:.2f}s < 5s floor)", flush=True)
            else:
                # Fire pre-publish hook (plugins like safety gate listen here).
                #
                # Default: a veto skips THIS video and the run continues. The
                # old code `break`ed here, which unwound the whole while-loop
                # — one flagged video silently cancelled the rest of the queue
                # and the block was never written back.
                #
                # MX_SAFETY_ABORT_BATCH=1 restores batch-abort semantics for
                # callers who want a content violation to halt the platform for
                # the day rather than publish the rest of the queue.
                publish_it = True
                abort_batch = _abort_batch_on_safety()
                if _HAS_NEXT_GEN:
                    try:
                        video["_page"] = page   # give hooks a page handle
                        try:
                            Hooks.emit("video:before_publish", video=video,
                                        platform=platform)
                        finally:
                            video.pop("_page", None)
                        verdict = video.pop("_safety_verdict", None)
                        if verdict and not verdict.get("allow", True):
                            print(f"[{platform}] {idx}/{len(videos)} "
                                  f"⛔ safety gate blocked: "
                                  f"{verdict.get('reason','')[:80]}", flush=True)
                            result = PublishResult(platform, "blocked",
                                                    method="safety_gate",
                                                    error=f"safety:{verdict.get('reason','')[:120]}")
                            publish_it = False
                            if abort_batch:
                                print(f"[{platform}] "
                                      f"🛑 MX_SAFETY_ABORT_BATCH=1："
                                      f"中止本平台剩余队列", flush=True)
                                safety_abort = True
                    except Exception:
                        pass
                for attempt in (1, 2) if publish_it else ():
                    try:
                        result = mod.publish_on_page(
                            page,
                            title=title,
                            description=video.get("description", ""),
                            video=video_path,
                            topics=topics_map[id(video)],
                            location=video.get("location"),
                            collection=collection,
                        )
                        result.method = result.method or "cdp"
                    except Exception as e:
                        dead = is_fatal_connection_error(e)
                        result = PublishResult(platform, "fail", method="cdp",
                                               error=("fatal_connection_closed" if dead
                                                      else f"exception: {str(e)[:180]}"))
                        if dead:
                            result.duration_s = time.time() - t0
                            print(f"[{platform}] CDP connection dead; aborting "
                                  f"remaining videos: {e}", flush=True)
                            connection_dead = True
                            break
                    result.duration_s = time.time() - t0
                    if result.status == "ok" or attempt == 2:
                        break

                    # ----- smarter retry decision -----
                    # Use the new classifier to decide what kind of failure
                    # we just had and route to the right recovery action.
                    if _HAS_NEXT_GEN:
                        cls = _classify(result.error,
                                         status=result.status,
                                         platform=platform)
                        print(f"[{platform}] {idx}/{len(videos)} "
                              f"failure→{cls.cls.value}/{cls.action.value} "
                              f"sev={cls.severity} err="
                              f"{(result.error or '')[:70]}")
                        # Hard stops: no point retrying.
                        if cls.cls.value == "content":
                            # Skip this video permanently (will be re-marked
                            # below as fail with the class label).
                            break
                        if cls.is_fatal_connection:
                            connection_dead = True
                            break
                        # Run the recovery engine BEFORE deciding to retry.
                        rec = perform_recovery(cls.action, page=page,
                                                platform=platform,
                                                classification=cls,
                                                detail=result.error or "")
                        if rec.requires_human:
                            print(f"[{platform}] {idx}/{len(videos)} "
                                  f"⚠️ 需要人工介入: {rec.detail}",
                                  flush=True)
                            break
                        if rec.action.value == "abort_platform":
                            connection_dead = True
                            break
                        if rec.action.value == "skip_video":
                            break
                        # Honor any cool-down the recovery engine computed.
                        if rec.cooldown_until > time.time():
                            wait = rec.cooldown_until - time.time()
                            wait = min(wait, 60.0)   # cap single-attempt wait
                            print(f"[{platform}] {idx}/{len(videos)} "
                                  f"recovery: cool-down {wait:.1f}s "
                                  f"({cls.label})", flush=True)
                            time.sleep(wait)
                        # REFRESH_PAGE / RELOGIN already moved the page.
                        # Otherwise (RETRY/COOL_DOWN) re-run _setup_page.
                        if cls.action.value not in ("refresh_page",
                                                      "relogin"):
                            try:
                                if hasattr(mod, "_setup_page"):
                                    mod._setup_page(page)
                            except Exception as se:
                                if is_fatal_connection_error(se):
                                    connection_dead = True
                                    break
                        time.sleep(2)
                        continue
                    # ----- legacy retry path (no next-gen) -----
                    if not is_retryable_failure(result.error):
                        break
                    print(f"[{platform}] {idx}/{len(videos)} retry after: "
                          f"{(result.error or '')[:80]}")
                    time.sleep(3)
                    try:
                        if hasattr(mod, "_setup_page"):
                            mod._setup_page(page)
                    except Exception as se:
                        if is_fatal_connection_error(se):
                            connection_dead = True
                            break

            with _state_lock:
                video.setdefault("published", {})[platform] = result_dict(result)
                save_queue(queue)
                track(platform, result)

            # ---- self-healing + self-learning integration ----
            if _pacing is not None:
                # Always feed the pacer, even when the gate is off: this is
                # what makes MX_PACING=1 effective on the very first run.
                try:
                    _pacing.record(platform, success=result.status == "ok")
                except Exception:
                    pass
            if _HAS_NEXT_GEN:
                try:
                    _record_event(_stats, video, result, attempt_n=attempt)
                    Hooks.emit("video:after_publish", video=video,
                               platform=platform, result=result)
                    if result.status not in ("ok", "blocked"):
                        cls = _classify(result.error, status=result.status,
                                        platform=platform)
                        Hooks.emit("video:on_error", video=video,
                                   platform=platform, classification=cls)
                except Exception:
                    pass
            # ----------------------------------------------------
            summary[result.status if result.status in summary else "fail"] += 1
            mark = {"ok": "✅", "partial": "⚠️ ", "fail": "❌",
                    "blocked": "🚫"}.get(result.status, "?")
            print(f"[{platform}] {idx}/{len(videos)} {mark} {result.status} "
                  f"{result.duration_s:.0f}s {short} "
                  f"{(result.error or result.details.get('final') or '')[:70]}")

            if safety_abort:
                # The blocked row is already in the queue file, so a re-run
                # does not resurrect it.
                summary["errors"].append("safety_abort_batch")
                break

            if connection_dead:
                if recoveries >= max_recoveries:
                    print(f"[{platform}] recovery budget ({max_recoveries}) "
                          f"exhausted; aborting rest of queue", flush=True)
                    break
                recoveries += 1
                _stop_pw(pw)
                try:
                    pw, browser, page = _recover_session(None)
                    connection_dead = False
                    print(f"[{platform}] session recovered "
                          f"({recoveries}/{max_recoveries}); resuming at "
                          f"{idx + 1}/{len(videos)}", flush=True)
                except Exception as recov_err:
                    print(f"[{platform}] recovery failed: {recov_err}",
                          flush=True)
                    break
                continue

        if platform == "kuaishou" and not connection_dead:
            attach_kuaishou_collections(
                page, queue, coll_name=ks_coll_name, day_start=day_start)
        elif platform == "douyin" and not connection_dead:
            reconcile_douyin_collections(
                page, queue, coll_name=today_coll, day_start=day_start)
    finally:
        try:
            pw.stop()
        except Exception:
            pass

    return summary


def queue_platforms(queue: dict, run_platforms: list | None = None) -> list[str]:
    """Platforms a video must reach to count as fully published.

    Derived from the queue's own records, unioned with this run's platforms,
    minus force-skipped ones. The old code required only the platforms in the
    current run, so a single-platform run (`--platform douyin`, or
    ONLY_PLATFORMS) declared the entire queue complete and persisted
    `remaining: 0` — permanently mis-reporting real pending work.

    Deriving from records also self-heals: a platform that was never targeted
    (xhs, force-skipped) has no rows and stays out of the requirement set.
    """
    plats = {p for v in queue.get("videos", [])
             for p in (v.get("published") or {})}
    plats.update(run_platforms or [])
    skip = {"xhs"}          # hardcoded default if the registry is unavailable
    try:
        from extension.registry import Registry, register_builtin_platforms
        register_builtin_platforms()          # idempotent
        specs = Registry.list_platforms()
        if specs:
            skip = {spec.name for spec in specs if spec.force_skip}
    except Exception:
        pass
    return sorted(plats - skip)


def recompute_totals(queue: dict, platforms: list | None = None) -> None:
    """Recompute the headline counters and the per-platform breakdown."""
    required = queue_platforms(queue, platforms)
    videos = queue.get("videos", [])

    def done(v: dict, p: str) -> bool:
        info = (v.get("published") or {}).get(p)
        return isinstance(info, dict) and info.get("status") in DONE_STATUSES

    queue["published"] = sum(1 for v in videos
                             if all(done(v, p) for p in required))
    queue["remaining"] = queue.get("total", len(videos)) - queue["published"]
    # Per-platform progress: what a single "published/total" figure hides.
    queue["by_platform"] = {
        p: {"done": sum(1 for v in videos if done(v, p)),
            "pending": sum(1 for v in videos
                           if not done(v, p)
                           and needs_publish(v, p, retry_failed=False))}
        for p in required
    }


def main(platforms: list | None = None, *, retry_failed: bool = False,
         force: bool = False) -> int:
    """Run one batch.

    `platforms` overrides env-based resolution so programmatic callers
    (mxpub.py) get exactly the list they gated — previously the wrapper's
    --platform filter was silently discarded and the batch ran everywhere.
    """
    env = get_env_summary()
    cdp_url = ensure_chrome(headless=env["is_headless"])
    print(f"🌍 cdp={cdp_url} headless={env['is_headless']}")

    queue = load_queue()
    platforms = list(platforms) if platforms else resolve_platforms()
    if retry_failed:
        print("🔁 RETRY_FAILED: re-arming rows whose last attempt was a "
              "transient failure")
    pending = {p: sum(1 for v in queue["videos"]
                      if needs_publish(v, p, retry_failed=retry_failed))
               for p in platforms}
    print(f"📋 {queue['total']} videos; pending: {pending}")

    # Self-learning + adaptive pacing
    _stats = StatsStore() if _HAS_NEXT_GEN else None
    _pacing = (PacingController.load() if _HAS_NEXT_GEN else None)
    Hooks.emit("batch:before", queue=queue, platforms=platforms)

    results = {}
    with ThreadPoolExecutor(max_workers=len(platforms)) as pool:
        futs = {pool.submit(run_platform, p, queue, cdp_url,
                              _stats=_stats, _pacing=_pacing,
                              _risk_force=force,
                              _retry_failed=retry_failed): p for p in platforms}
        for fut in as_completed(futs):
            platform = futs[fut]
            try:
                results[platform] = fut.result()
            except Exception as e:
                results[platform] = {"ok": 0, "fail": 1, "errors": [str(e)[:150]]}

    with _state_lock:
        recompute_totals(queue, platforms)
        save_queue(queue)

    if _HAS_NEXT_GEN and _pacing is not None:
        try:
            _pacing.save()
        except Exception:
            pass
    Hooks.emit("batch:after", queue=queue, summary=results)

    print("\n========== SUMMARY ==========")
    for p in platforms:
        r = results.get(p, {})
        print(f"  {p:9s} ok={r.get('ok', 0)} partial={r.get('partial', 0)} "
              f"fail={r.get('fail', 0)} blocked={r.get('blocked', 0)} "
              f"{'; '.join(r.get('errors', []))[:100]}")
    print(f"📊 queue: {queue['published']}/{queue['total']} fully done, "
          f"{queue['remaining']} remaining")
    for p, st in (queue.get("by_platform") or {}).items():
        print(f"   {p:9s} done={st['done']:3d}  pending={st['pending']:3d}")
    return 0 if all(r.get("fail", 0) == 0 for r in results.values()) else 1


# ---------------------------------------------------------------------------
# Self-healing + self-learning integration (added 2026-09-15).
# Soft-imports: missing modules degrade gracefully to the original behavior.
# ---------------------------------------------------------------------------
try:
    from core.hooks import Hooks
    from core.pacing import PacingController
    from core.errors import classify as _classify
    from core.recovery import perform_recovery
    from self_learn.stats import StatsStore, Event as _Event
    from self_learn.risk import RiskMonitor
    _HAS_NEXT_GEN = True
except Exception:                                       # pragma: no cover
    _HAS_NEXT_GEN = False


def _record_event(stats: "StatsStore", video: dict, result: PublishResult,
                  attempt_n: int = 1) -> None:
    """Persist one publish attempt into the self-learning event log."""
    if stats is None:
        return
    cls = _classify(result.error, status=result.status,
                     platform=result.platform)
    try:
        stats.record(_Event(
            ts=time.time(),
            platform=result.platform,
            video_id=video.get("title") or video.get("path", ""),
            status=result.status,
            method=result.method,
            duration_s=result.duration_s,
            error=(result.error or "")[:200],
            cls=cls.cls.value,
            action=cls.action.value,
            severity=cls.severity,
            hour=time.localtime().tm_hour,
            weekday=time.localtime().tm_wday,
            content_hash="",
            title_len=len(video.get("title", "") or ""),
            topics_n=len(video.get("hashtags") or []),
            attempt_n=attempt_n,
        ))
    except Exception:
        pass


def _abort_batch_on_safety() -> bool:
    """Whether a safety-gate veto should halt the whole platform batch.

    Default False: a single flagged video must not cancel the rest of the
    queue (that was the old `break` bug). Operators who would rather stop the
    platform for the day on a content violation can opt back in.
    """
    return os.environ.get("MX_SAFETY_ABORT_BATCH", "0").strip() not in (
        "0", "", "off", "false")


def _pacing_enabled() -> bool:
    """Adaptive pacing is opt-in: it can insert multi-minute waits.

    Publishing throughput is the whole point of the batch driver, so the gate
    stays off by default (MX_PACING=0) — but results are ALWAYS recorded, so
    the learned state is warm the moment it is switched on.
    """
    return os.environ.get("MX_PACING", "0").strip() not in ("0", "", "off", "false")


def _pacing_gate(platform: str, pacing) -> bool:
    """Honour the adaptive pacer. Returns False to skip this platform.

    Previously the gate printed "pacing 拦截", slept 5s and then published
    anyway — the protection existed only in the log. Now a veto is a veto: the
    caller waits up to MX_PACING_MAX_WAIT_S for the window to open, then skips
    the platform instead of hammering it.
    """
    if not _pacing_enabled():
        return True
    max_wait = float(os.environ.get("MX_PACING_MAX_WAIT_S", "300"))
    deadline = time.time() + max_wait
    while True:
        allow, reason = pacing.allow(platform)
        if allow:
            return True
        if time.time() >= deadline:
            print(f"[{platform}] ⏳ pacing 拦截（{reason}）；等待 {max_wait:.0f}s "
                  f"后仍未放行，跳过本平台", flush=True)
            return False
        wait = min(30.0, max(1.0, deadline - time.time()))
        print(f"[{platform}] ⏳ pacing 等待: {reason}; {wait:.0f}s", flush=True)
        time.sleep(wait)


def _maybe_risk_block(platform: str, stats, *,
                      force: bool = False) -> bool:
    """Returns True if the platform should be skipped this run due to risk."""
    if stats is None or force:
        return False
    try:
        rep = RiskMonitor(stats).check(platform)
        return rep.level == "critical"
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
