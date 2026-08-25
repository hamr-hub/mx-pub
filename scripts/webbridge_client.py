"""Minimal HTTP client for Kimi WebBridge daemon (http://127.0.0.1:10086).

The skill SKILL.md exposes ~13 actions; we wrap the subset a publish workflow
needs: navigate, find_tab, list_tabs, snapshot, click, fill, upload, evaluate,
screenshot, network.

Every call carries a top-level ``session`` so the daemon groups our tabs.
We default to ``publish-<unix_ts>`` and let callers override via env or arg.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_DAEMON = os.environ.get("WEBBRIDGE_DAEMON", "http://127.0.0.1:10086")
DEFAULT_SESSION = os.environ.get("WEBBRIDGE_SESSION", f"publish-{int(time.time())}")
REQUEST_TIMEOUT_S = 30


class WebBridgeError(RuntimeError):
    """Raised when the daemon returns ok=false or the HTTP call fails."""


@dataclass
class WebBridge:
    daemon: str = DEFAULT_DAEMON
    session: str = DEFAULT_SESSION
    retries: int = 2
    retry_backoff_s: float = 1.5

    def call(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "action": action,
            "args": args or {},
            "session": self.session,
        }
        body = json.dumps(payload).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(
                    f"{self.daemon}/command",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                if attempt == self.retries:
                    raise WebBridgeError(f"daemon unreachable: {e}") from e
                time.sleep(self.retry_backoff_s * (attempt + 1))
                continue
            except json.JSONDecodeError as e:
                raise WebBridgeError(f"daemon returned non-JSON: {raw!r}") from e
            if not data.get("ok"):
                err = data.get("error") or {}
                msg = err.get("message") or str(err)
                # find_tab "not found" is non-fatal — caller may fall back to navigate
                if err.get("code") in {"extension_error", "not_found"}:
                    raise WebBridgeError(f"{action}: {msg}")
                raise WebBridgeError(f"{action} failed: {msg}")
            return data.get("data", {})
        raise WebBridgeError(f"unreachable: {last_err}")

    # ---- convenience wrappers ------------------------------------------------

    def navigate(self, url: str, *, new_tab: bool = True, group_title: str | None = None) -> dict:
        return self.call("navigate", {"url": url, "newTab": new_tab, "group_title": group_title})

    def find_tab(self, url: str, *, active: bool = False) -> dict | None:
        """Return tab info or None if not found / not foreground."""
        try:
            return self.call("find_tab", {"url": url, "active": active})
        except WebBridgeError as e:
            if "no " in str(e) or "not viewing" in str(e):
                return None
            raise

    def list_tabs(self) -> list[dict]:
        return self.call("list_tabs", {}).get("tabs", [])

    def snapshot(self) -> dict:
        return self.call("snapshot", {})

    def click(self, selector: str) -> dict:
        return self.call("click", {"selector": selector})

    def fill(self, selector: str, value: str) -> dict:
        return self.call("fill", {"selector": selector, "value": value})

    def upload(self, selector: str, files: list[str]) -> dict:
        return self.call("upload", {"selector": selector, "files": files})

    def evaluate(self, code: str) -> dict:
        return self.call("evaluate", {"code": code})

    def screenshot(self, *, path: str | None = None, format: str = "png", quality: int | None = None,
                   selector: str | None = None) -> dict:
        args: dict[str, Any] = {"format": format}
        if quality is not None:
            args["quality"] = quality
        if selector:
            args["selector"] = selector
        if path:
            args["path"] = path
        return self.call("screenshot", args)

    def wait_for(self, *, text: str | None = None, text_gone: str | None = None,
                 seconds: float | None = None) -> dict:
        args: dict[str, Any] = {}
        if text:
            args["text"] = text
        if text_gone:
            args["textGone"] = text_gone
        if seconds is not None:
            args["time"] = seconds
        return self.call("wait_for", args)

    def health(self) -> dict:
        """Hit /status (not /command) to verify daemon liveness."""
        import urllib.request
        with urllib.request.urlopen(f"{self.daemon}/status", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))


def ping() -> dict:
    """One-shot daemon liveness check (used by CLI --health)."""
    return WebBridge().health()


# ===========================================================================
# Backend #2 — Playwright + connect_over_cdp (true background)
#
# Talks directly to Chrome DevTools Protocol on --remote-debugging-port=9222.
# Unlike webbridge, evaluate/click/upload run on whichever Playwright Page we
# hand it — no foreground requirement. This is the **default** backend.
#
# If 9222 is not reachable, ``make_backend(prefer="cdp")`` will fall back to
# ``WebBridge`` (which then asks the user to switch foreground tabs).
# ===========================================================================

try:
    from playwright.sync_api import sync_playwright, Browser, Page, Error as PWError  # type: ignore
except ImportError:  # Playwright not installed → fall back at runtime
    sync_playwright = None  # type: ignore
    Browser = Page = None  # type: ignore
    PWError = Exception


CDP_DEFAULT_URL = os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9222")


class WebBridgeCdp:
    """Playwright-backed backend that runs against the user's real Chrome.

    Launch Chrome with ``--remote-debugging-port=9222`` first (see
    ``docs/background-mode.md``). All evaluate / click / upload run on the
    currently-set page — no foreground dance required.
    """

    def __init__(self, cdp_url: str = CDP_DEFAULT_URL, session: str | None = None,
                 *, headless_timeout_ms: int = 30_000):
        if sync_playwright is None:
            raise WebBridgeError(
                "playwright not installed; `pip install playwright && playwright install chromium`"
            )
        self.cdp_url = cdp_url
        self.session = session or f"cdp-{int(time.time())}"
        self._pw = sync_playwright().start()
        try:
            self._browser: Browser = self._pw.chromium.connect_over_cdp(
                cdp_url, timeout=headless_timeout_ms
            )
        except Exception as e:
            self._pw.stop()
            raise WebBridgeError(f"connect_over_cdp({cdp_url}) failed: {e}") from e
        self._page: Page | None = self._pick_initial_page()
        if self._page is None:
            raise WebBridgeError(f"connected to {cdp_url} but no Pages found")

    def _pick_initial_page(self) -> Page | None:
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                if p.url not in ("about:blank",):
                    return p
        for ctx in self._browser.contexts:
            if ctx.pages:
                return ctx.pages[0]
        return None

    # ---- backend surface (mirrors WebBridge) ---------------------------

    def call(self, action: str, args: dict[str, Any] | None = None) -> dict:
        raise WebBridgeError(f"WebBridgeCdp has no generic .call(); use named methods")

    def _ensure_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = self._pick_initial_page()
            if self._page is None:
                raise WebBridgeError("no live page in CDP connection")
        return self._page

    def health(self) -> dict:
        return {
            "backend": "cdp",
            "cdp_url": self.cdp_url,
            "browser": "chromium",
            "pages": len(self.list_tabs()),
        }

    def list_tabs(self) -> list[dict]:
        out: list[dict] = []
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                out.append({"tabId": id(p), "url": p.url, "title": p.title()})
        return out

    def find_tab(self, url: str, *, active: bool = False) -> dict | None:
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                if p.url.startswith(url.split("?")[0]):
                    self._page = p
                    return {"success": True, "url": p.url, "tabId": id(p), "borrowed": False}
        return None

    def navigate(self, url: str, *, new_tab: bool = True, group_title: str | None = None) -> dict:
        if new_tab or self._page is None:
            ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            self._page = ctx.new_page()
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return {"success": True, "url": self._page.url}

    def snapshot(self) -> dict:
        p = self._ensure_page()
        return {"url": p.url, "title": p.title(),
                "tree": [],
                "body_text": p.evaluate("document.body.innerText")[:4000]}

    def click(self, selector: str) -> dict:
        p = self._ensure_page()
        p.locator(selector).first.click(timeout=10_000)
        return {"success": True}

    def fill(self, selector: str, value: str) -> dict:
        p = self._ensure_page()
        p.locator(selector).first.fill(value, timeout=10_000)
        return {"success": True, "mode": "value"}

    def upload(self, selector: str, files: list[str]) -> dict:
        p = self._ensure_page()
        p.locator(selector).first.set_input_files(files, timeout=30_000)
        return {"success": True, "fileCount": len(files)}

    def evaluate(self, code: str) -> dict:
        p = self._ensure_page()
        try:
            value = p.evaluate(code)
            return {"type": "value", "value": value}
        except PWError as e:
            raise WebBridgeError(f"evaluate failed: {e}") from e

    def screenshot(self, *, path: str | None = None, format: str = "png", quality: int | None = None,
                   selector: str | None = None) -> dict:
        p = self._ensure_page()
        kwargs: dict[str, Any] = {"type": format}
        if quality is not None and format in ("jpeg", "webp"):
            kwargs["quality"] = quality
        if path:
            p.screenshot(path=path, **kwargs)
            return {"format": format, "path": path, "saved": True}
        buf = p.screenshot(**kwargs)
        return {"format": format, "bytes": len(buf)}

    def wait_for(self, *, text: str | None = None, text_gone: str | None = None,
                 seconds: float | None = None) -> dict:
        p = self._ensure_page()
        if text:
            p.locator(f"text={text}").first.wait_for(state="visible", timeout=int((seconds or 5) * 1000))
        if text_gone or seconds:
            p.wait_for_timeout(int((seconds or 2) * 1000))
        return {"success": True}

    def close(self) -> None:
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass


def probe_cdp(cdp_url: str = CDP_DEFAULT_URL, timeout: float = 1.5) -> bool:
    """Return True iff Chrome is reachable on cdp_url (i.e. debug port enabled)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def make_backend(*, prefer: str = "cdp", session: str | None = None,
                 cdp_url: str = CDP_DEFAULT_URL,
                 webbridge_daemon: str = DEFAULT_DAEMON,
                 verbose: bool = True):
    """Return (backend, backend_name). Tries CDP first (default), falls back to webbridge.

    ``prefer``:
      - ``"cdp"`` (default) → try Playwright connect_over_cdp, fall back to webbridge.
      - ``"webbridge"``     → use webbridge directly.
      - ``"cdp-only"``      → fail loudly if CDP not available.
    """
    msgs: list[str] = []

    def log(msg: str) -> None:
        msgs.append(msg)
        if verbose:
            print(f"  [backend] {msg}")

    if prefer in ("cdp", "cdp-only"):
        if probe_cdp(cdp_url):
            try:
                be = WebBridgeCdp(cdp_url=cdp_url, session=session)
                log(f"CDP OK ({cdp_url}) → Playwright backend")
                return be, "cdp"
            except WebBridgeError as e:
                log(f"CDP probe passed but connect failed: {e}")
                if prefer == "cdp-only":
                    raise
        else:
            log(f"CDP not reachable at {cdp_url} (Chrome --remote-debugging-port not enabled)")
            if prefer == "cdp-only":
                raise WebBridgeError(
                    f"CDP required but Chrome not reachable at {cdp_url}. "
                    f"Restart Chrome with --remote-debugging-port=9222."
                )

    be = WebBridge(daemon=webbridge_daemon, session=session)
    log(f"using webbridge backend ({webbridge_daemon}) — foreground tab coordination required")
    return be, "webbridge"
