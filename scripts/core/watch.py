"""Network signal watcher — drives publish flow by real API events, not sleeps.

Usage:
    watch = ApiWatch(page)
    page.locator("input[type=file]").set_input_files(video)
    watch.wait_url("upload/finish", timeout=60)
    ...
    publish_btn.click()
    resp = watch.wait_url("/rest/cp/works/", "publish", timeout=25)
    ok, detail = publish_ok(resp)
"""
import os
import time
from typing import Optional
from urllib.parse import urlparse

MX_DEBUG = os.environ.get("MX_DEBUG", "") == "1"

# 2026-09 /goal: when the shared node driver dies (e.g. the CDP dialog race),
# every evaluate raises one of these; retry loops must stop immediately instead
# of swallowing the error and burning the full per-video timeout budget.
FATAL_CONNECTION_MARKERS = (
    "Connection closed",
    "Playwright connection closed",
    "Target closed",
    # A crashed renderer is a dead target too. Raised as an exception out of
    # page.* calls, so without this the driver treated a crashed tab as a
    # normal per-video failure and retried against it.
    "Target crashed",
    "Target has crashed",
    "Page crashed",
    "page has crashed",
    "Renderer crashed",
    "Target page, context or browser has been closed",
    "Target page has been closed",
    "Browser has been closed",
    "Browser closed",
    "Session closed",
    "page has been closed",
)


def is_fatal_connection_error(exc: BaseException) -> bool:
    """True when the Playwright/CDP transport itself is gone (no retry point)."""
    msg = str(exc)
    return any(m in msg for m in FATAL_CONNECTION_MARKERS)


# One live watcher per page. The batch driver reuses a single tab per platform
# and creates a fresh ApiWatch for every video, but nothing ever called
# remove_listener: listeners accumulated for the life of the tab (96 videos ==
# 96 listeners), so every response was dispatched to all of them and each kept
# growing its own event list. That is a slow memory/CPU leak across a batch.
# Videos are published strictly sequentially per tab, so the previous watcher
# is provably finished when the next one is built — closing it is safe, and its
# captured history stays readable for anyone still holding a reference.
_ACTIVE: dict[int, "ApiWatch"] = {}


