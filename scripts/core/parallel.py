"""Parallel publish orchestrator.

Run all platforms concurrently using ThreadPoolExecutor. Each platform
gets its own Playwright session (independent, no interference).

Target: ≤30s total wall time even when individual platforms take 2+ min.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Any, Callable, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from publisher import PublishResult, track, _get_module, CDP_URL_DEFAULT
from core.browser import (
    connect_chrome,
    find_file_input,
    find_platform_page,
    fill_first,
    click_publish_button,
    is_transient_error,
)


def _publish_one(platform: str, video: dict, cdp_url: str,
                 retry_max: int = 1, base_backoff_s: int = 3,
                 fast_mode: bool = False) -> PublishResult:
    """Run one platform publish in its own Playwright session.

    Each platform runs in its own thread with its own browser connection.
    """
    mod = _get_module(platform)
    title = video.get("title", "")
    description = video.get("description", "")
    video_path = video["path"]
    topics = video.get("hashtags", [])

    attempts = 0
    last_result: Optional[PublishResult] = None

    while attempts <= retry_max:
        attempts += 1
        t0 = time.time()
        try:
            p, browser, ctx = connect_chrome(cdp_url)
        except Exception as e:
            last_result = PublishResult(platform, "fail", method="cdp",
                                        error=f"chrome_connect: {str(e)[:150]}")
            last_result.duration_s = time.time() - t0
            if attempts <= retry_max:
                continue
            track(platform, last_result)
            return last_result

        try:
            # Find page
            page = None
            if hasattr(mod, "_match_url"):
                page = find_platform_page(ctx, mod._match_url())
            if page is None:
                page = ctx.new_page()
            page.bring_to_front()
            if hasattr(mod, "_setup_page"):
                page = mod._setup_page(page) or page

            if hasattr(mod, "publish_on_page"):
                result = mod.publish_on_page(
                    page, title=title, description=description,
                    video=video_path, topics=topics, fast_mode=fast_mode,
                )
                result.method = result.method or "cdp"
            else:
                result = PublishResult(platform, "fail", method="cdp",
                                      error="no_publish_on_page_in_module")
            result.duration_s = time.time() - t0
            last_result = result

            # Retry on transient errors
            if (result.status != "ok"
                    and is_transient_error(result.error)
                    and attempts <= retry_max):
                wait = base_backoff_s * (2 ** (attempts - 1))
                time.sleep(wait)
                continue
            break
        except Exception as e:
            err = str(e)[:200]
            last_result = PublishResult(platform, "fail", method="cdp", error=err)
            last_result.duration_s = time.time() - t0
            if (is_transient_error(err) and attempts <= retry_max):
                wait = base_backoff_s * (2 ** (attempts - 1))
                time.sleep(wait)
                continue
            break
        finally:
            try:
                p.stop()
            except Exception:
                pass

    track(platform, last_result)
    return last_result


def publish_parallel(platforms: list[str], video: dict,
                     cdp_url: str = CDP_URL_DEFAULT,
                     max_workers: int = 4,
                     retry_max: int = 1,
                     fast_mode: bool = False) -> dict[str, PublishResult]:
    """Publish one video to multiple platforms in parallel.

    Each platform runs in its own thread with its own Playwright session.
    Total wall time ≈ slowest individual platform.

    Args:
        platforms: list of platform names
        video: dict with path, title, description, hashtags
        cdp_url: Chrome DevTools Protocol URL
        max_workers: max concurrent threads (default 4, one per platform)
        retry_max: max retry attempts per platform

    Returns:
        dict mapping platform name to PublishResult
    """
    results: dict[str, PublishResult] = {}
    if not platforms:
        return results

    # Run all platforms in parallel
    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for platform in platforms:
            fut = executor.submit(_publish_one, platform, video, cdp_url, retry_max, fast_mode=fast_mode)
            futures[fut] = platform

        for fut in as_completed(futures):
            platform = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = PublishResult(platform, "fail", method="cdp",
                                      error=f"thread: {str(e)[:150]}")
            results[platform] = result

    return results