"""
mx-pub: Multi-platform auto-publish orchestrator.

API-first approach:
1. Try direct API (fastest, no tokens)
2. Fall back to browser automation (Playwright/CDP)
3. Track tokens used per attempt

Each platform has a publisher module:
- xhs (xiaohongshu)
- weixin (微信视频号)
- douyin (抖音)
- kuaishou (快手)
- toutiao (今日头条)
- bilibili (B站)

Usage:
  from publisher import publish
  result = publish("weixin", title="...", description="...", video="/path/to/video.mp4")
"""
import json
import time
from pathlib import Path

STATE_FILE = Path(__file__).parent.parent / "publish_state.json"


class PublishResult:
    def __init__(self, platform, status, *, method, duration_s=0, error=None, **details):
        self.platform = platform
        self.status = status  # ok / partial / fail
        self.method = method  # api / extension / cdp / gui
        self.duration_s = duration_s
        self.error = error
        self.details = details
        self.tokens_used = 0  # to be set by caller

    def to_dict(self):
        return {
            "platform": self.platform,
            "status": self.status,
            "method": self.method,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            **self.details,
        }


def track(platform, result: PublishResult):
    """Record publish attempt to state file."""
    state = {"platforms": {}, "history": []}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    if platform not in state["platforms"]:
        state["platforms"][platform] = {"attempts": 0, "successes": 0, "methods": {}, "last_status": None}
    p = state["platforms"][platform]
    p["attempts"] += 1
    if result.status == "ok":
        p["successes"] += 1
    p["last_status"] = result.status
    p["methods"][result.method] = p["methods"].get(result.method, 0) + 1
    p["last_updated"] = time.time()

    state["history"].append({
        "ts": time.time(),
        "platform": platform,
        **result.to_dict(),
        "tokens_used": result.tokens_used,
    })
    state["history"] = state["history"][-100:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def publish(platform, *, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Try API first, then automation. Track tokens used.
    """
    t0 = time.time()

    # Step 1: Try API
    try:
        mod = _get_module(platform)
        if hasattr(mod, "publish_via_api"):
            result = mod.publish_via_api(title=title, description=description, video=video, topics=topics or [], location=location, **kwargs)
            if result.status == "ok":
                result.duration_s = time.time() - t0
                track(platform, result)
                return result
    except Exception as e:
        api_err = str(e)
    else:
        api_err = None

    # Step 2: Try browser automation
    try:
        result = mod.publish_via_browser(title=title, description=description, video=video, topics=topics or [], location=location, **kwargs)
        result.duration_s = time.time() - t0
        track(platform, result)
        return result
    except Exception as e:
        result = PublishResult(platform, "fail", method="cdp", error=f"api={api_err}; cdp={e}")
        result.duration_s = time.time() - t0
        track(platform, result)
        return result


def _get_module(platform):
    """Lazy-import platform module."""
    import importlib
    return importlib.import_module(f"platforms.{platform}")
