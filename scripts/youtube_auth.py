"""OAuth2 desktop (loopback) authorization for the YouTube Data API v3.

An API key is deliberately NOT enough: videos.insert requires an OAuth access
token carrying the youtube.upload scope, and Google answers an API-key upload
with 401 "API keys are not supported by this API".

One-time setup
--------------
1. Google Cloud Console -> enable "YouTube Data API v3".
2. OAuth consent screen: External is fine; add the channel's Google account
   to the test users (an unpublished app can only be authorized by them).
3. Create an OAuth client ID of type "Desktop app", download its JSON, and
   save it as secrets/youtube_client_secret.json.
4. Run `python3 scripts/youtube_auth.py`, approve the consent page once.

The resulting access + refresh tokens are cached in secrets/youtube_token.json
(0600). Later runs refresh silently; no browser is opened again.
"""
import http.client
import json
import mimetypes
import queue
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = REPO_ROOT / "secrets"
CLIENT_SECRET_PATH = SECRETS_DIR / "youtube_client_secret.json"
TOKEN_PATH = SECRETS_DIR / "youtube_token.json"

# Least privilege: upload-only. The token cannot read channel data or manage
# playlists, which is also why `probe` hits tokeninfo instead of channels.list.
SCOPE = "https://www.googleapis.com/auth/youtube.upload"

REDIRECT_HOST = "127.0.0.1"
# Desktop clients may use any loopback port without pre-registering it.
CONSENT_TIMEOUT_S = 300
REFRESH_SKEW_S = 60

_TOKEN_URI_FALLBACK = "https://oauth2.googleapis.com/token"
_AUTH_URI_FALLBACK = "https://accounts.google.com/o/oauth2/v2/auth"


