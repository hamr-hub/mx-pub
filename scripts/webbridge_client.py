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
