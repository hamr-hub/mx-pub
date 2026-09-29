"""Raw-CDP page attachment — truth verification that never fights for the
browser-level endpoint.

Playwright's connect_over_cdp uses the /devtools/browser websocket; when
another client (e.g. an IDE playwright-mcp session) already owns browser-level
context management, Chrome answers 'Browser context management is not
supported'. The per-target /devtools/page/<id> websocket is independent and
always accepts Runtime/Page commands. This module is the stable fallback for
read-only in-page fetch verification.

It deliberately implements only what verification needs: open/list tabs over
the CDP HTTP API and evaluate JS (promise-aware) over the page websocket.
"""
from __future__ import annotations

import json
import select
import time
import urllib.parse
import urllib.request
from typing import Optional

import websocket  # websocket-client

CDP_HTTP = "http://127.0.0.1:9222"
NEW_TAB_TIMEOUT_S = 30
EVAL_TIMEOUT_S = 60


class CdpError(RuntimeError):
    pass


def list_tabs(cdp_http: str = CDP_HTTP) -> list[dict]:
    with urllib.request.urlopen(f"{cdp_http}/json/list", timeout=10) as resp:
        return [t for t in json.load(resp) if t.get("type") == "page"]


def new_tab(url: str, cdp_http: str = CDP_HTTP) -> dict:
    """Open a tab via the CDP HTTP endpoint (works without a browser client)."""
    enc = urllib.parse.quote(url, safe="")
    # Some Chrome builds accept PUT, others GET; try PUT then GET.
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(f"{cdp_http}/json/new?{enc}",
                                         method=method)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except Exception:
            continue
    raise CdpError(f"could not open tab {url}")


def find_tab(url_substr: str, cdp_http: str = CDP_HTTP) -> Optional[dict]:
    return next((t for t in list_tabs(cdp_http) if url_substr in t.get("url", "")),
                None)


def close_tab(tab_id: str, cdp_http: str = CDP_HTTP) -> None:
    try:
        urllib.request.urlopen(f"{cdp_http}/json/close/{tab_id}", timeout=10)
    except Exception:
        pass


class PageSession:
    """One websocket session attached to a single page target."""

    def __init__(self, ws_url: str, timeout: float = EVAL_TIMEOUT_S):
        self._ws = websocket.create_connection(
            ws_url, timeout=timeout, suppress_origin=True)
        self._seq = 0
        self.send("Runtime.enable")
        self.send("Page.enable")

    def send(self, method: str, params: dict | None = None,
             timeout: float | None = None) -> dict:
        self._seq += 1
        msg_id = self._seq
        self._ws.send(json.dumps({"id": msg_id, "method": method,
                                  "params": params or {}}))
        deadline = time.time() + (timeout if timeout is not None
                                  else self._ws.timeout)
        while True:
            # Enforce the deadline ourselves: recv() on a wedged socket blocks
            # forever, which used to hang the whole publisher on a dead CDP
            # transport instead of raising a recoverable CdpError.
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CdpError(f"{method}: CDP response timed out after "
                               f"{timeout if timeout is not None else 'default'}s")
            raw = self._recv_with_deadline(remaining)
            data = json.loads(raw)
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                raise CdpError(f"{method}: {data['error']}")
            return data.get("result") or {}

    def _recv_with_deadline(self, remaining: float) -> str:
        """recv() with a hard deadline.

        The websocket client's socket timeout is coarse (it applies per
        frame, not per exchange), so a half-open CDP connection could block
        indefinitely. select() on the raw socket gives us a real budget.
        """
        sock = getattr(self._ws, "sock", None) or getattr(self._ws, "_sock", None)
        if sock is None:                      # no socket access -> best effort
            return self._ws.recv()
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            raise CdpError("CDP recv timed out")
        return self._ws.recv()

    def evaluate(self, expression: str, timeout: float = EVAL_TIMEOUT_S):
        # send() already unwraps CDP's outer {"result": ...} envelope; here we
        # get Runtime.evaluate's payload {"result": RemoteObject,
        # "exceptionDetails"?: ...}.
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "timeout": int(timeout * 1000),
        }, timeout=timeout + 5)
        exc = result.get("exceptionDetails")
        if exc:
            text = (exc.get("exception") or {}).get("description", "")[:300]
            raise CdpError(f"evaluate exception: {text}")
        return result.get("result", {}).get("value")

    def goto(self, url: str, wait_s: float = 3.0) -> None:
        self.send("Page.navigate", {"url": url})
        time.sleep(wait_s)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self) -> "PageSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def attach(url_substr: str, *, cdp_http: str = CDP_HTTP,
           ensure_url: str | None = None) -> PageSession:
    """Attach to the first tab containing url_substr; optionally navigate it."""
    tab = find_tab(url_substr, cdp_http)
    if tab is None:
        if not ensure_url:
            raise CdpError(f"no tab matching {url_substr}")
        tab = new_tab(ensure_url, cdp_http)
        time.sleep(4)
    session = PageSession(tab["webSocketDebuggerUrl"])
    if ensure_url and url_substr not in tab.get("url", ""):
        session.goto(ensure_url)
    return session
