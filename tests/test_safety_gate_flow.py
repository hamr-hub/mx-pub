"""End-to-end control-flow test for the safety gate inside run_platform().

This drives the REAL publish loop with a fake Playwright session and a fake
platform module — no Chrome, no network. It is the only test that proves the
queue-level semantics of a safety veto, which is the difference between
"skip one video" and "silently cancel the rest of the batch".
"""
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import batch_publish  # noqa: E402
from publisher import PublishResult  # noqa: E402

N = 4
FLAGGED = 1


def _make_queue(n=N):
    return {
        "total": n,
        "videos": [
            {"name": f"v{i}.mp4", "title": f"video-{i}",
             "path": f"/tmp/v{i}.mp4", "description": "d",
             "hashtags": ["t"], "published": {}}
            for i in range(n)
        ],
    }


def _run_loop(monkeypatch, *, safety_env=None, save_queue=None,
              queue=None, stub_media=True, result_fn=None,
              max_recovery="0", raise_fn=None):
    """Run the real run_platform loop; return (attempted titles, summary)."""
    attempted: list[str] = []

    class FakeMod:
        @staticmethod
        def publish_on_page(page, title, description, video, topics,
                            location, collection):
            attempted.append(title)
            if raise_fn is not None:
                return raise_fn("douyin", title)
            if result_fn is not None:
                return result_fn("douyin", title)
            return PublishResult("douyin", "ok", method="fake")

        @staticmethod
        def _setup_page(page):
            pass

    class FakePage:
        url = "about:blank"

    class FakeCtx:
        pages = [FakePage()]

        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeCtx()]

    driver = types.SimpleNamespace(stop=lambda: None)
    driver.chromium = types.SimpleNamespace(
        connect_over_cdp=lambda url, timeout=0: FakeBrowser())

    fake_pw = types.SimpleNamespace(start=lambda: driver)
    monkeypatch.setattr(batch_publish, "_get_module", lambda p: FakeMod)
    monkeypatch.setattr(batch_publish, "is_publishable_duration",
                        lambda p: (True, 10.0))
    # This suite is about the safety gate, not media availability: the queue
    # uses synthetic /tmp paths, so let the pre-flight media check pass and
    # keep the real duration stub above as the single media gate here.
    # stub_media=False exercises the real check.
    if stub_media:
        monkeypatch.setattr(batch_publish, "missing_media_reason",
                            lambda p: "")
    monkeypatch.setattr(batch_publish, "save_queue",
                        save_queue or (lambda q: None))
    monkeypatch.setattr(batch_publish, "reconcile_douyin_collections",
                        lambda *a, **k: None)
    monkeypatch.setattr(batch_publish, "attach_kuaishou_collections",
                        lambda *a, **k: None)
    monkeypatch.setenv("MX_MAX_RECOVERY", str(max_recovery))
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: fake_pw, raising=False)
    # raw_cdp.tab probe: pretend a tab already exists
    monkeypatch.setattr("core.raw_cdp.list_tabs", lambda: [{"type": "page"}],
                        raising=False)

    monkeypatch.delenv("MX_SAFETY_ABORT_BATCH", raising=False)
    if safety_env is not None:
        monkeypatch.setenv("MX_SAFETY_ABORT_BATCH", safety_env)

    # safety gate: veto exactly one video through the real hook pipeline
    from core.hooks import Hooks

    def _veto(video=None, platform=None, **kw):
        if video.get("title") == f"video-{FLAGGED}":
            video["_safety_verdict"] = {"allow": False, "reason": "gambling"}

    Hooks.register("video:before_publish", _veto)
    monkeypatch.setattr(batch_publish, "_HAS_NEXT_GEN", True)
    try:
        summary = batch_publish.run_platform(
            "douyin", queue if queue is not None else _make_queue(),
            "http://127.0.0.1:9222")
    finally:
        Hooks._hooks.pop("video:before_publish", None)
    return attempted, summary


def test_safety_veto_skips_only_the_flagged_video(monkeypatch):
    """The regression: a veto used to `break` the whole while-loop, which
    cancelled every remaining video and never recorded the block."""
    attempted, summary = _run_loop(monkeypatch)

    assert attempted == ["video-0", "video-2", "video-3"], (
        "the flagged video must be skipped, the rest of the queue must run")
    assert summary["blocked"] == 1
    assert summary["ok"] == N - 1


def test_safety_veto_is_persisted_to_the_queue(monkeypatch):
    """A block that is not written back is resurrected on the next run."""
    saved: list[dict] = []
    _run_loop(monkeypatch, save_queue=lambda q: saved.append(json_copy(q)))
    assert saved, "the blocked row must be written back to the queue"
    row = next(v for v in saved[-1]["videos"] if v["title"] == "video-1")
    assert row["published"]["douyin"]["status"] == "blocked"
    # ...and must not be re-attempted next run
    assert batch_publish.needs_publish(row, "douyin") is False


def json_copy(obj):
    import copy
    return copy.deepcopy(obj)


def test_safety_abort_batch_stops_the_platform(monkeypatch):
    """Opt-in escape hatch for operators who want a violation to halt the day."""
    attempted, summary = _run_loop(monkeypatch, safety_env="1")

    assert attempted == ["video-0"], "batch abort must stop after the violation"
    assert "safety_abort_batch" in summary["errors"]


def test_abort_batch_env_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MX_SAFETY_ABORT_BATCH", raising=False)
    assert batch_publish._abort_batch_on_safety() is False
    monkeypatch.setenv("MX_SAFETY_ABORT_BATCH", "1")
    assert batch_publish._abort_batch_on_safety() is True
    monkeypatch.setenv("MX_SAFETY_ABORT_BATCH", "off")
    assert batch_publish._abort_batch_on_safety() is False


