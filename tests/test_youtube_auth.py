"""OAuth2 client-config loading and refresh-token exchange.

The interactive consent flow needs a human browser; everything around it
(config parsing, silent refresh when the cached access token has expired) is
exercised here against a loopback fake token endpoint.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import youtube_auth as ya  # noqa: E402


def write_client(path: Path, *, token_uri: str | None = None) -> dict:
    cfg = {"installed": {
        "client_id": "test-client.apps.googleusercontent.com",
        "client_secret": "secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": token_uri or "https://oauth2.googleapis.com/token",
    }}
    path.write_text(json.dumps(cfg))
    return cfg


def test_load_client_config_normalizes_installed(tmp_path):
    p = tmp_path / "client_secret.json"
    write_client(p)
    client = ya.load_client_config(p)
    assert client["client_id"].endswith(".apps.googleusercontent.com")
    assert client["client_secret"] == "secret"


def test_load_client_config_missing_and_malformed(tmp_path):
    with pytest.raises(ya.OAuthError, match="missing OAuth client secret"):
        ya.load_client_config(tmp_path / "nope.json")

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"some_other_type": {}}))
    with pytest.raises(ya.OAuthError, match="Desktop app"):
        ya.load_client_config(bad)


def make_token_server(*, status: int = 200, payload: dict | None = None,
                      fail_first: bool = False):
    attempts = {"n": 0, "fields": None}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            import urllib.parse
            attempts["fields"] = urllib.parse.parse_qs(body)
            attempts["n"] += 1
            if fail_first and attempts["n"] == 1:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            out = payload if payload is not None else {
                "access_token": "fresh-token",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": ya.SCOPE,
            }
            raw = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, attempts


def test_get_access_token_uses_cached_unexpired_token(tmp_path):
    client_p = tmp_path / "client.json"
    write_client(client_p)
    token_p = tmp_path / "token.json"
    token_p.write_text(json.dumps({
        "access_token": "cached-good",
        "expires_at": int(time.time()) + 3600,
    }))
    assert ya.get_access_token(client_p, token_p) == "cached-good"


def test_get_access_token_refreshes_expired_token(tmp_path):
    server, attempts = make_token_server()
    client_p = tmp_path / "client.json"
    uri = f"http://127.0.0.1:{server.server_address[1]}/token"
    write_client(client_p, token_uri=uri)
    token_p = tmp_path / "token.json"
    token_p.write_text(json.dumps({
        "access_token": "stale",
        "refresh_token": "refresh-abc",
        "expires_at": int(time.time()) - 10,
    }))
    try:
        token = ya.get_access_token(client_p, token_p)
        assert token == "fresh-token"
        assert attempts["fields"]["grant_type"] == ["refresh_token"]
        assert attempts["fields"]["refresh_token"] == ["refresh-abc"]

        cached = json.loads(token_p.read_text())
        assert cached["access_token"] == "fresh-token"
        # Google does not always return a new refresh token; keep the old one.
        assert cached["refresh_token"] == "refresh-abc"
        assert cached["expires_at"] > int(time.time())
    finally:
        server.shutdown()
        server.server_close()


def test_refresh_failure_raises_oauth_error(tmp_path):
    server, _ = make_token_server(status=400,
                                  payload={"error": "invalid_grant"})
    client_p = tmp_path / "client.json"
    uri = f"http://127.0.0.1:{server.server_address[1]}/token"
    write_client(client_p, token_uri=uri)
    token_p = tmp_path / "token.json"
    try:
        with pytest.raises(ya.OAuthError, match="400"):
            # An expired cache feeds refresh_access_token directly.
            ya.refresh_access_token(ya.load_client_config(client_p),
                                    "dead-refresh", token_p)
        assert not token_p.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_missing_token_cache_is_actionable(tmp_path):
    client_p = tmp_path / "client.json"
    write_client(client_p)
    with pytest.raises(ya.OAuthError, match="run"):
        ya.get_access_token(client_p, tmp_path / "token.json")