class ApiWatch:
    """Capture JSON XHR/fetch responses on a page (includes iframe responses).

    Lifecycle: construct one per video, and it detaches the page listener of
    any watcher this page already had. Prefer an explicit `close()` (or use it
    as a context manager) when you want deterministic teardown.
    """

    def __init__(self, page, method: str = "POST"):
        self.page = page
        self.method = method.upper()
        self.events: list = []
        self.closed = False
        key = id(page)
        previous = _ACTIVE.get(key)
        if previous is not None and previous is not self:
            previous.close()
        _ACTIVE[key] = self
        self._key = key
        page.on("response", self._on_response)

    def close(self) -> None:
        """Detach from the page. Captured events remain readable."""
        if self.closed:
            return
        self.closed = True
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception:
            pass
        if _ACTIVE.get(self._key) is self:
            _ACTIVE.pop(self._key, None)

    def __enter__(self) -> "ApiWatch":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _on_response(self, resp) -> None:
        if self.closed:
            return
        try:
            req = resp.request
            if req.method.upper() != self.method:
                return
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype:
                if MX_DEBUG:
                    print(f"[ApiWatch] skip non-json POST {resp.status} "
                          f"{ctype[:30]} {urlparse(resp.url).path[:100]}", flush=True)
                return
            path = urlparse(resp.url).path
            self.events.append({
                "status": resp.status,
                "url": resp.url,
                "path": path,
                "resp": resp,
            })
            if MX_DEBUG:
                print(f"[ApiWatch] POST {resp.status} {path[:110]}", flush=True)
        except Exception:
            pass

    def find(self, *keywords: str, after: int = 0) -> Optional[dict]:
        """First captured response whose URL contains ALL keywords (after index)."""
        for ev in self.events[after:]:
            url = ev["url"]
            if all(k in url for k in keywords):
                return ev
        return None

    def find_any(self, *patterns: tuple, after: int = 0) -> Optional[dict]:
        """First captured response matching any pattern (tuple of ALL-keywords)."""
        for ev in self.events[after:]:
            url = ev["url"]
            if any(all(k in url for k in kw) for kw in patterns):
                return ev
        return None

    def wait_url(self, *keywords: str, timeout: float = 60,
                 interval: float = 0.4, heartbeat: float = 3.0) -> Optional[dict]:
        """Poll until a matching response was captured.

        heartbeat: probe the CDP connection every N seconds; if the driver is
        dead, raise immediately instead of waiting out the full timeout.
        """
        deadline = time.time() + timeout
        last_beat = time.time()
        while time.time() < deadline:
            ev = self.find(*keywords, after=0)
            if ev is not None:
                return ev
            if heartbeat and time.time() - last_beat >= heartbeat:
                last_beat = time.time()
                try:
                    self.page.evaluate("1")
                except Exception as e:
                    if is_fatal_connection_error(e):
                        raise
            time.sleep(interval)
        return None

    def wait_any(self, patterns: tuple, timeout: float = 25,
                 interval: float = 0.4) -> Optional[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ev = self.find_any(*patterns)
            if ev is not None:
                return ev
            time.sleep(interval)
        return None

    def recent_paths(self, limit: int = 15) -> list:
        """Paths captured since watcher creation (debug logging)."""
        return [f'{e["status"]} {e["path"][-90:]}' for e in self.events[-limit:]]


def body_json(ev: dict) -> Optional[dict]:
    """Safely read a captured response's JSON body."""
    if ev is None:
        return None
    try:
        return ev["resp"].json()
    except Exception:
        return None


def await_publish(watch: "ApiWatch", start_index: int, patterns: tuple,
                  page, dom_success: str, dom_fail: str = "false",
                  timeout: float = 25, interval: float = 0.5,
                  frame=None, api_validator=None):
    """Wait for the publish API call after clicking the publish button.

    patterns: tuple of (ALL-keywords tuple)s matched against response URLs of
    events captured at/after start_index.
    frame: optional Frame to evaluate dom_success/dom_fail in (micro-frontends).
    api_validator: optional callable(dict)->bool; a pattern match whose body
      fails validation is ignored (config endpoints can share path keywords).

    Returns (kind, ev_or_none, json_or_none, dom_text):
      kind in ('api', 'dom', 'fail', 'timeout')
    """
    deadline = time.time() + timeout
    seen_debug = start_index
    rejected = set()
    scope = frame or page
    while time.time() < deadline:
        for ev in watch.events[seen_debug:]:
            seen_debug += 1
            if MX_DEBUG:
                print(f"[await_publish] POST {ev['status']} {ev['path'][:120]}", flush=True)
        for i, ev in enumerate(watch.events[start_index:]):
            if i in rejected:
                continue
            url = ev["url"]
            if any(all(k in url for k in kw) for kw in patterns):
                data = body_json(ev)
                if api_validator is None or api_validator(data):
                    return "api", ev, data, None
                rejected.add(i)
        try:
            if scope.evaluate(f"() => {{ try {{ return !!( {dom_success} ); }} catch (e) {{ return false; }} }}"):
                txt = scope.evaluate("() => document.body.innerText.slice(0, 300)")
                return "dom", None, None, txt
            if scope.evaluate(f"() => {{ try {{ return !!( {dom_fail} ); }} catch (e) {{ return false; }} }}"):
                txt = scope.evaluate("() => document.body.innerText.slice(0, 300)")
                return "fail", None, None, txt
        except Exception as e:
            if is_fatal_connection_error(e):
                raise
        time.sleep(interval)
    return "timeout", None, None, None


def wait_dom(page, js_predicate: str, timeout: float = 90,
             interval: float = 0.5) -> bool:
    """Poll a page until the JS boolean-expression predicate is true.

    The predicate runs inside an IIFE that also catches iframes errors; it must
    itself be an expression (e.g. 'document.querySelector("x") !== null').
    """
    js = f"() => {{ try {{ return !!( {js_predicate} ); }} catch (e) {{ return false; }} }}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(js):
                return True
        except Exception as e:
            if is_fatal_connection_error(e):
                raise
        time.sleep(interval)
    return False