# ---------------------------------------------------------------------------
# Media pre-flight (the real check, not stubbed)
# ---------------------------------------------------------------------------

def test_missing_media_never_reaches_the_browser(monkeypatch, tmp_path):
    """A row whose file is gone must not cost a Playwright round-trip.

    Regression: is_publishable_duration() fails *open* on unreadable media, so
    every such row was handed to the platform module and came back as
    `upload: [Errno 2] No such file or directory`.
    """
    real = tmp_path / "v0.mp4"
    real.write_bytes(b"x" * 32)
    gone = tmp_path / "v1.mp4"          # deliberately never created

    queue = {
        "total": 2,
        "videos": [
            {"name": "v0.mp4", "title": "video-0", "path": str(real),
             "description": "d", "hashtags": ["t"], "published": {}},
            {"name": "v1.mp4", "title": "video-1", "path": str(gone),
             "description": "d", "hashtags": ["t"], "published": {}},
        ],
    }
    saved: list[dict] = []
    attempted, summary = _run_loop(monkeypatch, queue=queue, stub_media=False,
                                   save_queue=lambda q: saved.append(json_copy(q)))

    assert attempted == ["video-0"], (
        "the missing file must never be handed to the platform module")
    assert summary["ok"] == 1

    row = next(v for v in saved[-1]["videos"] if v["title"] == "video-1")
    info = row["published"]["douyin"]
    assert info["status"] == "fail"
    assert info["error"].startswith("file_missing:")

    # Retry-later, not terminal: an unmounted volume comes back.
    assert batch_publish.needs_publish(row, "douyin") is True


def test_stale_browser_fs_view_restarts_the_session_once(monkeypatch, tmp_path):
    """A driver that cannot see a file the driver *can* see needs a new driver.

    The Playwright driver is a long-lived Node process; when an external volume
    is remounted underneath it, every set_input_files returns ENOENT forever.
    Page-level recovery cannot help, so this must route into the existing
    session-recovery path instead of retrying each remaining row twice.
    """
    videos = []
    for i in range(3):
        f = tmp_path / f"v{i}.mp4"
        f.write_bytes(b"x" * 32)
        # "clip-N", not "video-N": _run_loop's safety veto targets video-1,
        # and this test is about session recovery, not the safety gate.
        videos.append({"name": f"v{i}.mp4", "title": f"clip-{i}",
                       "path": str(f), "description": "d",
                       "hashtags": ["t"], "published": {}})
    queue = {"total": 3, "videos": videos}

    enoent = ("upload: [Errno 2] No such file or directory: "
              f"'{videos[0]['path']}'")
    seen: list[str] = []

    def result_fn(platform, title):
        seen.append(title)
        if len(seen) == 1:                    # only the first attempt fails
            return PublishResult(platform, "fail", method="cdp", error=enoent)
        return PublishResult(platform, "ok", method="fake")

    # Chrome is alive; the session just needs rebuilding.
    monkeypatch.setattr("core.environment.cdp_alive", lambda url: True,
                        raising=False)

    attempted, summary = _run_loop(monkeypatch, queue=queue, stub_media=False,
                                   result_fn=result_fn, max_recovery="2")

    # video-0 fails once, the session is rebuilt, and the remaining videos
    # publish instead of each burning two more doomed round-trips.
    assert attempted == ["clip-0", "clip-1", "clip-2"]
    assert summary["ok"] == 2
    assert summary["fail"] == 1
    # The failed row is transient, so a later run still picks it up.
    assert batch_publish.needs_publish(
        {**videos[0], "published": {"douyin": {"status": "fail",
                                               "error": enoent}}},
        "douyin") is True


def test_crashed_renderer_recovers_and_keeps_publishing(monkeypatch, tmp_path):
    """A crashed tab must cost one rebuild, not the rest of the queue.

    Regression: `Page.bring_to_front: Target crashed` scored unknown/retry, so
    a real run died after 15 consecutive crash rows. The classifier now reads
    it as a dead target, which routes into session recovery — and if Chrome is
    genuinely gone, _recover_session relaunches it before resuming.
    """
    videos = []
    for i in range(3):
        f = tmp_path / f"v{i}.mp4"
        f.write_bytes(b"x" * 32)
        videos.append({"name": f"v{i}.mp4", "title": f"clip-{i}",
                       "path": str(f), "description": "d",
                       "hashtags": ["t"], "published": {}})
    queue = {"total": 3, "videos": videos}

    calls: list[str] = []

    def raise_fn(platform, title):
        calls.append(title)
        if len(calls) == 1:
            raise RuntimeError("Page.bring_to_front: Target crashed")
        return PublishResult(platform, "ok", method="fake")

    # Chrome is gone, so _recover_session must relaunch it.
    monkeypatch.setattr("core.environment.cdp_alive", lambda url: False,
                        raising=False)
    relaunched: list[str] = []
    monkeypatch.setattr("batch_publish.ensure_chrome",
                        lambda url, headless=False: relaunched.append(url),
                        raising=False)

    attempted, summary = _run_loop(monkeypatch, queue=queue, stub_media=False,
                                   raise_fn=raise_fn, max_recovery="2")

    assert relaunched, "a dead Chrome must be relaunched before resuming"
    assert attempted == ["clip-0", "clip-1", "clip-2"]
    assert summary["ok"] == 2
    assert summary["fail"] == 1