class _RobustHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that races ALL resolved addresses.

    Google's CDN answers DNS with rotating IPs (172.217.x.x), and on some
    networks individual addresses are blackholed: the TCP handshake simply
    hangs forever. Stock urllib resolves via gethostbyname (single A record,
    no fallback); curl's Happy Eyeballs transparently picks a reachable one.
    This tries every getaddrinfo result with a short connect timeout so one
    dead IP cannot wedge a whole upload (observed: stuck 7+ min on .118.4
    while .113.4 was reachable).
    """

    connect_timeout = 6

    def connect(self):
        # Happy Eyeballs: race every resolved address. Sequential fallback
        # measured 66s (eight blackholed IPs at 8s each before one answered).
        infos = socket.getaddrinfo(self.host, self.port, proto=socket.IPPROTO_TCP)
        context = self._context or ssl.create_default_context()
        winners: queue.Queue = queue.Queue()
        claimed = threading.Event()

        def attempt(fam, socktype, proto, sockaddr):
            sock = socket.socket(fam, socktype, proto)
            sock.settimeout(self.connect_timeout)
            try:
                sock.connect(sockaddr)
                tls = context.wrap_socket(sock, server_hostname=self.host)
            except OSError:
                sock.close()
                return
            if claimed.is_set():
                tls.close()  # a faster address already won
            else:
                winners.put(tls)

        threads = [threading.Thread(target=attempt, args=info[0:3] + (info[4],),
                                    daemon=True)
                   for info in infos]
        for t in threads:
            t.start()
        try:
            winner = winners.get(timeout=self.connect_timeout + 3)
        except queue.Empty:
            raise ConnectionError(
                f"all {len(infos)} addresses for {self.host} unreachable")
        claimed.set()
        # Drain any ties and close the losers.
        while True:
            try:
                extra = winners.get_nowait()
            except queue.Empty:
                break
            if extra is not winner:
                extra.close()
        self.sock = winner
        self.sock.settimeout(self.timeout)


def robust_opener(connect_timeout: int = 8):
    """urllib opener using the multi-IP fallback connection."""

    class _Conn(_RobustHTTPSConnection):
        pass

    _Conn.connect_timeout = connect_timeout

    class _Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_Conn, req)

    # build_opener keeps the stock HTTPHandler (http:// loopback fakes in
    # tests); only the https transport is replaced.
    return urllib.request.build_opener(_Handler)


# Reused by the uploader (init POST + chunk PUTs to the Location host).
DEFAULT_OPENER = robust_opener()


class OAuthError(RuntimeError):
    """The OAuth dance or a token exchange failed."""


def _post_form(url: str, fields: dict, timeout: int = 30,
               opener=None) -> dict:
    """POST application/x-www-form-urlencoded and parse the JSON reply."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    open_ = (opener or DEFAULT_OPENER).open
    try:
        with open_(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise OAuthError(f"token endpoint {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise OAuthError(f"token endpoint unreachable: {e.reason}") from None


def load_client_config(path: Path = CLIENT_SECRET_PATH) -> dict:
    """Load the downloaded Desktop-client JSON into a normalized shape."""
    if not path.exists():
        raise OAuthError(
            f"missing OAuth client secret: {path}. Download the Desktop-app "
            "OAuth client JSON from Google Cloud Console first."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = raw.get("installed") or raw.get("web")
    if not cfg:
        raise OAuthError(f"{path}: expected an 'installed' (Desktop app) client")
    return {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "auth_uri": cfg.get("auth_uri", _AUTH_URI_FALLBACK),
        "token_uri": cfg.get("token_uri", _TOKEN_URI_FALLBACK),
    }


def _write_token_file(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    path.chmod(0o600)


def _persist_exchange(resp: dict, previous_refresh: str | None,
                      path: Path) -> dict:
    """Convert a token-endpoint response to our cached shape and save it."""
    if "access_token" not in resp:
        raise OAuthError(f"token response has no access_token: {resp}")
    now = int(time.time())
    token = {
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token", previous_refresh),
        "scope": resp.get("scope", SCOPE),
        "token_type": resp.get("token_type", "Bearer"),
        "expires_at": now + int(resp.get("expires_in", 3600)),
    }
    _write_token_file(token, path)
    return token


def refresh_access_token(client: dict, refresh_token: str,
                         token_path: Path = TOKEN_PATH) -> dict:
    """Exchange a refresh token for a fresh access token."""
    resp = _post_form(client["token_uri"], {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    return _persist_exchange(resp, refresh_token, token_path)


def _wait_for_code(redirect_port: int, state: str) -> str:
    """Run a one-shot loopback server and return the captured auth code."""
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence stderr access logs
            pass

        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if params.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write("state mismatch".encode())
                return
            if "code" not in params:
                result["error"] = params.get("error", ["unknown"])[0]
            else:
                result["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "授权成功，可以关闭此页面" if "code" in result else result["error"]
            self.wfile.write(f"<h2>{ok}</h2>".encode())

    httpd = ThreadingHTTPServer((REDIRECT_HOST, redirect_port), Handler)
    httpd.timeout = 5
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.time() + CONSENT_TIMEOUT_S
        while "code" not in result and "error" not in result:
            if time.time() > deadline:
                raise OAuthError("timed out waiting for the browser consent")
            httpd.handle_request()
        if "error" in result:
            raise OAuthError(f"consent denied: {result['error']}")
        return result["code"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def authorize_interactive(client_path: Path = CLIENT_SECRET_PATH,
                          token_path: Path = TOKEN_PATH) -> dict:
    """Run the full loopback consent flow and cache the granted tokens."""
    client = load_client_config(client_path)
    # Bind first so the redirect URI carries the real ephemeral port.
    probe = ThreadingHTTPServer((REDIRECT_HOST, 0), lambda *a, **k: None)
    port = probe.server_address[1]
    probe.server_close()

    redirect_uri = f"http://{REDIRECT_HOST}:{port}"
    state = secrets.token_urlsafe(24)
    auth_url = client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        # Force a refresh token even if the app was authorized before.
        "prompt": "consent",
        "state": state,
    })
    print(f"[youtube-auth] opening: {auth_url}")
    if not webbrowser.open(auth_url):
        print("[youtube-auth] could not open a browser; paste the URL above")

    code = _wait_for_code(port, state)
    resp = _post_form(client["token_uri"], {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    token = _persist_exchange(resp, None, token_path)
    print(f"[youtube-auth] authorized, token cached at {token_path}")
    return token


def get_access_token(client_path: Path = CLIENT_SECRET_PATH,
                     token_path: Path = TOKEN_PATH) -> str:
    """Return a valid access token, refreshing the cached one if needed."""
    if not token_path.exists():
        raise OAuthError(
            f"no cached token at {token_path}; run "
            "`python3 scripts/youtube_auth.py` once to authorize"
        )
    token = json.loads(token_path.read_text(encoding="utf-8"))
    if token.get("expires_at", 0) - REFRESH_SKEW_S > time.time():
        return token["access_token"]
    if not token.get("refresh_token"):
        raise OAuthError("access token expired and no refresh_token cached")
    client = load_client_config(client_path)
    return refresh_access_token(client, token["refresh_token"],
                                token_path)["access_token"]


def probe(token_path: Path = TOKEN_PATH) -> dict:
    """Validate the cached access token against Google's tokeninfo endpoint."""
    access = get_access_token()
    url = ("https://oauth2.googleapis.com/tokeninfo?access_token="
           + urllib.parse.quote(access))
    with DEFAULT_OPENER.open(url, timeout=20) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    if SCOPE not in (info.get("scope") or "").split():
        raise OAuthError(f"token missing scope {SCOPE}: {info.get('scope')}")
    return info


def guess_mime_type(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "video/mp4"


def main() -> int:
    try:
        if len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "probe":
            info = probe()
            print(f"[youtube-auth] ok scope={info['scope']} "
                  f"expires_in={info.get('expires_in')}s")
        else:
            authorize_interactive()
    except OAuthError as e:
        print(f"[youtube-auth] {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
