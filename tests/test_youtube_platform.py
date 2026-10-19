"""YouTube resumable upload: metadata limits + the chunked protocol.

The Google side is faked with a loopback HTTP server implementing just enough
of the resumable protocol (init -> 308 Range -> 200 resource) to prove the
client, including one forced 503 to exercise the retry/resume path.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import platforms.youtube as yt  # noqa: E402


class FakeUploadState:
    def __init__(self, fail_first_chunk=False, reject_init=False):
        self.received = bytearray()
        self.init_headers = {}
        self.metadata = None
        self.fail_first_chunk = fail_first_chunk
        self.reject_init = reject_init
        self.chunk_puts = 0


def make_server(state: FakeUploadState):
    session_path = "/upload-session"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _reply(self, status: int, body: bytes = b"", headers=None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):
            if self.path.startswith("/upload?"):
                if state.reject_init:
                    self._reply(403, json.dumps({"error": {"errors": [
                        {"reason": "quotaExceeded",
                         "message": "quota spent"}]}}).encode(),
                        {"Content-Type": "application/json"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                state.metadata = json.loads(self.rfile.read(length) or b"{}")
                state.init_headers = dict(self.headers)
                self._reply(200, headers={
                    "Location":
                    f"http://127.0.0.1:{self.server.server_address[1]}{session_path}"})
            else:
                self._reply(404)

        def do_PUT(self):
            if self.path != session_path:
                self._reply(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length) if length else b""
            rng = self.headers.get("Content-Range", "")

            # Empty status query: bytes */SIZE
            if data == b"" and rng.startswith("bytes */"):
                headers = ({"Range": f"bytes=0-{len(state.received) - 1}"}
                           if state.received else None)
                self._reply(308, headers=headers)
                return

            state.chunk_puts += 1
            if state.fail_first_chunk and state.chunk_puts == 1:
                self._reply(503, headers={"Retry-After": "0"})
                return

            total = int(rng.rsplit("/", 1)[1])
            start = int(rng.split(" ")[1].split("-")[0])
            # Rebase: only accept bytes continuing what we already hold.
            state.received = state.received[:start] + bytearray(data)

            if len(state.received) < total:
                self._reply(308, headers={
                    "Range": f"bytes=0-{len(state.received) - 1}"})
            else:
                self._reply(200, json.dumps({
                    "id": "vid_42",
                    "status": {"privacyStatus":
                               state.metadata["status"]["privacyStatus"]},
                }).encode(), {"Content-Type": "application/json"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def upload_server():
    state = FakeUploadState()
    server = make_server(state)
    yield f"http://127.0.0.1:{server.server_address[1]}/upload", state
    server.shutdown()
    server.server_close()


def test_build_metadata_enforces_limits_and_defaults():
    meta = yt.build_metadata(
        "x" * 200,
        description="d" * 6000,
        topics=["#nature", "nature", "water", "", " "],
        privacy="unlisted",
        category_id="22",
    )
    sn, st = meta["snippet"], meta["status"]
    assert len(sn["title"]) == yt.TITLE_MAX
    assert len(sn["description"]) == yt.DESC_MAX
    assert sn["tags"] == ["nature", "water"]          # '#' stripped + dedup
    assert sn["categoryId"] == "22"
    assert st["privacyStatus"] == "unlisted"
    assert st["selfDeclaredMadeForKids"] is False


def test_build_metadata_tag_budget():
    tags = [f"tag{i:02d}" for i in range(40)]
    meta = yt.build_metadata("t", topics=tags)
    assert len(meta["snippet"]["tags"]) == yt.TAG_MAX_COUNT


def test_build_metadata_rejects_empty_title_and_bad_privacy():
    with pytest.raises(ValueError):
        yt.build_metadata("   ")
    with pytest.raises(ValueError):
        yt.build_metadata("t", privacy="friends")


def test_build_metadata_publish_at_forces_private():
    meta = yt.build_metadata("t", privacy="public",
                             publish_at="2030-01-01T00:00:00Z")
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["status"]["publishAt"] == "2030-01-01T00:00:00Z"


def test_upload_happy_path(tmp_path, upload_server):
    url, state = upload_server
    video = tmp_path / "clip.mp4"
    # ~9 MiB -> at least 3 chunks at CHUNK_BYTES=4 MiB.
    video.write_bytes(bytes((i % 256 for i in range(9 * 1024 * 1024))))

    result = yt.upload_file("tok", video,
                            yt.build_metadata("Test clip"), upload_url=url)

    assert result["id"] == "vid_42"
    assert state.init_headers["Authorization"] == "Bearer tok"
    assert state.init_headers["X-Upload-Content-Type"] == "video/mp4"
    assert len(state.received) == video.stat().st_size
    assert state.metadata["snippet"]["title"] == "Test clip"


def test_upload_resumes_after_503(tmp_path):
    state = FakeUploadState(fail_first_chunk=True)
    server = make_server(state)
    url = f"http://127.0.0.1:{server.server_address[1]}/upload"
    try:
        video = tmp_path / "clip.mp4"
        video.write_bytes(bytes((i % 256 for i in range(5 * 1024 * 1024))))
        result = yt.upload_file("tok", video,
                                yt.build_metadata("Retry clip"),
                                upload_url=url)
        assert result["id"] == "vid_42"
        assert state.chunk_puts >= 3  # one 503 + the real chunks
        assert len(state.received) == video.stat().st_size
    finally:
        server.shutdown()
        server.server_close()


def test_upload_rejects_missing_and_empty_file(tmp_path):
    with pytest.raises(yt.UploadError, match="file_missing"):
        yt.upload_file("tok", tmp_path / "nope.mp4",
                       yt.build_metadata("t"))
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(yt.UploadError, match="empty_file"):
        yt.upload_file("tok", empty, yt.build_metadata("t"))


def test_init_session_quota_error_is_permanent(tmp_path, upload_server):
    url, _ = upload_server
    state_reject = FakeUploadState(reject_init=True)
    server = make_server(state_reject)
    reject_url = f"http://127.0.0.1:{server.server_address[1]}/upload"
    try:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fakevideo")
        with pytest.raises(yt.UploadError, match="quotaExceeded"):
            yt.upload_file("tok", video, yt.build_metadata("t"),
                           upload_url=reject_url)
    finally:
        server.shutdown()
        server.server_close()


def test_publish_via_api_maps_oauth_failure(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fakevideo")

    def boom():
        raise yt.OAuthError("no cached token")

    monkeypatch.setattr(yt, "get_access_token", boom)
    result = yt.publish_via_api(title="t", video=str(video))
    assert result.status == "fail"
    assert result.error.startswith("oauth:")
    assert result.method == "api"


def test_publish_via_api_success_details(monkeypatch, tmp_path, upload_server):
    url, _ = upload_server
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fakevideo-bytes")
    monkeypatch.setattr(yt, "get_access_token", lambda: "tok")
    result = yt.publish_via_api(title="Clip", description="desc",
                                video=str(video), topics=["nature"],
                                upload_url=url)
    assert result.status == "ok"
    assert result.details["video_id"] == "vid_42"
    assert result.details["url"] == "https://youtu.be/vid_42"
    assert result.details["privacy"] == "private"
