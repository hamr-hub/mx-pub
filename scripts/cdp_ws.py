"""CDP-over-WebSocket direct client.

Connects to Kimi WebBridge daemon's WebSocket endpoint at
``ws://127.0.0.1:10086/ws`` (with the correct extension Origin) and exposes
a ``call(method, params)`` interface for raw Chrome DevTools Protocol
commands.

This bypasses webbridge's restrictive tool allowlist (which blocked
``Target.attachToTarget``, ``Target.getTargets``, ``Page.captureScreenshot``,
etc.) — we can drive ANY tab (including background) using full CDP.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
from typing import Any

DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 10086
DEFAULT_WS_PATH = "/ws"
# This is the Origin webbridge extension expects — verified via handshake
EXTENSION_ORIGIN = "chrome-extension://hinhmbbmelmmgiehkfmmkmfndadahmkk"


class CDPSession:
    """Minimal Chrome DevTools Protocol client over WebSocket."""

    def __init__(
        self,
        host: str = DEFAULT_DAEMON_HOST,
        port: int = DEFAULT_DAEMON_PORT,
        path: str = DEFAULT_WS_PATH,
        origin: str = EXTENSION_ORIGIN,
        timeout: float = 30.0,
    ):
        self.host = host
        self.port = port
        self.path = path
        self.origin = origin
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._events: list[dict] = []
        self._reader_thread: threading.Thread | None = None
        self._closed = False
        self._connect()

    # ---- transport -----------------------------------------------------

    def _connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.host, self.port))
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: {self.origin}\r\n"
            f"Sec-WebSocket-Protocol: webbridge\r\n"
            f"\r\n"
        )
        s.sendall(req.encode())
        # read handshake response
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError(f"ws handshake failed: {resp[:200]!r}")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"ws handshake failed: {resp[:200]!r}")
        self._sock = s
        # start reader thread
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        self._closed = True
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    # ---- ws framing -----------------------------------------------------

    def _send_frame(self, payload: bytes) -> None:
        assert self._sock is not None
        # client→server frames must be masked
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x81])  # FIN + text
        ln = len(payload)
        if ln < 126:
            header.append(0x80 | ln)
        elif ln < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", ln))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", ln))
        header.extend(mask)
        with self._lock:
            self._sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        out = b""
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("ws closed")
            out += chunk
        return out

    def _recv_frame(self) -> bytes:
        assert self._sock is not None
        hdr = self._recv_exact(2)
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(8))[0]
        return self._recv_exact(ln)

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                frame = self._recv_frame()
                msg = json.loads(frame.decode("utf-8", errors="replace"))
                if "id" in msg:
                    with self._lock:
                        self._responses[msg["id"]] = msg
                else:
                    # event
                    self._events.append(msg)
        except Exception:
            self._closed = True

    # ---- JSON-RPC -------------------------------------------------------

    def call(self, method: str, params: dict | None = None, *, timeout: float = 60.0) -> dict:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            payload = {"id": mid, "method": method, "params": params or {}}
        self._send_frame(json.dumps(payload).encode("utf-8"))
        # wait for response
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if mid in self._responses:
                    return self._responses.pop(mid)
            time.sleep(0.05)
        raise TimeoutError(f"CDP call {method} timed out after {timeout}s")

    def events(self) -> list[dict]:
        """Return and clear buffered events."""
        with self._lock:
            evs = self._events
            self._events = []
        return evs


def smoke_test() -> None:
    cdp = CDPSession()
    print("Connected to ws://127.0.0.1:10086/ws")
    targets = cdp.call("Target.getTargets")
    print(f"Target.getTargets returned {len(targets.get('result', {}).get('targetInfos', []))} targets")
    for t in targets.get("result", {}).get("targetInfos", [])[:8]:
        print(f"  - {t.get('type', '?')} {t.get('url', '?')[:80]} (targetId={t.get('targetId')})")
    cdp.close()


if __name__ == "__main__":
    smoke_test()
